"""Frozen positive-price policy oracle and public quote-manager probes.

Importing this module evaluates no fixture, strategy or oracle. Explicit probe
calls are for the preregistered archive workers. Rational arithmetic is an
independent oracle for these finite, small, empty-volatility-history inputs;
it does not call production pricing, sizing or quote-management helpers.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from lobmm.config import StrategyConfig
from lobmm.enums import EventType, OrderStatus, ReportType, Side, StrategyName
from lobmm.events import BookView, EventValidationError, MarketEvent
from lobmm.orders import (
    CancelRequest,
    ExecutionReport,
    NewOrderRequest,
    OrderError,
)
from lobmm.strategies import make_strategy
from lobmm.strategies.base import DesiredQuote

FIXTURES_SHA256 = "96c30af69e182c3218c9f892ac5276f773b3a10393adf0782441869470cf727c"
CASE_NAMES = (
    "flat_low",
    "positive_floor",
    "ordinary_flat",
    "low_long_half",
    "low_short_half",
    "low_long_cap",
    "low_short_cap",
    "nonpositive_ask",
    "minimum_half_spread",
)
POLICIES = ("fixed_spread", "inventory_aware", "microprice")
POST_ONLY_MODES = (True, False)


def _document(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(fixtures_path)
        if fixtures_path is not None
        else Path(__file__).resolve().parents[2]
        / "docs/evidence/positive-price-20261109/fixtures.json"
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURES_SHA256:
        raise ValueError("frozen positive-price fixture hash changed")
    document = json.loads(path.read_text())
    if (
        tuple(document["pure_cases"]) != CASE_NAMES
        or tuple(document["policies"]) != POLICIES
        or tuple(document["post_only_modes"]) != POST_ONLY_MODES
    ):
        raise ValueError("frozen positive-price case ledger changed")
    return document


def load_cases(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    """Read and copy the nine unevaluated, hash-pinned pure specifications."""
    return deepcopy(_document(fixtures_path)["pure_cases"])


def _view(
    specification: dict[str, Any], *, timestamp_ns: int, sequence_number: int
) -> BookView:
    bid, ask = specification["bid"], specification["ask"]
    bid_quantity = specification["bid_quantity"]
    ask_quantity = specification["ask_quantity"]
    depth = bid_quantity + ask_quantity
    return BookView(
        timestamp_ns=timestamp_ns,
        sequence_number=sequence_number,
        best_bid_ticks=bid,
        best_ask_ticks=ask,
        best_bid_quantity=bid_quantity,
        best_ask_quantity=ask_quantity,
        midpoint_ticks=float(Fraction(bid + ask, 2)),
        microprice_ticks=float(
            Fraction(ask * bid_quantity + bid * ask_quantity, depth)
        ),
        imbalance=float(Fraction(bid_quantity - ask_quantity, depth)),
        bids=((bid, bid_quantity),),
        asks=((ask, ask_quantity),),
    )


def build_policy_case(
    name: str,
    policy: str | StrategyName,
    post_only: bool,
    fixtures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Explicitly construct config/view inputs, without evaluating a strategy."""
    document = _document(fixtures_path)
    policy = StrategyName(policy).value
    if policy not in document["policies"] or type(post_only) is not bool:
        raise ValueError("policy/mode is outside the frozen case ledger")
    spec = deepcopy(document["pure_cases"][name])
    config = StrategyConfig(
        name=StrategyName(policy),
        post_only=post_only,
        **spec.get("strategy_overrides", {}),
    )
    return {
        "name": name,
        "policy": policy,
        "post_only": post_only,
        "config": config,
        "view": _view(
            spec,
            timestamp_ns=spec["timestamp_ns"],
            sequence_number=spec["sequence_number"],
        ),
        "inventory": spec["inventory"],
        "max_abs_inventory": spec["max_abs_inventory"],
    }


def _fraction(value: int | float) -> Fraction:
    # Frozen parameters are integer or binary-exact halves; decimal spelling
    # avoids repeating the production float operations in the scalar oracle.
    return Fraction(str(value))


def oracle_quotes(
    *,
    config: StrategyConfig,
    view: BookView,
    known_inventory: int,
    max_abs_inventory: int,
) -> tuple[tuple[int, int, int], ...]:
    """Original small-fixture formulas, inventory permissions, then price > 0.

    The oracle deliberately retains positive targets that cross observed BBO.
    These cases have no prior volatility observations. Policy inputs and this
    oracle's exact rational assumptions are frozen before their evaluation.
    """
    assert view.midpoint_ticks is not None
    normalized = (
        Fraction(known_inventory, max_abs_inventory)
        if max_abs_inventory
        else Fraction(0)
    )
    half = _fraction(config.base_half_spread_ticks)
    reservation = _fraction(view.midpoint_ticks)
    quantity = config.order_size
    if config.name is not StrategyName.FIXED_SPREAD:
        if config.name is StrategyName.MICROPRICE:
            reservation = _fraction(
                view.microprice_ticks
                if view.microprice_ticks is not None
                else view.midpoint_ticks
            ) + _fraction(config.imbalance_coefficient_ticks) * _fraction(
                view.imbalance if view.imbalance is not None else 0
            )
            half = max(half, _fraction(config.minimum_half_spread_ticks))
        elif config.name is not StrategyName.INVENTORY_AWARE:
            raise ValueError("unsupported policy in independent oracle")
        reservation -= _fraction(config.inventory_penalty_ticks) * normalized
        if max_abs_inventory > 0:
            fraction = min(
                Fraction(1), Fraction(abs(known_inventory), max_abs_inventory)
            )
            scaled = quantity * (1 - Fraction(3, 4) * fraction)
            quantity = max(1, scaled.numerator // scaled.denominator)

    lower, upper = reservation - half, reservation + half
    bid = lower.numerator // lower.denominator
    ask = -((-upper.numerator) // upper.denominator)
    if bid >= ask:
        ask = bid + 1
    result: list[tuple[int, int, int]] = []
    if known_inventory < max_abs_inventory and bid > 0:
        result.append((int(Side.BID), bid, quantity))
    if known_inventory > -max_abs_inventory and ask > 0:
        result.append((int(Side.ASK), ask, quantity))
    return tuple(result)


def _quote_rows(quotes: Any) -> list[list[int]]:
    return [[int(q.side), q.price_ticks, q.quantity] for q in quotes]


def _plain(value: Any) -> Any:
    if isinstance(value, Side):
        return int(value)
    if isinstance(value, (OrderStatus, ReportType, StrategyName)):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _error(error: Exception, stage: str) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error), "stage": stage}


def _policy_probe(case: dict[str, Any]) -> dict[str, Any]:
    config, view = case["config"], case["view"]
    expected = oracle_quotes(
        config=config,
        view=view,
        known_inventory=case["inventory"],
        max_abs_inventory=case["max_abs_inventory"],
    )
    record: dict[str, Any] = {
        "name": case["name"],
        "policy": case["policy"],
        "post_only": case["post_only"],
        "config": config.model_dump(mode="json"),
        "view": _plain(asdict(view)),
        "inventory": case["inventory"],
        "max_abs_inventory": case["max_abs_inventory"],
        "oracle": _plain(expected),
        "actual": None,
        "error": None,
    }
    strategy = make_strategy(config)
    try:
        record["actual"] = _quote_rows(
            strategy.desired_quotes(
                view=view,
                known_inventory=case["inventory"],
                max_abs_inventory=case["max_abs_inventory"],
            )
        )
    except ValueError as exc:
        record["error"] = _error(exc, "desired_quotes")
    record["checks"] = {
        "no_error": record["error"] is None,
        "matches_oracle": record["actual"] == record["oracle"],
        "all_prices_positive": record["actual"] is not None
        and all(row[1] > 0 for row in record["actual"]),
    }
    return record


def _active_rows(strategy: Any) -> list[dict[str, Any]]:
    return [_plain(asdict(quote)) for quote in strategy.active_quotes]


def _action_rows(actions: Any) -> list[dict[str, Any]]:
    return [
        {"type": type(action).__name__, **_plain(asdict(action))} for action in actions
    ]


def managed_probe(
    policy: str | StrategyName,
    post_only: bool,
    fixtures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Public initial submit/ack, omitted-side cancellation and pending repeat.

    No cancel acknowledgement is invented. Both managed quotes must remain
    present after the cancel is sent, with only the bid pending cancellation.
    """
    doc = _document(fixtures_path)
    spec = deepcopy(doc["managed_case"])
    policy = StrategyName(policy).value
    if policy not in doc["policies"] or type(post_only) is not bool:
        raise ValueError("policy/mode is outside the frozen case ledger")
    config = StrategyConfig(
        name=StrategyName(policy),
        post_only=post_only,
        **spec["config_overrides"],
    )
    strategy = make_strategy(config)
    initial_view = _view(
        spec["initial_book"],
        timestamp_ns=spec["initial_decision_ns"],
        sequence_number=1,
    )
    low_view = _view(
        spec["low_book"],
        timestamp_ns=spec["low_decision_ns"],
        sequence_number=2,
    )
    oracle_initial = oracle_quotes(
        config=config,
        view=initial_view,
        known_inventory=0,
        max_abs_inventory=spec["max_abs_inventory"],
    )
    oracle_low = oracle_quotes(
        config=config,
        view=low_view,
        known_inventory=0,
        max_abs_inventory=spec["max_abs_inventory"],
    )
    strategy.on_market_data(initial_view)
    initial = strategy.decide(
        timestamp_ns=spec["initial_decision_ns"],
        max_abs_inventory=spec["max_abs_inventory"],
    )
    if not all(isinstance(action, NewOrderRequest) for action in initial):
        raise AssertionError("initial public decision did not contain new orders")
    for number, action in enumerate(initial, 1):
        strategy.record_action_sent(action, timestamp_ns=spec["initial_decision_ns"])
        strategy.on_execution_report(
            ExecutionReport(
                report_id=f"independent-accepted-{number}",
                report_type=ReportType.ACCEPTED,
                exchange_timestamp_ns=spec["accepted_exchange_ns"],
                order_id=f"independent-order-{int(action.side)}",
                client_order_id=action.client_order_id,
                order_status=OrderStatus.LIVE,
                remaining_quantity=action.quantity,
            ),
            notification_timestamp_ns=spec["accepted_notification_ns"],
        )
    accepted = _active_rows(strategy)
    accepted_by_side = {quote["side"]: quote for quote in accepted}
    record: dict[str, Any] = {
        "policy": policy,
        "post_only": post_only,
        "config": config.model_dump(mode="json"),
        "initial_view": _plain(asdict(initial_view)),
        "low_view": _plain(asdict(low_view)),
        "oracle_initial": _plain(oracle_initial),
        "oracle_low": _plain(oracle_low),
        "initial_actions": _action_rows(initial),
        "accepted_quotes": accepted,
        "low_actions": None,
        "repeat_actions": None,
        "after_low_quotes": None,
        "after_repeat_quotes": None,
        "decisions": None,
        "error": None,
    }
    strategy.on_market_data(low_view)
    try:
        low = strategy.decide(
            timestamp_ns=spec["low_decision_ns"],
            max_abs_inventory=spec["max_abs_inventory"],
        )
    except ValueError as exc:
        record["error"] = _error(exc, "low_decide")
        record["after_low_quotes"] = _active_rows(strategy)
        record["decisions"] = [_plain(asdict(d)) for d in strategy.decisions]
        record["checks"] = {
            "initial_quotes_exact": _quote_rows(initial) == _plain(oracle_initial),
            "no_error": False,
            "one_original_bid_cancel": False,
            "ask_unchanged": False,
            "repeat_idempotent": False,
        }
        return record
    for action in low:
        strategy.record_action_sent(action, timestamp_ns=spec["low_decision_ns"])
    after_low = _active_rows(strategy)
    after_by_side = {quote["side"]: quote for quote in after_low}
    decisions_before_repeat = [_plain(asdict(d)) for d in strategy.decisions]
    repeat = strategy.decide(
        timestamp_ns=spec["repeat_decision_ns"],
        max_abs_inventory=spec["max_abs_inventory"],
    )
    after_repeat = _active_rows(strategy)
    record.update(
        low_actions=_action_rows(low),
        repeat_actions=_action_rows(repeat),
        after_low_quotes=after_low,
        after_repeat_quotes=after_repeat,
        decisions=[_plain(asdict(d)) for d in strategy.decisions],
    )
    original_bid = accepted_by_side[int(Side.BID)]
    original_ask = accepted_by_side[int(Side.ASK)]
    pending_bid = after_by_side.get(int(Side.BID), {})
    record["checks"] = {
        "initial_quotes_exact": _quote_rows(initial) == _plain(oracle_initial),
        "initial_requests_preserve_post_only": all(
            action.post_only is post_only for action in initial
        ),
        "accepted_context": len(accepted) == 2
        and all(
            q["exchange_order_id"] is not None
            and q["accepted_timestamp_ns"] == spec["accepted_notification_ns"]
            and q["dispatch_status"] == "sent"
            for q in accepted
        ),
        "surviving_target_exact": _plain(oracle_low)
        == [[int(Side.ASK), 3, config.order_size]],
        "no_error": True,
        "one_original_bid_cancel": len(low) == 1
        and isinstance(low[0], CancelRequest)
        and low[0].order_id == original_bid["exchange_order_id"]
        and low[0].client_order_id is None
        and low[0].decision_timestamp_ns == spec["low_decision_ns"]
        and low[0].send_timestamp_ns == spec["low_decision_ns"],
        "minimum_lifetime_not_elapsed": spec["low_decision_ns"]
        - spec["initial_decision_ns"]
        < config.minimum_quote_lifetime_ns,
        "both_managed_until_report": set(after_by_side)
        == {int(Side.BID), int(Side.ASK)},
        "bid_pending_not_deleted": pending_bid.get("pending_cancel") is True
        and pending_bid.get("cancel_dispatch_status") == "sent"
        and pending_bid.get("exchange_order_id") == original_bid["exchange_order_id"],
        "ask_unchanged": after_by_side.get(int(Side.ASK)) == original_ask,
        "repeat_idempotent": repeat == ()
        and after_repeat == after_low
        and record["decisions"] == decisions_before_repeat,
        "one_existing_reason": len(strategy.decisions) == 3
        and strategy.decisions[-1].action == "cancel"
        and strategy.decisions[-1].reason == "risk_side_suppression"
        and strategy.decisions[-1].side is Side.BID,
        "dispatch_counts": strategy.submitted_orders == 2
        and strategy.cancel_attempts == 1
        and strategy.cancel_requests == 1,
        "inventory_unchanged": strategy.known_inventory == 0,
    }
    return record


def validator_probes() -> list[dict[str, Any]]:
    """Fixed existing price-validator controls; no strategy or engine calls."""
    records = []
    for side in (Side.BID, Side.ASK):
        for price in (0, -1, 1):
            for kind in ("DesiredQuote", "NewOrderRequest", "MarketEvent"):
                record: dict[str, Any] = {
                    "kind": kind,
                    "side": int(side),
                    "price": price,
                    "error": None,
                }
                try:
                    if kind == "DesiredQuote":
                        DesiredQuote(side, price, 1)
                    elif kind == "NewOrderRequest":
                        NewOrderRequest(
                            client_order_id="independent-price-validator",
                            strategy_id="independent",
                            side=side,
                            price_ticks=price,
                            quantity=1,
                            decision_timestamp_ns=0,
                            send_timestamp_ns=0,
                        )
                    else:
                        MarketEvent(0, 0, EventType.ADD, side, price, 1)
                except (ValueError, OrderError, EventValidationError) as exc:
                    record["error"] = _error(exc, "constructor")
                record["check"] = (
                    record["error"] is None
                    if price > 0
                    else record["error"] is not None
                    and "positive" in record["error"]["message"]
                )
                records.append(record)
    return records


def run_policy_probes(
    fixtures_path: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Explicit archive-worker ledger; baseline construction errors are retained."""
    document = _document(fixtures_path)
    return {
        "pure": [
            _policy_probe(build_policy_case(name, policy, mode, fixtures_path))
            for name in document["pure_cases"]
            for policy in document["policies"]
            for mode in document["post_only_modes"]
        ],
        "managed": [
            managed_probe(policy, mode, fixtures_path)
            for policy in document["policies"]
            for mode in document["managed_case"]["post_only_modes"]
        ],
        "validators": validator_probes(),
    }


@pytest.mark.parametrize("name", CASE_NAMES)
@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("post_only", POST_ONLY_MODES)
def test_frozen_policy_targets_equal_original_formulas_intersect_positive_domain(
    name: str, policy: str, post_only: bool
) -> None:
    result = _policy_probe(build_policy_case(name, policy, post_only))
    assert result["error"] is None, result
    assert result["actual"] == result["oracle"], result
    assert all(result["checks"].values()), result


@pytest.mark.parametrize("policy", POLICIES)
def test_hand_oracle_keeps_price_one_and_positive_crossed_targets_without_clipping(
    policy: str,
) -> None:
    expectations = {
        "flat_low": ((-1, 3, 10),),
        "positive_floor": ((1, 1, 10), (-1, 3, 10)),
        "minimum_half_spread": ((1, 1, 10), (-1, 2, 10)),
        "low_long_cap": ((-1, 3, 10),) if policy == "fixed_spread" else ((-1, 1, 2),),
        "low_short_cap": () if policy == "fixed_spread" else ((1, 2, 2),),
        "nonpositive_ask": ((-1, 3, 10),) if policy == "fixed_spread" else (),
    }
    for name, expected in expectations.items():
        case = build_policy_case(name, policy, True)
        assert (
            oracle_quotes(
                config=case["config"],
                view=case["view"],
                known_inventory=case["inventory"],
                max_abs_inventory=case["max_abs_inventory"],
            )
            == expected
        )


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("post_only", POST_ONLY_MODES)
def test_omitted_bid_uses_existing_cancel_path_and_retains_accepted_ask(
    policy: str, post_only: bool
) -> None:
    result = managed_probe(policy, post_only)
    assert result["error"] is None, result
    assert result["checks"], result
    assert all(result["checks"].values()), result


def test_existing_price_validators_reject_nonpositive_and_keep_price_one() -> None:
    results = validator_probes()
    assert results
    assert all(row["check"] for row in results), results
