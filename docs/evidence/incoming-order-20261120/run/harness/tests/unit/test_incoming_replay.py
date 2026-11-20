"""Fixed hand and admission controls for the standalone synthetic matcher.

No production matcher/accounting helper or retained study result is imported.
The known 11-unit witness below is explicitly prior exposed, not a discovery.
The fixture lazily loads only the research tool when tests are actually run.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module")
def replay_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools/replay_incoming_orders.py"
    spec = importlib.util.spec_from_file_location("incoming_replay_unit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _activate(
    order_id: str,
    quantity: int,
    price: int,
    timestamp: int,
    sequence: int,
    owner: str = "external",
) -> dict[str, Any]:
    return {
        "event_id": "A_" + order_id,
        "kind": "activate",
        "order_id": order_id,
        "owner": owner,
        "participant_id": "maker_" + order_id,
        "price_ticks": price,
        "quantity": quantity,
        "timestamp_ns": timestamp,
        "sequence": sequence,
    }


def _incoming(
    quantity: int,
    timestamp: int,
    sequence: int,
    identifier: str = "M1",
    side: str = "sell",
    limit: int | None = None,
) -> dict[str, Any]:
    return {
        "event_id": identifier,
        "kind": "incoming",
        "aggressor_side": side,
        "participant_id": "aggressor",
        "quantity": quantity,
        "limit_price_ticks": limit,
        "timestamp_ns": timestamp,
        "sequence": sequence,
    }


def _case(events: list[dict[str, Any]], side: str = "bid") -> dict[str, Any]:
    return {
        "case_id": "unit_control",
        "resting_side": side,
        "events": events,
        # Matching must not consult this subtree, even when it is not an oracle.
        "expected": "not consulted by replay_world",
    }


def _fill(order_id: str, quantity: int, price: int, owner: str) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "quantity": quantity,
        "price_ticks": price,
        "owner": owner,
    }


def test_prior_exposed_witness_uses_separate_worlds_and_ignores_expected(
    replay_tool: ModuleType,
) -> None:
    case = _case(
        [
            _activate("E1", 6, 101, 0, 1),
            _activate("E2", 2, 100, 0, 2),
            _activate("E3", 3, 99, 0, 3),
            _activate("O1", 5, 101, 10, 4, "own"),
            _incoming(11, 30, 5),
        ]
    )
    original = copy.deepcopy(case)
    assert replay_tool.validate_case(case) is None
    baseline = replay_tool.replay_world(case, "baseline")
    counterfactual = replay_tool.replay_world(case, "counterfactual")
    assert case == original
    assert baseline["trace"][-1]["fills"] == [
        _fill("E1", 6, 101, "external"),
        _fill("E2", 2, 100, "external"),
        _fill("E3", 3, 99, "external"),
    ]
    assert counterfactual["trace"][-1]["fills"] == [
        _fill("E1", 6, 101, "external"),
        _fill("O1", 5, 101, "own"),
    ]
    assert baseline["totals"] == {
        "requested_quantity": 11,
        "external_quantity": 11,
        "own_quantity": 0,
        "unfilled_quantity": 0,
    }
    assert counterfactual["totals"] == {
        "requested_quantity": 11,
        "external_quantity": 6,
        "own_quantity": 5,
        "unfilled_quantity": 0,
    }
    assert baseline["final_remaining_by_id"] == {"E1": 0, "E2": 0, "E3": 0}
    assert counterfactual["final_remaining_by_id"] == {
        "E1": 0,
        "E2": 2,
        "E3": 3,
        "O1": 0,
    }
    # An ignored baseline activation is still a chronological trace row.
    assert baseline["trace"][3] == {
        "event_id": "A_O1",
        "kind": "activate",
        "remaining_by_id": {"E1": 6, "E2": 2, "E3": 3},
        "fills": [],
        "unfilled": None,
        "external_quantity": 0,
        "own_quantity": 0,
    }
    assert counterfactual["trace"][0]["remaining_by_id"] == {"E1": 6}
    counterfactual["trace"][0]["remaining_by_id"]["E1"] = 999
    assert counterfactual["trace"][1]["remaining_by_id"]["E1"] == 6
    assert baseline["trace"][0]["remaining_by_id"]["E1"] == 6
    assert case == original


def test_price_priority_then_time_and_partial_order_retains_priority(
    replay_tool: ModuleType,
) -> None:
    case = _case(
        [
            _activate("E1", 2, 100, 0, 1),
            _activate("O1", 3, 101, 10, 2, "own"),
            _activate("E2", 2, 101, 20, 3),
            _incoming(4, 30, 4),
            _activate("E3", 2, 101, 40, 5),
            _incoming(2, 50, 6, "M2"),
        ]
    )
    result = replay_tool.replay_world(case, "counterfactual")
    assert result["trace"][3]["fills"] == [
        _fill("O1", 3, 101, "own"),
        _fill("E2", 1, 101, "external"),
    ]
    assert result["trace"][5]["fills"] == [
        _fill("E2", 1, 101, "external"),
        _fill("E3", 1, 101, "external"),
    ]
    assert result["final_remaining_by_id"] == {
        "E1": 2,
        "O1": 0,
        "E2": 0,
        "E3": 1,
    }
    assert result["totals"] == {
        "requested_quantity": 6,
        "external_quantity": 3,
        "own_quantity": 3,
        "unfilled_quantity": 0,
    }


def test_multiple_own_orders_share_one_incoming_budget(replay_tool: ModuleType) -> None:
    case = _case(
        [
            _activate("E1", 1, 100, 0, 1),
            _activate("O1", 2, 100, 1, 2, "own"),
            _activate("O2", 2, 100, 2, 3, "own"),
            _incoming(4, 3, 4),
        ]
    )
    result = replay_tool.replay_world(case, "counterfactual")
    assert result["trace"][-1]["fills"] == [
        _fill("E1", 1, 100, "external"),
        _fill("O1", 2, 100, "own"),
        _fill("O2", 1, 100, "own"),
    ]
    assert result["final_remaining_by_id"] == {"E1": 0, "O1": 0, "O2": 1}
    assert result["totals"]["requested_quantity"] == 4
    assert result["totals"]["external_quantity"] == 1
    assert result["totals"]["own_quantity"] == 3
    assert result["totals"]["unfilled_quantity"] == 0


def test_late_activation_cannot_resurrect_an_ioc_residual(
    replay_tool: ModuleType,
) -> None:
    case = _case(
        [
            _activate("E1", 2, 101, 0, 1),
            _incoming(5, 10, 2),
            _activate("O1", 4, 101, 20, 3, "own"),
        ]
    )
    result = replay_tool.replay_world(case, "counterfactual")
    assert result["trace"][1]["unfilled"] == 3
    assert result["trace"][2] == {
        "event_id": "A_O1",
        "kind": "activate",
        "remaining_by_id": {"E1": 0, "O1": 4},
        "fills": [],
        "unfilled": None,
        "external_quantity": 0,
        "own_quantity": 0,
    }
    assert result["totals"] == {
        "requested_quantity": 5,
        "external_quantity": 2,
        "own_quantity": 0,
        "unfilled_quantity": 3,
    }


@pytest.mark.parametrize("activation_before", (True, False))
def test_same_timestamp_uses_declared_sequence_not_timestamp_alone(
    replay_tool: ModuleType, activation_before: bool
) -> None:
    external = _activate("E1", 2, 101, 0, 1)
    if activation_before:
        events = [
            external,
            _activate("O1", 3, 101, 10, 2, "own"),
            _incoming(3, 10, 3),
        ]
    else:
        events = [
            external,
            _incoming(3, 10, 2),
            _activate("O1", 3, 101, 10, 3, "own"),
        ]
    result = replay_tool.replay_world(_case(events), "counterfactual")
    assert result["totals"]["own_quantity"] == (1 if activation_before else 0)
    assert result["totals"]["unfilled_quantity"] == (0 if activation_before else 1)
    assert result["final_remaining_by_id"]["O1"] == (2 if activation_before else 3)


@pytest.mark.parametrize(
    (
        "resting_side",
        "aggressor_side",
        "first_price",
        "second_price",
        "own_price",
        "limit",
    ),
    (("bid", "sell", 101, 100, 99, 100), ("ask", "buy", 101, 102, 103, 102)),
)
def test_explicit_limit_preserves_ineligible_depth_and_unfilled_quantity(
    replay_tool: ModuleType,
    resting_side: str,
    aggressor_side: str,
    first_price: int,
    second_price: int,
    own_price: int,
    limit: int,
) -> None:
    case = _case(
        [
            _activate("E1", 2, first_price, 0, 1),
            _activate("E2", 3, second_price, 0, 2),
            _activate("O1", 4, own_price, 1, 3, "own"),
            _incoming(6, 2, 4, side=aggressor_side, limit=limit),
        ],
        resting_side,
    )
    result = replay_tool.replay_world(case, "counterfactual")
    assert result["trace"][-1]["fills"] == [
        _fill("E1", 2, first_price, "external"),
        _fill("E2", 3, second_price, "external"),
    ]
    assert result["trace"][-1]["unfilled"] == 1
    assert result["final_remaining_by_id"] == {"E1": 0, "E2": 0, "O1": 4}


@pytest.mark.parametrize(
    "problem",
    (
        "case_id",
        "resting_side",
        "event_id",
        "order_id",
        "chronology",
        "duplicate_key",
        "negative_timestamp",
        "boolean_timestamp",
        "negative_sequence",
        "boolean_sequence",
        "zero_maker_quantity",
        "boolean_maker_quantity",
        "negative_maker_quantity",
        "zero_price",
        "boolean_price",
        "owner",
        "maker_participant",
        "aggressor_side",
        "self_trade",
        "zero_limit",
        "boolean_limit",
        "zero_incoming_quantity",
        "boolean_incoming_quantity",
        "cancel",
        "reserve",
        "funds",
    ),
)
def test_admission_rejects_invalid_or_out_of_scope_inputs(
    replay_tool: ModuleType, problem: str
) -> None:
    case = _case([_activate("E1", 2, 101, 0, 1), _incoming(1, 10, 2)])
    activation, incoming = case["events"]
    if problem == "case_id":
        case["case_id"] = ""
    elif problem == "resting_side":
        case["resting_side"] = "both"
    elif problem == "event_id":
        incoming["event_id"] = activation["event_id"]
    elif problem == "order_id":
        extra = _activate("E1", 1, 100, 20, 3)
        extra["event_id"] = "A_E1_second"
        case["events"].append(extra)
    elif problem == "chronology":
        case["events"].reverse()
    elif problem == "duplicate_key":
        incoming.update(timestamp_ns=0, sequence=1)
    elif problem == "negative_timestamp":
        activation["timestamp_ns"] = -1
    elif problem == "boolean_timestamp":
        activation["timestamp_ns"] = False
    elif problem == "negative_sequence":
        activation["sequence"] = -1
    elif problem == "boolean_sequence":
        activation["sequence"] = True
    elif problem == "zero_maker_quantity":
        activation["quantity"] = 0
    elif problem == "boolean_maker_quantity":
        activation["quantity"] = True
    elif problem == "negative_maker_quantity":
        activation["quantity"] = -1
    elif problem == "zero_price":
        activation["price_ticks"] = 0
    elif problem == "boolean_price":
        activation["price_ticks"] = True
    elif problem == "owner":
        activation["owner"] = "hidden"
    elif problem == "maker_participant":
        activation["participant_id"] = ""
    elif problem == "aggressor_side":
        incoming["aggressor_side"] = "buy"
    elif problem == "self_trade":
        incoming["participant_id"] = activation["participant_id"]
    elif problem == "zero_limit":
        incoming["limit_price_ticks"] = 0
    elif problem == "boolean_limit":
        incoming["limit_price_ticks"] = True
    elif problem == "zero_incoming_quantity":
        incoming["quantity"] = 0
    elif problem == "boolean_incoming_quantity":
        incoming["quantity"] = True
    elif problem == "cancel":
        incoming["kind"] = "cancel"
    elif problem == "reserve":
        activation["reserve_quantity"] = 1
    else:
        incoming["funds"] = 100
    with pytest.raises(ValueError):
        replay_tool.validate_case(case)


def test_admission_caps_declared_units_and_activation_count(
    replay_tool: ModuleType,
) -> None:
    too_many_units = _case([_activate("E1", 65, 101, 0, 1), _incoming(1, 1, 2)])
    with pytest.raises(ValueError):
        replay_tool.validate_case(too_many_units)
    too_many_activations = _case(
        [_activate(f"E{i}", 1, 101, i, i) for i in range(9)] + [_incoming(1, 10, 10)]
    )
    with pytest.raises(ValueError):
        replay_tool.validate_case(too_many_activations)


def test_admission_caps_incoming_count(replay_tool: ModuleType) -> None:
    case = _case(
        [_activate("E1", 1, 101, 0, 0)]
        + [_incoming(1, index, index, f"M{index}") for index in range(1, 10)]
    )
    with pytest.raises(ValueError):
        replay_tool.validate_case(case)


def test_unknown_world_is_rejected(replay_tool: ModuleType) -> None:
    case = _case([_activate("E1", 2, 101, 0, 1), _incoming(1, 10, 2)])
    with pytest.raises(ValueError):
        replay_tool.replay_world(case, "combined")
