"""Independent displayed-unit reference for the frozen incoming-order family.

The matching operator receives only event inputs, never expected outcomes.
Run only after the parent's explicit execution GO; importing authors no trials.
No application or primary matcher helper is imported.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any

CASE_PATH = Path("docs/evidence/incoming-order-20261120/cases.json")
CASE_SHA = "23b6f928ae94609decfceb86e1c435f8806bb05fb21fe2eb8baf8e9b1efb1b2e"
DESIGN_PATH = Path("docs/INCOMING_REPLAY_DESIGN.md")
WALL_SECONDS = 120
EVIDENCE_BYTES = 16 * 1024**2
RECIPES = (
    "known11",
    "known8",
    "boundary6",
    "mixed12",
    "exhausted17",
    "better_price",
    "worse_price",
    "split_time",
    "two_own",
    "partial_carry",
    "displaced_carry",
    "late_activation",
    "eligible_limit",
    "excluded_limit",
    "shallow6",
    "shallow11",
    "tie_before",
    "tie_after",
)
STARTED_NS = 0


def demand(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def clock_check() -> None:
    demand(
        time.monotonic_ns() - STARTED_NS <= WALL_SECONDS * 10**9,
        "independent reference wall-time cap",
    )


def nonempty(value: Any) -> bool:
    return type(value) is str and bool(value)


def positive(value: Any) -> bool:
    return type(value) is int and value > 0


def validate_inputs(events: Any, resting_side: Any) -> None:
    demand(resting_side in ("bid", "ask"), "invalid resting side")
    demand(type(events) is list and bool(events), "missing ordered events")
    event_ids, maker_ids, maker_participants, aggressors = set(), set(), set(), set()
    previous = (-1, -1)
    placed_units = activations = incoming_count = 0
    for event in events:
        demand(type(event) is dict, "event is not an object")
        common = {
            "event_id",
            "kind",
            "timestamp_ns",
            "sequence",
            "quantity",
            "participant_id",
        }
        kind = event.get("kind")
        required = (
            (common | {"order_id", "owner", "price_ticks"})
            if kind == "activate"
            else (common | {"aggressor_side", "limit_price_ticks"})
        )
        demand(
            kind in ("activate", "incoming") and set(event) == required,
            "unsupported event kind or fields: hidden/funds/cancels are not modeled",
        )
        demand(
            nonempty(event["event_id"]) and event["event_id"] not in event_ids,
            "duplicate or empty event ID",
        )
        event_ids.add(event["event_id"])
        demand(
            type(event["timestamp_ns"]) is int
            and event["timestamp_ns"] >= 0
            and type(event["sequence"]) is int
            and event["sequence"] >= 0,
            "invalid chronology scalars",
        )
        key = (event["timestamp_ns"], event["sequence"])
        demand(key > previous, "event (timestamp,sequence) order is not strict")
        previous = key
        demand(
            positive(event["quantity"]) and nonempty(event["participant_id"]),
            "invalid quantity or participant",
        )
        if kind == "activate":
            activations += 1
            placed_units += event["quantity"]
            demand(
                nonempty(event["order_id"]) and event["order_id"] not in maker_ids,
                "duplicate or empty maker ID",
            )
            maker_ids.add(event["order_id"])
            maker_participants.add(event["participant_id"])
            demand(
                event["owner"] in ("external", "own")
                and positive(event["price_ticks"]),
                "invalid displayed maker owner/price",
            )
        else:
            incoming_count += 1
            aggressors.add(event["participant_id"])
            demand(
                event["aggressor_side"] == ("sell" if resting_side == "bid" else "buy"),
                "aggressor does not consume the declared resting side",
            )
            demand(
                event["limit_price_ticks"] is None
                or positive(event["limit_price_ticks"]),
                "invalid hard limit",
            )
    demand(
        activations <= 8 and incoming_count <= 8 and placed_units <= 64,
        "frozen per-case event/unit caps",
    )
    demand(
        not (maker_participants & aggressors), "self-trading participants are excluded"
    )


@dataclass(frozen=True)
class Unit:
    order_id: str
    number: int
    owner: str
    price: int
    entered_at: tuple[int, int]


def replay_units(
    events: list[dict[str, Any]],
    resting_side: str,
    include_own: bool,
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Atom expansion + eligible prefix removal, independent of aggregate matching."""
    live: list[Unit] = []
    entered: dict[str, int] = {}
    spent: Counter[str] = Counter()
    spent_atoms: set[tuple[str, int]] = set()
    trace = []
    totals = {
        name: 0
        for name in (
            "requested_quantity",
            "external_quantity",
            "own_quantity",
            "unfilled_quantity",
        )
    }
    for index, event in enumerate(events):
        clock_check()
        fills, unfilled, ext, own = [], None, 0, 0
        if event["kind"] == "activate":
            if include_own or event["owner"] == "external":
                entered[event["order_id"]] = event["quantity"]
                live.extend(
                    Unit(
                        event["order_id"],
                        ordinal,
                        event["owner"],
                        event["price_ticks"],
                        (event["timestamp_ns"], event["sequence"]),
                    )
                    for ordinal in range(event["quantity"])
                )
        else:
            limit = event["limit_price_ticks"]
            eligible = [
                unit
                for unit in live
                if limit is None
                or (
                    unit.price >= limit
                    if resting_side == "bid"
                    else unit.price <= limit
                )
            ]
            direction = -1 if resting_side == "bid" else 1
            eligible.sort(
                key=lambda unit: (direction * unit.price, *unit.entered_at, unit.number)
            )
            selected = eligible[: min(event["quantity"], len(eligible))]
            chosen = {(unit.order_id, unit.number) for unit in selected}
            demand(
                len(chosen) == len(selected) and not (chosen & spent_atoms),
                "a displayed unit was spent more than once",
            )
            spent_atoms.update(chosen)
            live = [unit for unit in live if (unit.order_id, unit.number) not in chosen]
            for order_id, group in groupby(selected, key=lambda unit: unit.order_id):
                units = list(group)
                first = units[0]
                demand(
                    all(
                        unit.price == first.price and unit.owner == first.owner
                        for unit in units
                    ),
                    "one maker has inconsistent token identity",
                )
                quantity = len(units)
                fills.append(
                    {
                        "order_id": order_id,
                        "quantity": quantity,
                        "price_ticks": first.price,
                        "owner": first.owner,
                    }
                )
                spent[order_id] += quantity
                if first.owner == "own":
                    own += quantity
                else:
                    ext += quantity
            unfilled = event["quantity"] - len(selected)
            demand(
                event["quantity"] == ext + own + unfilled and unfilled >= 0,
                "incoming budget is not conserved",
            )
            demand(include_own or own == 0, "baseline executed own liquidity")
            totals["requested_quantity"] += event["quantity"]
            totals["external_quantity"] += ext
            totals["own_quantity"] += own
            totals["unfilled_quantity"] += unfilled
        counts = Counter(unit.order_id for unit in live)
        remaining = {order_id: counts[order_id] for order_id in entered}
        demand(
            all(
                entered[order_id] == spent[order_id] + remaining[order_id]
                for order_id in entered
            ),
            "maker quantity is not conserved",
        )
        trace.append(
            {
                "event_id": event["event_id"],
                "kind": event["kind"],
                "remaining_by_id": remaining,
                "fills": fills,
                "unfilled": unfilled,
                "external_quantity": ext,
                "own_quantity": own,
            }
        )
        progress.update(
            completed_events=index + 1,
            event_id=event["event_id"],
            remaining_by_id=remaining,
            totals=dict(totals),
        )
    demand(
        totals["requested_quantity"]
        == totals["external_quantity"]
        + totals["own_quantity"]
        + totals["unfilled_quantity"],
        "world total budget is not conserved",
    )
    return {"trace": trace, "final_remaining_by_id": dict(remaining), "totals": totals}


def differences(expected: Any, actual: Any, path: str = "") -> list[dict[str, Any]]:
    if type(expected) is not type(actual):
        return [{"path": path, "expected": expected, "actual": actual}]
    if isinstance(expected, dict):
        result = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                result.append(
                    {
                        "path": path + "." + key,
                        "expected": expected.get(key, "<missing>"),
                        "actual": actual.get(key, "<missing>"),
                    }
                )
            else:
                result.extend(differences(expected[key], actual[key], path + "." + key))
        return result
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [
                {
                    "path": path + ".length",
                    "expected": len(expected),
                    "actual": len(actual),
                }
            ]
        return [
            item
            for i, (left, right) in enumerate(zip(expected, actual, strict=True))
            for item in differences(left, right, f"{path}[{i}]")
        ]
    return (
        []
        if expected == actual
        else [{"path": path, "expected": expected, "actual": actual}]
    )


def derive_case(case: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    # Only input fields enter replay_units. The oracle is accessed afterward.
    events, resting = case["events"], case["resting_side"]
    validate_inputs(events, resting)
    before = object_hash(events)
    actual = {}
    for world in ("baseline", "counterfactual"):
        progress.update(world=world, completed_events=0)
        actual[world] = replay_units(
            events, resting, world == "counterfactual", progress
        )
        demand(object_hash(events) == before, "reference mutated event input")
    baseline = [row for row in actual["baseline"]["trace"] if row["kind"] == "incoming"]
    counterfactual = [
        row for row in actual["counterfactual"]["trace"] if row["kind"] == "incoming"
    ]
    orders = [event for event in events if event["kind"] == "incoming"]
    paired = []
    for incoming, left, right in zip(orders, baseline, counterfactual, strict=True):
        demand(
            left["event_id"] == right["event_id"] == incoming["event_id"],
            "world incoming streams differ",
        )
        displaced = left["external_quantity"] - right["external_quantity"]
        residual_reduction = left["unfilled"] - right["unfilled"]
        own = right["own_quantity"]
        demand(own == displaced + residual_reduction, "paired signed identity failed")
        hybrid_error = (
            left["external_quantity"] + own + right["unfilled"] - incoming["quantity"]
        )
        demand(hybrid_error == displaced, "hybrid signed error identity failed")
        paired.append(
            {
                "event_id": incoming["event_id"],
                "requested_quantity": incoming["quantity"],
                "baseline_external": left["external_quantity"],
                "counterfactual_external": right["external_quantity"],
                "counterfactual_own": own,
                "baseline_unfilled": left["unfilled"],
                "counterfactual_unfilled": right["unfilled"],
                "external_displacement": displaced,
                "unfilled_reduction": residual_reduction,
                "hybrid_budget_error": hybrid_error,
            }
        )
    errors = [row["hybrid_budget_error"] for row in paired]
    mismatch = differences(case["expected"], actual, "worlds")
    mismatch.extend(
        differences(case["expected_hybrid_errors"], errors, "hybrid_errors")
    )
    return {
        "case_id": case["case_id"],
        "recipe_id": case["recipe_id"],
        "mirrored": case["mirrored"],
        "prior_exposed_control": case["prior_exposed_control"],
        "worlds": actual,
        "paired": paired,
        "actual_worlds_sha256": object_hash(actual),
        "manual_expected_sha256": object_hash(case["expected"]),
        "hybrid_errors": errors,
        "mismatch_count": len(mismatch),
        "mismatches": mismatch,
    }


def first_incoming(result: dict[str, Any], world: str) -> dict[str, Any]:
    return next(
        row for row in result["worlds"][world]["trace"] if row["kind"] == "incoming"
    )


def structural_checks(results: list[dict[str, Any]]) -> dict[str, Any]:
    keyed = {(row["recipe_id"], row["mirrored"]): row for row in results}
    witnesses = {}
    for mirror in (False, True):
        shallow6, shallow11 = keyed[("shallow6", mirror)], keyed[("shallow11", mirror)]
        known8, split = keyed[("known8", mirror)], keyed[("split_time", mirror)]
        tie_before, tie_after = (
            keyed[("tie_before", mirror)],
            keyed[("tie_after", mirror)],
        )
        carry, late = (
            keyed[("displaced_carry", mirror)],
            keyed[("late_activation", mirror)],
        )
        values = {
            "shallow_baseline_prints_identical": first_incoming(shallow6, "baseline")[
                "fills"
            ]
            == first_incoming(shallow11, "baseline")["fills"],
            "shallow_cf_own_0_5": [
                first_incoming(x, "counterfactual")["own_quantity"]
                for x in (shallow6, shallow11)
            ]
            == [0, 5],
            "known8_split_own_2_4": [
                first_incoming(x, "counterfactual")["own_quantity"]
                for x in (known8, split)
            ]
            == [2, 4],
            "tie_sequence_own_2_0": [
                first_incoming(x, "counterfactual")["own_quantity"]
                for x in (tie_before, tie_after)
            ]
            == [2, 0],
            "carry_hybrid_plus5_minus5_terminalzero": carry["hybrid_errors"] == [5, -5]
            and sum(carry["hybrid_errors"]) == 0,
            "carry_second_external_displacement_negative": carry["paired"][1][
                "external_displacement"
            ]
            == -5,
            "late_ioc_not_resurrected": first_incoming(late, "counterfactual")[
                "unfilled"
            ]
            == 5
            and late["worlds"]["counterfactual"]["final_remaining_by_id"]["O1"] == 5,
        }
        demand(all(values.values()), "predefined structural witness failed")
        witnesses["buy_mirror" if mirror else "sell_base"] = values
    for recipe in RECIPES:
        left, right = keyed[(recipe, False)], keyed[(recipe, True)]
        transformed = copy.deepcopy(left["worlds"])
        for world in transformed.values():
            for row in world["trace"]:
                for fill in row["fills"]:
                    fill["price_ticks"] = 202 - fill["price_ticks"]
        demand(
            transformed == right["worlds"]
            and left["hybrid_errors"] == right["hybrid_errors"],
            "actual quantity/price mirror symmetry failed",
        )
    return witnesses


def main() -> int:
    global STARTED_NS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    STARTED_NS = time.monotonic_ns()
    guard = sys.get_int_max_str_digits()
    root = args.root.resolve()
    result: dict[str, Any] = {
        "status": "started",
        "reference_sha256": digest(Path(__file__).resolve()),
        "cases_sha256": None,
        "design_sha256": None,
        "cases": [],
        "progress": {},
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "isolated": bool(sys.flags.isolated),
            "dont_write_bytecode": sys.dont_write_bytecode,
        },
    }
    try:
        demand(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application imported before reference",
        )
        demand(
            sys.flags.isolated and sys.dont_write_bytecode,
            "invoke reference using -I -B",
        )
        case_path = root / CASE_PATH
        result["cases_sha256"] = digest(case_path)
        result["design_sha256"] = digest(root / DESIGN_PATH)
        demand(result["cases_sha256"] == CASE_SHA, "frozen case bytes changed")
        family = json.loads(case_path.read_text())
        cases = family["cases"]
        demand(
            type(cases) is list and len(cases) == 36 <= 64, "fixed family size differs"
        )
        demand(
            family["scenario_count"] == 36
            and family["world_path_count"] == 72
            and family["incoming_walk_count"] == 80,
            "frozen family count metadata differs",
        )
        demand(
            {(case["recipe_id"], case["mirrored"]) for case in cases}
            == {(name, mirror) for name in RECIPES for mirror in (False, True)},
            "recipe/mirror inventory differs",
        )
        demand(
            len({case["case_id"] for case in cases}) == len(cases), "duplicate case IDs"
        )
        for case in cases:
            clock_check()
            result["progress"] = {
                "case_id": case["case_id"],
                "world": None,
                "completed_events": 0,
            }
            derived = derive_case(case, result["progress"])
            result["cases"].append(derived)
            demand(
                len(canonical(result)) <= EVIDENCE_BYTES - 65536, "reference output cap"
            )
            demand(
                derived["mismatch_count"] == 0,
                "reference differs from frozen manual oracle",
            )
        result["structural_witnesses"] = structural_checks(result["cases"])
        result["case_count"] = len(result["cases"])
        result["world_path_count"] = 2 * len(result["cases"])
        result["incoming_walk_count"] = sum(
            2 * len(row["paired"]) for row in result["cases"]
        )
        demand(
            result["incoming_walk_count"] == 80, "actual incoming walk count differs"
        )
        demand(sys.get_int_max_str_digits() == guard, "integer string guard changed")
        demand(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application imported after reference",
        )
        demand(digest(case_path) == CASE_SHA, "case bytes changed during reference")
        clock_check()
        result.update(
            status="complete",
            mismatch_count=0,
            untested_suffix=None,
            integer_guard_unchanged=True,
            no_application_imports=True,
            scope="Same frozen constructed family, unit-token mechanics corroboration only; no data calibration, production replacement, economics or new independent samples",
        )
    except Exception as exc:
        result.update(
            status="stopped",
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
            untested_suffix="Interrupted current-case suffix and remaining scenarios are not accepted or substituted",
        )
    result["elapsed_ns"] = time.monotonic_ns() - STARTED_NS
    encoded = canonical(result) + b"\n"
    if len(encoded) > EVIDENCE_BYTES:
        result = {
            "status": "stopped",
            "stop_reason": "reference output cap exceeded",
            "reference_sha256": result["reference_sha256"],
            "cases_sha256": result["cases_sha256"],
            "completed_case_ids": [row["case_id"] for row in result["cases"]],
            "progress": result["progress"],
            "full_payload_not_retained": True,
        }
        encoded = canonical(result) + b"\n"
    sys.stdout.buffer.write(encoded)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
