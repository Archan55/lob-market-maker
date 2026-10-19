"""Independent raw-event accounting and adversarial pending-intent review.

No production fold or primary study accountant is imported. The experiment is
used only to create real retained inputs; quantities below are calculated again
with integer arithmetic and compared with an independently specified formula.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from tools.pending_intent_evidence import classify
from tools.pending_intent_experiment import run_case


def _bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _audit(document: dict[str, Any], account: str) -> dict[str, Any]:
    """Review only raw order/market inputs, with separate signed accounting."""
    orders: dict[str, dict[str, int]] = {}
    command_ids: set[str] = set()
    inventory = cash = 0
    fills = []
    for sequence, event in enumerate(document["accounts"][account], 1):
        if event["kind"] == "claim":
            continue
        if event["kind"] == "command":
            identity = event["command_id"]
            if identity in command_ids:
                continue
            command_ids.add(identity)
            request = event["request"]
            payload = request["payload"]
            if request["kind"] == "new":
                assert type(payload["quantity"]) is int and payload["quantity"] > 0
                assert payload["side"] in (-1, 1)
                assert payload["price_ticks"] > 0
                identity = payload["client_order_id"]
                assert identity not in orders
                orders[identity] = dict(
                    side=payload["side"],
                    price=payload["price_ticks"],
                    original=payload["quantity"],
                    remaining=payload["quantity"],
                    filled=0,
                    cancelled=0,
                )
            else:
                assert request["kind"] == "cancel"
                assert payload["order_id"] is None
                target = orders.get(payload["client_order_id"])
                if target is not None:
                    target["cancelled"] += target["remaining"]
                    target["remaining"] = 0
            continue
        assert event["kind"] == "trade"
        assert event["side"] in (-1, 1) and event["quantity"] > 0
        budget = event["quantity"]
        for identity, order in orders.items():
            if order["side"] != event["side"]:
                continue
            quantity = min(budget, order["remaining"])
            budget -= quantity
            order["remaining"] -= quantity
            order["filled"] += quantity
            inventory += order["side"] * quantity
            cash -= order["side"] * quantity * order["price"]
            if quantity:
                fills.append(
                    (sequence, identity, order["side"], quantity, order["price"])
                )
    for order in orders.values():
        assert order["original"] == (
            order["remaining"] + order["filled"] + order["cancelled"]
        )
    assert inventory == sum(side * quantity for _, _, side, quantity, _ in fills)
    assert cash == -sum(
        side * quantity * price for _, _, side, quantity, price in fills
    )
    live = sum(order["remaining"] for order in orders.values())
    return dict(
        inventory=inventory,
        cash_ticks=cash,
        orders=orders,
        live=live,
        interval=[
            inventory
            - sum(
                order["remaining"] for order in orders.values() if order["side"] == -1
            ),
            inventory
            + sum(
                order["remaining"] for order in orders.values() if order["side"] == 1
            ),
        ],
        command_ids=command_ids,
    )


@pytest.mark.parametrize(
    ("side", "committed", "prefill", "postfill"),
    list(itertools.product((1, -1), (False, True), (0, 2), (0, 1, 7))),
)
def test_independent_raw_quantities_and_zero_classification_liveness_gain(
    tmp_path: Path, side: int, committed: bool, prefill: int, postfill: int
) -> None:
    directory = tmp_path / "case"
    case = run_case(directory, side, committed, prefill, postfill)
    account = case["capsule"]["account_id"]
    boundary = _audit(case["raw_after_boundary"], account)
    at_cut = _audit(case["raw_at_cut"], account)
    final = _audit(case["raw_final"], account)
    target_price, sibling_price = (113, 109) if side == 1 else (127, 131)

    if committed:
        target_filled, sibling_filled, cancelled = prefill, 5, 7 - prefill
        live_boundary, live_cut, live_final = 5, max(0, 5 - postfill), 0
    else:
        target_filled, sibling_filled, cancelled = 7, min(5, prefill + postfill), 0
        live_boundary = 12 - prefill
        live_cut = max(0, 12 - prefill - postfill)
        live_final = max(0, 5 - prefill - postfill)
    assert final["inventory"] == side * (target_filled + sibling_filled)
    assert final["cash_ticks"] == -side * (
        target_filled * target_price + sibling_filled * sibling_price
    )
    assert final["orders"]["target"]["filled"] == target_filled
    assert final["orders"]["target"]["cancelled"] == cancelled
    assert final["orders"]["sibling"]["filled"] == sibling_filled
    assert final["orders"]["sibling"]["cancelled"] == 0
    assert (boundary["live"], at_cut["live"], final["live"]) == (
        live_boundary,
        live_cut,
        live_final,
    )
    assert case["executable_quantity_time_20_to_50"] == 10 * (
        live_boundary + live_cut + live_final
    )
    for raw, saved in (
        (boundary, case["after_boundary_from_raw_inputs"]),
        (at_cut, case["at_cut_from_raw_inputs"]),
        (final, case["final_from_raw_inputs"]),
    ):
        assert (raw["inventory"], raw["cash_ticks"], raw["interval"]) == (
            saved["inventory"],
            saved["cash_ticks"],
            saved["attainable_inventory_interval"],
        )
    assert case["classification"]["disposition"] == (
        "observed_exact_commit" if committed else "certified_no_commit"
    )
    assert case["classification"]["authorizes_recovery"] is False
    assert case["cancellation_liveness_improved"] is False
    assert case["entry_recovery_enabled"] is False
    assert set(case["blocked_operations_after_classification"]) == {
        "snapshot",
        "facade_trade",
        "target_cancel_retry",
        "sibling_cancel",
        "new_entry",
    }
    assert "blocked-sibling-cancel" not in final["command_ids"]
    assert "quarantined-new" not in final["command_ids"]

    inputs = [
        case[key]
        for key in ("capsule", "archive", "independently_supplied_cut", "raw_at_cut")
    ]
    original_inputs = _bytes(inputs)
    paths = (directory / "authority.sqlite", directory / "witness.sqlite")
    original_stores = [path.read_bytes() for path in paths]
    for _ in range(2):
        assert classify(*inputs) == case["classification"]
    assert _bytes(inputs) == original_inputs
    assert [path.read_bytes() for path in paths] == original_stores


@pytest.fixture
def complete_case(tmp_path: Path) -> dict[str, Any]:
    return run_case(tmp_path / "complete", 1, True, 2, 1)


@pytest.mark.parametrize(
    "fault", ("missing", "changed-effect", "live-writer", "fill-only", "wrong-head")
)
def test_independent_invalid_evidence_never_has_a_disposition(
    complete_case: dict[str, Any], fault: str
) -> None:
    capsule = copy.deepcopy(complete_case["capsule"])
    archive = copy.deepcopy(complete_case["archive"])
    cut = copy.deepcopy(complete_case["independently_supplied_cut"])
    current = copy.deepcopy(complete_case["raw_at_cut"])
    if fault == "missing":
        archive = None
    elif fault == "changed-effect":
        archive["transitions"][1]["event"]["quantity"] += 1
    elif fault == "live-writer":
        cut["pending_writer_terminal"] = False
    elif fault == "fill-only":
        cut["coverage"] = "all-fills-only"
    else:
        cut["head_digest"] = "0" * 64
    inputs = [capsule, archive, cut, current]
    before = _bytes(inputs)
    decision = classify(*inputs)
    assert decision["disposition"] == "unresolved"
    assert decision["authorizes_recovery"] is False
    assert _bytes(inputs) == before


def test_independent_same_command_at_different_prefix_is_not_exact_commit(
    complete_case: dict[str, Any],
) -> None:
    capsule = complete_case["capsule"]
    archive = copy.deepcopy(complete_case["archive"])
    history = copy.deepcopy(capsule["baseline"])
    account = capsule["account_id"]
    archive["transitions"] = []
    for sequence, event in enumerate(
        (
            dict(kind="trade", side=1, quantity=1, timestamp_ns=19),
            capsule["event"],
        ),
        1,
    ):
        before = _sha(history)
        history["accounts"][account].append(copy.deepcopy(event))
        archive["transitions"].append(
            dict(
                sequence=sequence,
                account_id=account,
                event=copy.deepcopy(event),
                before_digest=before,
                after_digest=_sha(history),
            )
        )
    cut = copy.deepcopy(complete_case["independently_supplied_cut"])
    cut.update(sequence=2, head_digest=_sha(history))
    decision = classify(capsule, archive, cut, history)
    assert decision["disposition"] == "unresolved"
    assert "different prefix" in decision["reason"]
    assert decision["authorizes_recovery"] is False


def test_independent_negative_order_quantity_cannot_validate_current_document(
    complete_case: dict[str, Any],
) -> None:
    """Pin malformed-current acceptance; an ordinary checksum cannot fix meaning."""
    current = copy.deepcopy(complete_case["raw_at_cut"])
    account = complete_case["capsule"]["account_id"]
    # The already cancelled target has two units filled. Reducing its original
    # quantity to -7 causes negative fills/cancellation under a weak accountant,
    # yet its arithmetic conservation equation still happens to balance.
    accepted = next(
        event
        for event in current["accounts"][account]
        if event["kind"] == "command" and event["command_id"] == "accept-target"
    )
    accepted["request"]["payload"]["quantity"] = -7
    decision = classify(
        complete_case["capsule"],
        complete_case["archive"],
        complete_case["independently_supplied_cut"],
        current,
    )
    assert decision["disposition"] == "unresolved", decision
    assert decision["authorizes_recovery"] is False


@pytest.mark.parametrize("field", ("side", "quantity"))
def test_independent_boolean_trade_cannot_certify_a_commit(
    complete_case: dict[str, Any], field: str
) -> None:
    """A valid hash chain cannot turn bool-valued order inputs into integers."""
    capsule = complete_case["capsule"]
    archive = copy.deepcopy(complete_case["archive"])
    history = copy.deepcopy(capsule["baseline"])
    for item in archive["transitions"]:
        if item["event"]["kind"] == "trade":
            item["event"][field] = True
        item["before_digest"] = _sha(history)
        history["accounts"][item["account_id"]].append(copy.deepcopy(item["event"]))
        item["after_digest"] = _sha(history)
    cut = copy.deepcopy(complete_case["independently_supplied_cut"])
    cut["head_digest"] = _sha(history)
    decision = classify(capsule, archive, cut, history)
    assert decision["disposition"] == "unresolved", decision
    assert decision["authorizes_recovery"] is False
