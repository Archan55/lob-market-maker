"""Construct the frozen family from manual vectors; never run a matcher.

Mirroring changes prices/sides only. Expected states are arithmetic subtraction
of declared manual fills from declared activations, not matching predictions.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def activate(order: str, quantity: int, price: int, time: int, sequence: int):
    own = order.startswith("O")
    return {
        "event_id": "A_" + order,
        "kind": "activate",
        "order_id": order,
        "owner": "own" if own else "external",
        "participant_id": "strategy" if own else "maker_" + order,
        "price_ticks": price,
        "quantity": quantity,
        "timestamp_ns": time,
        "sequence": sequence,
    }


def incoming(quantity: int, time=30, sequence=5, identifier="M1", limit=None):
    return {
        "event_id": identifier,
        "kind": "incoming",
        "aggressor_side": "sell",
        "participant_id": "aggressor",
        "quantity": quantity,
        "limit_price_ticks": limit,
        "timestamp_ns": time,
        "sequence": sequence,
    }


def standard(own_price=101):
    return [
        activate("E1", 6, 101, 0, 1),
        activate("E2", 2, 100, 0, 2),
        activate("E3", 3, 99, 0, 3),
        activate("O1", 5, own_price, 10, 4),
    ]


def build_family():
    recipes = []

    def add(name, events, baseline, counterfactual, hybrid, exposed=False):
        # Vector entries are (ordered [(maker, units), ...], IOC unfilled).
        recipes.append(
            {
                "recipe_id": name,
                "events": events,
                "manual_baseline": baseline,
                "manual_counterfactual": counterfactual,
                "manual_hybrid_errors": hybrid,
                "prior_exposed_control": exposed,
            }
        )

    full = [("E1", 6), ("E2", 2), ("E3", 3)]
    top8 = [("E1", 6), ("E2", 2)]
    own11 = [("E1", 6), ("O1", 5)]
    own8 = [("E1", 6), ("O1", 2)]
    add("known11", [*standard(), incoming(11)], [(full, 0)], [(own11, 0)], [5], True)
    add("known8", [*standard(), incoming(8)], [(top8, 0)], [(own8, 0)], [2], True)
    add(
        "boundary6",
        [*standard(), incoming(6)],
        [([("E1", 6)], 0)],
        [([("E1", 6)], 0)],
        [0],
    )
    add(
        "mixed12",
        [*standard(), incoming(12)],
        [(full, 1)],
        [([*own11, ("E2", 1)], 0)],
        [4],
    )
    add(
        "exhausted17",
        [*standard(), incoming(17)],
        [(full, 6)],
        [([*own11, ("E2", 2), ("E3", 3)], 1)],
        [0],
    )
    add(
        "better_price",
        [*standard(102), incoming(8)],
        [(top8, 0)],
        [([("O1", 5), ("E1", 3)], 0)],
        [5],
    )
    add(
        "worse_price",
        [*standard(100), incoming(11)],
        [(full, 0)],
        [([*top8, ("O1", 3)], 0)],
        [3],
    )
    split = [
        activate("E1", 4, 101, 0, 1),
        activate("E2", 2, 100, 0, 2),
        activate("E3", 3, 99, 0, 3),
        activate("O1", 5, 101, 10, 4),
        activate("E4", 2, 101, 20, 5),
        incoming(8, sequence=6),
    ]
    add(
        "split_time",
        split,
        [([("E1", 4), ("E4", 2), ("E2", 2)], 0)],
        [([("E1", 4), ("O1", 4)], 0)],
        [4],
    )
    two = [
        *standard()[:3],
        activate("Oa", 3, 101, 10, 4),
        activate("Ob", 4, 101, 20, 5),
        incoming(11, sequence=6),
    ]
    add("two_own", two, [(full, 0)], [([("E1", 6), ("Oa", 3), ("Ob", 2)], 0)], [5])
    add(
        "partial_carry",
        [*standard(), incoming(8), incoming(5, 40, 6, "M2")],
        [(top8, 0), ([("E3", 3)], 2)],
        [(own8, 0), ([("O1", 3), ("E2", 2)], 0)],
        [2, 1],
    )
    add(
        "displaced_carry",
        [*standard(), incoming(11), incoming(5, 40, 6, "M2")],
        [(full, 0), ([], 5)],
        [(own11, 0), ([("E2", 2), ("E3", 3)], 0)],
        [5, -5],
    )
    late = [*standard()[:3], incoming(16, sequence=4), activate("O1", 5, 101, 40, 5)]
    add("late_activation", late, [(full, 5)], [(full, 5)], [0])
    add(
        "eligible_limit",
        [*standard(), incoming(11, limit=100)],
        [(top8, 3)],
        [(own11, 0)],
        [2],
    )
    add(
        "excluded_limit",
        [*standard(100), incoming(11, limit=101)],
        [([("E1", 6)], 5)],
        [([("E1", 6)], 5)],
        [0],
    )
    shallow = [activate("E1", 6, 101, 0, 1), activate("O1", 5, 101, 10, 2)]
    add(
        "shallow6",
        [*shallow, incoming(6, sequence=3)],
        [([("E1", 6)], 0)],
        [([("E1", 6)], 0)],
        [0],
    )
    add(
        "shallow11",
        [*shallow, incoming(11, sequence=3)],
        [([("E1", 6)], 5)],
        [(own11, 0)],
        [0],
    )
    before = [*standard()[:3], activate("O1", 5, 101, 30, 4), incoming(8, sequence=5)]
    after = [*standard()[:3], incoming(8, sequence=4), activate("O1", 5, 101, 30, 5)]
    add("tie_before", before, [(top8, 0)], [(own8, 0)], [2])
    add("tie_after", after, [(top8, 0)], [(top8, 0)], [0])

    cases = []
    for recipe in recipes:
        for mirror in (False, True):
            events = copy.deepcopy(recipe["events"])
            if mirror:
                for event in events:
                    if event["kind"] == "activate":
                        event["price_ticks"] = 202 - event["price_ticks"]
                    else:
                        event["aggressor_side"] = "buy"
                        if event["limit_price_ticks"] is not None:
                            event["limit_price_ticks"] = (
                                202 - event["limit_price_ticks"]
                            )
            catalog = {e["order_id"]: e for e in events if e["kind"] == "activate"}
            expected = {}
            for world in ("baseline", "counterfactual"):
                trace, remaining, cursor = [], {}, 0
                totals = {
                    key: 0
                    for key in (
                        "requested_quantity",
                        "external_quantity",
                        "own_quantity",
                        "unfilled_quantity",
                    )
                }
                for event in events:
                    fills, unfilled, ext, own = [], None, 0, 0
                    if event["kind"] == "activate":
                        if world == "counterfactual" or event["owner"] == "external":
                            remaining[event["order_id"]] = event["quantity"]
                    else:
                        vector, unfilled = recipe["manual_" + world][cursor]
                        cursor += 1
                        for order_id, quantity in vector:
                            maker = catalog[order_id]
                            fills.append(
                                {
                                    "order_id": order_id,
                                    "quantity": quantity,
                                    "price_ticks": maker["price_ticks"],
                                    "owner": maker["owner"],
                                }
                            )
                            remaining[order_id] -= quantity
                            if maker["owner"] == "own":
                                own += quantity
                            else:
                                ext += quantity
                        totals["requested_quantity"] += event["quantity"]
                        totals["external_quantity"] += ext
                        totals["own_quantity"] += own
                        totals["unfilled_quantity"] += unfilled
                    trace.append(
                        {
                            "event_id": event["event_id"],
                            "kind": event["kind"],
                            "remaining_by_id": dict(remaining),
                            "fills": fills,
                            "unfilled": unfilled,
                            "external_quantity": ext,
                            "own_quantity": own,
                        }
                    )
                expected[world] = {
                    "trace": trace,
                    "final_remaining_by_id": dict(remaining),
                    "totals": totals,
                }
            cases.append(
                {
                    "case_id": recipe["recipe_id"] + ("_buy" if mirror else "_sell"),
                    "recipe_id": recipe["recipe_id"],
                    "mirrored": mirror,
                    "prior_exposed_control": recipe["prior_exposed_control"],
                    "resting_side": "ask" if mirror else "bid",
                    "events": events,
                    "expected": expected,
                    "expected_hybrid_errors": recipe["manual_hybrid_errors"],
                }
            )
    return {
        "version": 1,
        "construction": "Manual vectors plus price mirror202-p and arithmetic remaining subtraction; no matcher executed",
        "scenario_count": 36,
        "world_path_count": 72,
        "incoming_walk_count": 80,
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to overwrite frozen cases")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_family(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
