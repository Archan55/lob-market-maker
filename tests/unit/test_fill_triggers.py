"""Independent hand controls for the retained fill-trigger analyzer.

No application classes or retained cohort values are used. Analyzer import is
deferred until the test fixture runs; this file creates no observations itself.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import struct
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module")
def trigger_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools/audit_fill_triggers.py"
    spec = importlib.util.spec_from_file_location("fill_trigger_unit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _event(
    timestamp: int,
    sequence: int,
    kind: str,
    side: int | None,
    price: int,
    quantity: int,
) -> dict[str, Any]:
    return {
        "timestamp_ns": timestamp,
        "sequence_number": sequence,
        "event_type": kind,
        "side": side,
        "price_ticks": price,
        "quantity": quantity,
    }


def _identity(identifier: str, *, price: int, quantity: int) -> dict[str, Any]:
    return {
        "order_id": identifier,
        "client_order_id": "C-" + identifier,
        "strategy_id": "hand-control",
        "side": 1,
        "price_ticks": price,
        "original_quantity": quantity,
        "creation_timestamp_ns": 0,
        "send_timestamp_ns": 1,
        "exchange_arrival_timestamp_ns": 2,
    }


def _transition(
    identity: dict[str, Any],
    identifier: str,
    previous: str | None,
    new: str,
    timestamp: int,
    remaining: int,
    reason: str | None,
) -> dict[str, Any]:
    return {
        **identity,
        "transition_id": identifier,
        "previous_status": previous,
        "new_status": new,
        "timestamp_ns": timestamp,
        "remaining_quantity": remaining,
        "reason": reason,
    }


def _accept(identity: dict[str, Any]) -> list[dict[str, Any]]:
    identifier = identity["order_id"]
    quantity = identity["original_quantity"]
    return [
        _transition(
            identity,
            identifier + "-created",
            None,
            "pending_arrival",
            2,
            quantity,
            "created",
        ),
        _transition(
            identity,
            identifier + "-accepted",
            "pending_arrival",
            "live",
            2,
            quantity,
            "accepted",
        ),
    ]


def _fill(
    identity: dict[str, Any], identifier: str, quantity: int, timestamp: int
) -> dict[str, Any]:
    return {
        "fill_id": identifier,
        "order_id": identity["order_id"],
        "client_order_id": identity["client_order_id"],
        "strategy_id": identity["strategy_id"],
        "side": identity["side"],
        "quantity": quantity,
        "price_ticks": identity["price_ticks"],
        "liquidity_role": "maker",
        "exchange_fill_timestamp_ns": timestamp,
        "decision_timestamp_ns": identity["creation_timestamp_ns"],
        "send_timestamp_ns": identity["send_timestamp_ns"],
        "exchange_arrival_timestamp_ns": identity["exchange_arrival_timestamp_ns"],
        "strategy_notification_timestamp_ns": timestamp + 1,
    }


def _through_and_exact() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    events = [
        _event(0, 0, "RESET", None, 0, 0),
        _event(0, 1, "SNAPSHOT", 1, 100, 1),
        _event(0, 2, "SNAPSHOT", -1, 104, 1),
        _event(10, 3, "TRADE", 1, 100, 1),
    ]
    through = _identity("T", price=101, quantity=3)
    exact = _identity("E", price=100, quantity=1)
    transitions = [
        *_accept(through),
        *_accept(exact),
        _transition(through, "T-fill", "live", "filled", 10, 0, "fill"),
        _transition(exact, "E-fill", "live", "filled", 10, 0, "fill"),
    ]
    fills = [_fill(through, "F-T", 3, 10), _fill(exact, "F-E", 1, 10)]
    return events, transitions, fills


def _partial_then_through() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    events = [
        _event(0, 0, "RESET", None, 0, 0),
        _event(0, 1, "SNAPSHOT", 1, 100, 2),
        _event(0, 2, "SNAPSHOT", -1, 105, 1),
        _event(10, 3, "TRADE", 1, 100, 2),
        _event(15, 4, "ADD", 1, 99, 1),
        _event(20, 5, "TRADE", 1, 99, 1),
        _event(30, 6, "ADD", -1, 106, 1),
    ]
    order = _identity("P", price=100, quantity=3)
    transitions = [
        *_accept(order),
        _transition(order, "P-fill-1", "live", "partially_filled", 10, 2, "fill"),
        _transition(order, "P-fill-2", "partially_filled", "filled", 20, 0, "fill"),
    ]
    fills = [_fill(order, "F-P1", 1, 10), _fill(order, "F-P2", 2, 20)]
    return events, transitions, fills


def _ratio(numerator: int, denominator: int) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


@pytest.mark.parametrize(
    ("side", "limit", "trade_price", "expected"),
    (
        (1, 100, 100, "exact_price"),
        (-1, 100, 100, "exact_price"),
        (1, 101, 100, "price_through"),
        (-1, 99, 100, "price_through"),
    ),
)
def test_both_side_relations_use_own_limit_against_resting_trade_side(
    trigger_tool: ModuleType, side: int, limit: int, trade_price: int, expected: str
) -> None:
    assert trigger_tool.classify_relation(side, limit, trade_price) == expected


@pytest.mark.parametrize(
    ("side", "limit", "trade_price"), ((1, 99, 100), (-1, 101, 100))
)
def test_worse_price_relation_is_rejected_not_guessed(
    trigger_tool: ModuleType, side: int, limit: int, trade_price: int
) -> None:
    with pytest.raises(ValueError):
        trigger_tool.classify_relation(side, limit, trade_price)


def test_independent_tape_fingerprint_and_timestamp_multiplicity(
    trigger_tool: ModuleType,
) -> None:
    events, _, _ = _through_and_exact()
    original = copy.deepcopy(events)
    observed = trigger_tool.reconstruct_tape(events)
    # The fixed canonical wire contract is timestamp, sequence, kind, side,
    # price and quantity, in big-endian signed widths 64/64/8/8/64/64.
    encoded = b"".join(
        struct.pack(">qqbbqq", *row)
        for row in (
            (0, 0, 4, 0, 0, 0),
            (0, 1, 5, 1, 100, 1),
            (0, 2, 5, -1, 104, 1),
            (10, 3, 3, 1, 100, 1),
        )
    )
    assert observed["event_stream_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert observed["events"] == original
    assert observed["events_by_timestamp"][0] == original[:3]
    assert observed["events_by_timestamp"][10] == original[3:]
    assert events == original


@pytest.mark.parametrize(
    "problem", ("overdraw", "crossed", "time", "sequence", "bool_quantity")
)
def test_tape_admission_uses_strict_integer_depth_without_clamping(
    trigger_tool: ModuleType, problem: str
) -> None:
    events, _, _ = _through_and_exact()
    if problem == "overdraw":
        events[-1]["quantity"] = 2
    elif problem == "crossed":
        events[1]["price_ticks"] = 104
    elif problem == "time":
        events.append(_event(9, 4, "ADD", -1, 105, 1))
    elif problem == "sequence":
        events[-1]["sequence_number"] = 2
    else:
        events[-1]["quantity"] = True
    with pytest.raises(ValueError):
        trigger_tool.reconstruct_tape(events)


def test_through_quantity_does_not_spend_exact_printed_budget_and_shares_are_exhaustive(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _through_and_exact()
    original = copy.deepcopy((events, transitions, fills))
    result = trigger_tool.analyze_rows(events, transitions, fills, Fraction(1, 4))
    assert [
        (
            row["fill_id"],
            row["classification"],
            row["quantity"],
            row["pre_fill_remaining"],
            row["post_fill_remaining"],
            row["distance_ticks"],
            row["tick_notional"],
            row["matched_transition_id"],
        )
        for row in result["fills"]
    ] == [
        ("F-T", "price_through", 3, 3, 0, 1, 303, "T-fill"),
        ("F-E", "exact_price", 1, 1, 0, 0, 100, "E-fill"),
    ]
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["trigger_sequence"] == 3
    assert event["timestamp_ns"] == 10
    assert event["printed_quantity"] == event["exact_quantity"] == 1
    assert event["through_quantity"] == 3
    assert event["combined_quantity"] == 4
    assert event["through_to_printed_ratio"] == _ratio(3, 1)
    assert event["exact_within_printed"] is True
    assert event["combined_exceeds_printed"] is True
    assert result["summary"] == {
        "total": {
            "fill_count": 2,
            "order_count": 2,
            "trigger_event_count": 1,
            "quantity": 4,
            "tick_notional": 403,
            "currency_notional": _ratio(403, 4),
        },
        "classes": {
            "exact_price": {
                "fill_count": 1,
                "order_count": 1,
                "trigger_event_count": 1,
                "quantity": 1,
                "tick_notional": 100,
                "currency_notional": _ratio(25, 1),
                "fill_share": _ratio(1, 2),
                "quantity_share": _ratio(1, 4),
                "tick_notional_share": _ratio(100, 403),
                "order_coverage": _ratio(1, 2),
                "trigger_event_coverage": _ratio(1, 1),
            },
            "price_through": {
                "fill_count": 1,
                "order_count": 1,
                "trigger_event_count": 1,
                "quantity": 3,
                "tick_notional": 303,
                "currency_notional": _ratio(303, 4),
                "fill_share": _ratio(1, 2),
                "quantity_share": _ratio(3, 4),
                "tick_notional_share": _ratio(303, 403),
                "order_coverage": _ratio(1, 2),
                "trigger_event_coverage": _ratio(1, 1),
            },
        },
        "order_set_overlap": 0,
        "trigger_set_overlap": 1,
        "through_distance_ticks": {
            "minimum": 1,
            "maximum": 1,
            "unit_weighted_mean": _ratio(1, 1),
        },
        "order_catalog_count": 2,
        "set_scope": "Within this cohort only; class order/event sets may overlap. Exact-price fills are not calibrated venue executions.",
    }
    assert (events, transitions, fills) == original


def test_partial_exact_then_full_remaining_through_preserves_order_overlap(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _partial_then_through()
    result = trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))
    assert [
        (row["classification"], row["pre_fill_remaining"], row["post_fill_remaining"])
        for row in result["fills"]
    ] == [("exact_price", 3, 2), ("price_through", 2, 0)]
    assert result["summary"]["total"]["order_count"] == 1
    assert result["summary"]["order_set_overlap"] == 1
    assert result["summary"]["trigger_set_overlap"] == 0
    assert result["summary"]["classes"]["exact_price"]["order_coverage"] == _ratio(1, 1)
    assert result["summary"]["classes"]["price_through"]["order_coverage"] == _ratio(
        1, 1
    )
    assert result["summary"]["classes"]["exact_price"]["quantity_share"] == _ratio(1, 3)
    assert result["summary"]["classes"]["price_through"]["quantity_share"] == _ratio(
        2, 3
    )


def test_empty_emitted_fill_set_preserves_zero_counts_and_null_denominators(
    trigger_tool: ModuleType,
) -> None:
    events, _, _ = _through_and_exact()
    result = trigger_tool.analyze_rows(events, [], [], Fraction(1, 4))
    assert result["fills"] == result["events"] == []
    totals = {
        "fill_count": 0,
        "order_count": 0,
        "trigger_event_count": 0,
        "quantity": 0,
        "tick_notional": 0,
        "currency_notional": _ratio(0, 1),
    }
    classes = {
        name: {
            **totals,
            "fill_share": None,
            "quantity_share": None,
            "tick_notional_share": None,
            "order_coverage": None,
            "trigger_event_coverage": None,
        }
        for name in ("exact_price", "price_through")
    }
    assert result["summary"] == {
        "total": totals,
        "classes": classes,
        "order_set_overlap": 0,
        "trigger_set_overlap": 0,
        "through_distance_ticks": {
            "minimum": None,
            "maximum": None,
            "unit_weighted_mean": None,
        },
        "order_catalog_count": 0,
        "set_scope": "Within this cohort only; class order/event sets may overlap. Exact-price fills are not calibrated venue executions.",
    }


def test_same_time_cancellation_follows_partial_fill_without_consuming_quantity(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _partial_then_through()
    identity = {
        key: transitions[0][key]
        for key in (
            "order_id",
            "client_order_id",
            "strategy_id",
            "side",
            "price_ticks",
            "original_quantity",
            "creation_timestamp_ns",
            "send_timestamp_ns",
            "exchange_arrival_timestamp_ns",
        )
    }
    transitions[-1] = _transition(
        identity, "P-cancel", "partially_filled", "cancelled", 10, 2, "cancelled"
    )
    fills = fills[:1]
    result = trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))
    assert len(result["fills"]) == 1
    assert result["fills"][0]["post_fill_remaining"] == 2
    assert result["summary"]["classes"]["price_through"]["quantity"] == 0
    assert result["summary"]["classes"]["price_through"]["quantity_share"] == _ratio(
        0, 1
    )
    assert result["summary"]["through_distance_ticks"] == {
        "minimum": None,
        "maximum": None,
        "unit_weighted_mean": None,
    }


def test_retrospective_cumulative_and_queue_columns_do_not_change_classification(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _partial_then_through()
    clean = trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))
    decorated = copy.deepcopy(transitions)
    for row in decorated:
        row.update(
            cumulative_filled_quantity=999,
            average_fill_price_ticks=0.0,
            queue_ahead_estimate=-999,
            external_behind_estimate=-999,
        )
    decorated_fills = copy.deepcopy(fills)
    for row in decorated_fills:
        row["queue_ahead_estimate"] = -999
    observed = trigger_tool.analyze_rows(
        events, decorated, decorated_fills, Fraction(1)
    )
    assert observed == clean


def test_multiple_exact_orders_share_one_printed_budget(
    trigger_tool: ModuleType,
) -> None:
    events, _, _ = _through_and_exact()
    first = _identity("A", price=100, quantity=1)
    second = _identity("B", price=100, quantity=1)
    transitions = [
        *_accept(first),
        *_accept(second),
        _transition(first, "A-fill", "live", "filled", 10, 0, "fill"),
        _transition(second, "B-fill", "live", "filled", 10, 0, "fill"),
    ]
    fills = [_fill(first, "F-A", 1, 10), _fill(second, "F-B", 1, 10)]
    with pytest.raises(ValueError):
        trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))


@pytest.mark.parametrize(
    "problem", ("ambiguous", "wrong_type", "wrong_side", "unknown_notification")
)
def test_trigger_association_never_guesses_or_imputes(
    trigger_tool: ModuleType, problem: str
) -> None:
    events, transitions, fills = _through_and_exact()
    if problem == "ambiguous":
        events.append(_event(10, 4, "ADD", -1, 105, 1))
    elif problem == "wrong_type":
        events[-1]["event_type"] = "CANCEL"
    elif problem == "wrong_side":
        events[-1].update(side=-1, price_ticks=104)
    else:
        fills[0]["strategy_notification_timestamp_ns"] = None
    with pytest.raises(ValueError):
        trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))


@pytest.mark.parametrize(
    "problem",
    (
        "identity",
        "fill_identity",
        "remaining",
        "previous_status",
        "extra_fill_transition",
        "duplicate_transition",
        "duplicate_fill",
    ),
)
def test_immutable_identity_and_one_for_one_fill_transition_matching_are_required(
    trigger_tool: ModuleType, problem: str
) -> None:
    events, transitions, fills = _partial_then_through()
    if problem == "identity":
        transitions[1]["client_order_id"] = "different-client"
    elif problem == "fill_identity":
        fills[0]["client_order_id"] = "different-client"
    elif problem == "remaining":
        transitions[2]["remaining_quantity"] = 1
    elif problem == "previous_status":
        transitions[3]["previous_status"] = "live"
    elif problem == "extra_fill_transition":
        extra = copy.deepcopy(transitions[2])
        extra.update(transition_id="unmatched-fill", previous_status="partially_filled")
        transitions.insert(3, extra)
    elif problem == "duplicate_transition":
        transitions[1]["transition_id"] = transitions[0]["transition_id"]
    else:
        fills[1]["fill_id"] = fills[0]["fill_id"]
    with pytest.raises(ValueError):
        trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))


def test_through_fill_must_consume_full_remaining_even_with_consistent_partial_checkpoint(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _partial_then_through()
    fills[1]["quantity"] = 1
    transitions[3].update(new_status="partially_filled", remaining_quantity=1)
    cancelled = copy.deepcopy(transitions[3])
    cancelled.update(
        transition_id="P-cancel",
        previous_status="partially_filled",
        new_status="cancelled",
        reason="cancelled",
    )
    transitions.append(cancelled)
    with pytest.raises(ValueError):
        trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))


def test_filled_status_cannot_be_nonfill_with_unchanged_positive_remaining(
    trigger_tool: ModuleType,
) -> None:
    events, transitions, fills = _through_and_exact()
    transitions[-2].update(reason="not_a_fill", remaining_quantity=3)
    # Remove that order's emitted fill to isolate the invalid terminal status,
    # rather than fail merely because a fill transition is unmatched.
    fills = fills[1:]
    with pytest.raises(ValueError):
        trigger_tool.analyze_rows(events, transitions, fills, Fraction(1))
