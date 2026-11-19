"""Frozen independent reference for two exposed maker-fill trigger cohorts.

No data reads, application imports, joins or classifications occur on import.
Execute only after the owner's explicit result-review GO. The reference uses
full timestamp bisects and retained per-order transition scans, not primary
analyzer helpers. Root is explicit through --root (default: cwd).
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import struct
import sys
import time
import traceback
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

RESOURCE = Path("docs/evidence/end-to-end-resources-20261117/run")
SOURCE = "0866bf13e449cd66e3862454eaaf424a5172c33a"
COHORTS = (("001", 2000, 26), ("002", 20000, 780))
COLUMNS = (
    "timestamp_ns",
    "sequence_number",
    "event_type",
    "side",
    "price_ticks",
    "quantity",
)
IDENTITY = (
    "client_order_id",
    "strategy_id",
    "side",
    "price_ticks",
    "original_quantity",
    "creation_timestamp_ns",
    "send_timestamp_ns",
    "exchange_arrival_timestamp_ns",
)
EDGES = {
    "pending_arrival": {"live", "rejected"},
    "live": {"partially_filled", "filled", "cancelled", "expired"},
    "partially_filled": {"partially_filled", "filled", "cancelled", "expired"},
    "filled": set(),
    "cancelled": set(),
    "expired": set(),
    "rejected": set(),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def ratio(numerator: int, denominator: int) -> dict[str, int] | None:
    if denominator == 0:
        return None
    value = Fraction(numerator, denominator)
    return {"numerator": value.numerator, "denominator": value.denominator}


def config_literals(path: Path) -> dict[str, str]:
    import yaml

    document = yaml.compose(path.read_text())

    def scalar(*keys: str) -> str:
        node = document
        for key in keys:
            require(
                isinstance(node, yaml.MappingNode),
                "configuration path is not a mapping",
            )
            found = [value for name, value in node.value if name.value == key]
            require(len(found) == 1, "missing or repeated configuration field")
            node = found[0]
        require(
            isinstance(node, yaml.ScalarNode),
            "configuration field is not a literal scalar",
        )
        return node.value

    require(
        scalar("queue_model", "price_through_fills").lower() == "true",
        "price-through rule is not enabled",
    )
    require(
        scalar("strategy", "post_only").lower() == "true", "strategy is not post-only"
    )
    require(
        scalar("backtest", "session_end_policy") == "mark", "session policy is not MARK"
    )
    values = {
        "tick": scalar("instrument", "tick_size"),
        "currency": scalar("instrument", "currency"),
        "maker_fee": scalar("fees", "maker_fee_per_unit"),
        "maker_rebate": scalar("fees", "maker_rebate_per_unit"),
        "taker_fee": scalar("fees", "taker_fee_per_unit"),
        "proportional": scalar("fees", "proportional_fee_rate"),
        "original_exchange_validation_mode": scalar("exchange", "validation_mode"),
    }
    require(Fraction(values["tick"]) > 0, "literal tick must be positive")
    for field in ("maker_fee", "maker_rebate", "taker_fee", "proportional"):
        require(Fraction(values[field]) >= 0, "literal cost must be nonnegative")
    return values


def strict_tape(
    table: Any, expected_count: int
) -> tuple[list[dict[str, Any]], list[int], str]:
    import pyarrow as pa

    require(
        tuple(table.schema.names) == COLUMNS
        and table.num_rows == expected_count <= 100000,
        "canonical input schema/count drift",
    )
    for field in table.schema:
        valid = (
            (pa.types.is_string(field.type) or pa.types.is_large_string(field.type))
            if field.name == "event_type"
            else pa.types.is_int8(field.type)
            if field.name == "side"
            else pa.types.is_int64(field.type)
        )
        require(valid, "canonical input target type drift: " + field.name)
    rows = table.to_pylist()
    fingerprint = hashlib.sha256()
    codes = {"ADD": 1, "CANCEL": 2, "TRADE": 3, "RESET": 4, "SNAPSHOT": 5}
    depth: dict[int, dict[int, int]] = {1: {}, -1: {}}
    previous_time, previous_sequence = -1, -1
    for index, row in enumerate(rows):
        timestamp, sequence = row["timestamp_ns"], row["sequence_number"]
        require(
            type(timestamp) is int
            and type(sequence) is int
            and timestamp >= max(0, previous_time)
            and sequence > previous_sequence,
            f"input ordering/type admission at row {index}",
        )
        event, side, price, quantity = (
            row[key] for key in ("event_type", "side", "price_ticks", "quantity")
        )
        require(type(event) is str and event in codes, "invalid canonical event type")
        require(
            type(price) is int and type(quantity) is int,
            "input integer scalar admission",
        )
        if event == "RESET":
            require(side is None and price == quantity == 0, "invalid RESET")
            depth = {1: {}, -1: {}}
        else:
            require(
                type(side) is int and side in (-1, 1) and price > 0 and quantity > 0,
                "invalid external liquidity scalar",
            )
            before = depth[side].get(price, 0)
            if event in ("ADD", "SNAPSHOT"):
                depth[side][price] = before + quantity
            else:
                require(
                    before >= quantity,
                    f"strict external reduction overdraw at row {index}",
                )
                if quantity == before:
                    del depth[side][price]
                else:
                    depth[side][price] = before - quantity
            require(
                not depth[1] or not depth[-1] or max(depth[1]) < min(depth[-1]),
                "independent external book crossed",
            )
        fingerprint.update(
            struct.pack(
                ">qqbbqq", timestamp, sequence, codes[event], side or 0, price, quantity
            )
        )
        previous_time, previous_sequence = timestamp, sequence
    return rows, [row["timestamp_ns"] for row in rows], fingerprint.hexdigest()


def transition_matches(
    orders: list[dict[str, Any]], fills: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], Counter[str]]:
    """Scan each order's retained transition stream and next retained fill."""
    require(len(orders) <= 100000 and len(fills) <= 20000, "transition/fill cap")
    transition_ids = [row["transition_id"] for row in orders]
    require(
        all(type(value) is str and value for value in transition_ids)
        and len(set(transition_ids)) == len(transition_ids),
        "transition ID admission",
    )
    require(
        all(type(row["order_id"]) is str and row["order_id"] for row in orders),
        "registry transition order IDs must be nonempty strings",
    )
    fill_ids = [row["fill_id"] for row in fills]
    require(
        all(type(value) is str and value for value in fill_ids)
        and len(set(fill_ids)) == len(fill_ids),
        "fill ID admission",
    )
    previous = -1
    for fill in fills:
        require(
            all(
                type(fill[field]) is str and fill[field]
                for field in ("order_id", "client_order_id", "strategy_id")
            ),
            "empty fill identity",
        )
        require(
            type(fill["side"]) is int and fill["side"] in (-1, 1), "fill side admission"
        )
        require(
            all(
                type(fill[field]) is int and fill[field] > 0
                for field in ("price_ticks", "quantity")
            ),
            "fill integer admission",
        )
        require(fill["liquidity_role"] == "maker", "non-maker fill in fixed cohort")
        timestamp = fill["exchange_fill_timestamp_ns"]
        require(
            type(timestamp) is int and timestamp >= max(0, previous),
            "fill chronological admission",
        )
        previous = timestamp
    order_ids = list(dict.fromkeys(row["order_id"] for row in orders))
    matches, identities = {}, {}
    coverage = Counter(
        {
            name: 0
            for name in (
                "same_timestamp_cancel_after_partial_fill",
                "orders_with_multiple_fill_events",
                "through_fills_after_prior_partial_fill",
                "side_bid",
                "side_ask",
                "combined_quantity_exceeds_printed_events",
                "through_quantity_exceeds_printed_events",
                "multiple_exact_fills_same_trigger",
            )
        }
    )
    coverage["transition_rows_with_nonempty_order_id"] = len(orders)
    for order_id in order_ids:
        require(type(order_id) is str, "order ID is not a string")
        history = [row for row in orders if row["order_id"] == order_id]
        identity = {field: history[0][field] for field in IDENTITY}
        require(
            all(
                all(
                    row[field] == identity[field]
                    and type(row[field]) is type(identity[field])
                    for field in IDENTITY
                )
                for row in history
            ),
            "immutable order identity changed: " + order_id,
        )
        require(
            all(
                type(identity[field]) is str and identity[field]
                for field in ("client_order_id", "strategy_id")
            ),
            "invalid catalog identity",
        )
        require(
            type(identity["side"]) is int and identity["side"] in (-1, 1),
            "invalid catalog side",
        )
        require(
            all(
                type(identity[field]) is int and identity[field] > 0
                for field in ("price_ticks", "original_quantity")
            ),
            "invalid original order price/quantity",
        )
        clocks = [
            identity[field]
            for field in (
                "creation_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
            )
        ]
        require(
            all(type(x) is int and x >= 0 for x in clocks)
            and clocks[0] <= clocks[1] <= clocks[2],
            "catalog order clock admission",
        )
        own_fills = [fill for fill in fills if fill["order_id"] == order_id]
        cursor, remaining, status, last_time, last_fill_time = (
            0,
            identity["original_quantity"],
            None,
            -1,
            None,
        )
        for transition in history:
            timestamp = transition["timestamp_ns"]
            require(
                type(timestamp) is int and timestamp >= max(0, last_time),
                "per-order transition time regression",
            )
            require(
                transition["previous_status"] == status,
                "transition previous-status chain mismatch",
            )
            target = transition["new_status"]
            if status is None:
                require(
                    target == "pending_arrival"
                    and transition["reason"] == "created"
                    and timestamp == clocks[2],
                    "invalid initial order transition",
                )
            else:
                require(target in EDGES[status], "invalid lifecycle edge")
            if transition["reason"] == "fill":
                require(
                    status in ("live", "partially_filled") and cursor < len(own_fills),
                    "unmatched fill transition",
                )
                fill = own_fills[cursor]
                for field in ("client_order_id", "strategy_id", "side", "price_ticks"):
                    require(
                        fill[field] == identity[field]
                        and type(fill[field]) is type(identity[field]),
                        "fill/order identity mismatch: " + field,
                    )
                require(
                    all(
                        type(fill[name]) is int
                        for name in (
                            "decision_timestamp_ns",
                            "send_timestamp_ns",
                            "exchange_arrival_timestamp_ns",
                        )
                    )
                    and fill["decision_timestamp_ns"] == clocks[0]
                    and fill["send_timestamp_ns"] == clocks[1]
                    and fill["exchange_arrival_timestamp_ns"] == clocks[2],
                    "fill/order clock identity mismatch",
                )
                notify = fill["strategy_notification_timestamp_ns"]
                require(
                    type(notify) is int
                    and clocks[2]
                    < timestamp
                    == fill["exchange_fill_timestamp_ns"]
                    <= notify,
                    "arrival/trade/notification admission",
                )
                before = remaining
                remaining -= fill["quantity"]
                require(
                    remaining >= 0
                    and target == ("filled" if remaining == 0 else "partially_filled"),
                    "fill remaining/status mismatch",
                )
                matches[fill["fill_id"]] = {
                    "pre": before,
                    "post": remaining,
                    "transition": transition["transition_id"],
                }
                cursor += 1
                last_fill_time = timestamp
            elif (
                target == "cancelled"
                and last_fill_time == timestamp
                and status == "partially_filled"
            ):
                coverage["same_timestamp_cancel_after_partial_fill"] += 1
            require(
                type(transition["remaining_quantity"]) is int
                and transition["remaining_quantity"] == remaining,
                "transition remaining checkpoint mismatch",
            )
            status, last_time = target, timestamp
        require(cursor == len(own_fills), "fills missing lifecycle transitions")
        identities[order_id] = identity
        coverage["orders_with_multiple_fill_events"] += int(len(own_fills) > 1)
    require(
        len(matches) == len(fills),
        "not every emitted fill has exactly one matched order transition",
    )
    return matches, identities, coverage


def summarize(
    records: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    tick: Fraction,
    catalog_count: int,
) -> dict[str, Any]:
    all_orders = {row["order_id"] for row in records}
    all_events = {row["trigger_sequence"] for row in records}
    total_units = sum(row["quantity"] for row in records)
    total_notional = sum(row["tick_notional"] for row in records)

    def counts(selected: list[dict[str, Any]]) -> dict[str, Any]:
        units = sum(row["quantity"] for row in selected)
        notional = sum(row["tick_notional"] for row in selected)
        return {
            "fill_count": len(selected),
            "order_count": len({row["order_id"] for row in selected}),
            "trigger_event_count": len({row["trigger_sequence"] for row in selected}),
            "quantity": units,
            "tick_notional": notional,
            "currency_notional": ratio(notional * tick.numerator, tick.denominator),
        }

    classes = {}
    for label in ("exact_price", "price_through"):
        selected = [row for row in records if row["classification"] == label]
        result = counts(selected)
        result.update(
            fill_share=ratio(len(selected), len(records)),
            quantity_share=ratio(result["quantity"], total_units),
            tick_notional_share=ratio(result["tick_notional"], total_notional),
            order_coverage=ratio(result["order_count"], len(all_orders)),
            trigger_event_coverage=ratio(
                result["trigger_event_count"], len(all_events)
            ),
        )
        classes[label] = result
    through = [row for row in records if row["classification"] == "price_through"]
    distances = [row["distance_ticks"] for row in through]
    orders_by_class = [
        {row["order_id"] for row in records if row["classification"] == label}
        for label in classes
    ]
    events_by_class = [
        {row["trigger_sequence"] for row in records if row["classification"] == label}
        for label in classes
    ]
    require(
        len(records) == len(fills)
        and sum(result["quantity"] for result in classes.values()) == total_units
        and sum(result["tick_notional"] for result in classes.values())
        == total_notional,
        "exhaustive classification coverage failed",
    )
    return {
        "total": counts(records),
        "classes": classes,
        "order_catalog_count": catalog_count,
        "order_set_overlap": len(set.intersection(*orders_by_class)),
        "trigger_set_overlap": len(set.intersection(*events_by_class)),
        "coverage_denominators": "Class order/event coverage uses all filled orders/associated trigger events; sets may overlap and are not disjoint shares",
        "through_distance_ticks": {
            "minimum": min(distances, default=None),
            "maximum": max(distances, default=None),
            "unit_weighted_mean": ratio(
                sum(row["quantity"] * row["distance_ticks"] for row in through),
                sum(row["quantity"] for row in through),
            ),
        },
    }


def review(
    root: Path,
    selection: tuple[str, int, int],
    metadata: dict[str, Any],
    progress: dict[str, Any],
    stage_start: float,
) -> None:
    import pyarrow.parquet as pq

    began = time.monotonic()
    ident, expected_events, expected_fills = selection
    progress.update(cohort=ident, status="started", per_fill=[], event_budgets=[])
    relative = Path("attempts") / ident
    engine = relative / "engine/resource-study"
    names = {
        "input": relative / "input/events.parquet",
        "fills": engine / "fills.parquet",
        "orders": engine / "orders.parquet",
        "config": engine / "run_config.yaml",
        "resource_result": relative / "result.json",
    }
    paths = {key: root / RESOURCE / path for key, path in names.items()}
    progress["original_file_hashes"] = {key: sha(path) for key, path in paths.items()}
    for key, value in progress["original_file_hashes"].items():
        require(
            value == metadata["original_file_stats"][names[key].as_posix()]["sha256"],
            "immutable source file changed: " + key,
        )
    literals = config_literals(paths["config"])
    tape, times, fingerprint = strict_tape(
        pq.read_table(paths["input"]), expected_events
    )
    resource = json.loads(paths["resource_result"].read_text())
    require(
        fingerprint == resource["input"]["event_stream_sha256"],
        "original event fingerprint mismatch",
    )
    fills = pq.read_table(paths["fills"]).to_pylist()
    orders = pq.read_table(paths["orders"]).to_pylist()
    require(len(fills) == expected_fills, "known original fill coverage changed")
    matches, identities, coverage = transition_matches(orders, fills)
    for fill in fills:
        require(
            time.monotonic() - began <= 120 and time.monotonic() - stage_start <= 300,
            "independent reference time cap",
        )
        timestamp = fill["exchange_fill_timestamp_ns"]
        left, right = (
            bisect.bisect_left(times, timestamp),
            bisect.bisect_right(times, timestamp),
        )
        require(
            right - left == 1,
            "fill timestamp does not identify exactly one ALL-type tape event",
        )
        event = tape[left]
        require(
            event["event_type"] == "TRADE" and event["side"] == fill["side"],
            "unique trigger is not a matching-side trade",
        )
        distance = fill["side"] * (fill["price_ticks"] - event["price_ticks"])
        require(distance >= 0, "maker execution is worse-priced than trigger trade")
        label = "exact_price" if distance == 0 else "price_through"
        match = matches[fill["fill_id"]]
        require(
            fill["quantity"] <= match["pre"]
            and (label != "price_through" or fill["quantity"] == match["pre"]),
            "exact/through remaining-quantity invariant",
        )
        if (
            label == "price_through"
            and match["pre"] < identities[fill["order_id"]]["original_quantity"]
        ):
            coverage["through_fills_after_prior_partial_fill"] += 1
        coverage["side_bid" if fill["side"] == 1 else "side_ask"] += 1
        progress["per_fill"].append(
            {
                "fill_id": fill["fill_id"],
                "order_id": fill["order_id"],
                "trigger_sequence": event["sequence_number"],
                "classification": label,
                "quantity": fill["quantity"],
                "pre_fill_remaining": match["pre"],
                "post_fill_remaining": match["post"],
                "distance_ticks": distance,
                "tick_notional": fill["quantity"] * fill["price_ticks"],
                "matched_transition_id": match["transition"],
            }
        )
    records = progress["per_fill"]
    for event in tape:
        associated = [
            row
            for row in records
            if row["trigger_sequence"] == event["sequence_number"]
        ]
        if not associated:
            continue
        exact = sum(
            row["quantity"]
            for row in associated
            if row["classification"] == "exact_price"
        )
        through = sum(
            row["quantity"]
            for row in associated
            if row["classification"] == "price_through"
        )
        record = {
            "trigger_sequence": event["sequence_number"],
            "timestamp_ns": event["timestamp_ns"],
            "printed_quantity": event["quantity"],
            "exact_quantity": exact,
            "through_quantity": through,
            "combined_quantity": exact + through,
            "exact_within_printed": exact <= event["quantity"],
            "combined_exceeds_printed": exact + through > event["quantity"],
            "through_to_printed_ratio": ratio(through, event["quantity"]),
        }
        progress["event_budgets"].append(record)
        require(
            record["exact_within_printed"], "shared exact-price printed budget exceeded"
        )
        coverage["combined_quantity_exceeds_printed_events"] += int(
            record["combined_exceeds_printed"]
        )
        coverage["through_quantity_exceeds_printed_events"] += int(
            through > event["quantity"]
        )
        coverage["multiple_exact_fills_same_trigger"] += int(
            sum(row["classification"] == "exact_price" for row in associated) > 1
        )
    summary = summarize(records, fills, Fraction(literals["tick"]), len(identities))
    require(
        time.monotonic() - began <= 120 and time.monotonic() - stage_start <= 300,
        "independent reference time cap",
    )
    progress.update(
        status="complete",
        input_fingerprint=fingerprint,
        literal_configuration=literals,
        coverage=dict(coverage),
        summary=summary,
        per_fill_sha256=hashlib.sha256(encoded(records)).hexdigest(),
        event_budgets_sha256=hashlib.sha256(
            encoded(progress["event_budgets"])
        ).hexdigest(),
        summary_sha256=hashlib.sha256(encoded(summary)).hexdigest(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root, began = args.root.resolve(), time.monotonic()
    result: dict[str, Any] = {
        "status": "started",
        "independent_source_sha256": sha(Path(__file__).resolve()),
        "cohorts": [],
    }
    try:
        require(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application imported",
        )
        run = root / RESOURCE
        metadata, plan = (
            json.loads((run / name).read_text())
            for name in ("manifest.json", "plan.json")
        )
        require(
            sha(run / "plan.json") == metadata["plan_sha256"]
            and plan["source_revision"] == SOURCE,
            "original source/plan identity mismatch",
        )
        source = {
            path.relative_to(root).as_posix(): sha(path)
            for path in (root / "src/lobmm").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        require(
            len(source) == 38 and source == plan["source_sha256"],
            "production bytes changed",
        )
        result["source_revision"] = SOURCE
        for selected in COHORTS:
            item: dict[str, Any] = {}
            result["cohorts"].append(item)
            review(root, selected, metadata, item, began)
        require(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application imported during review",
        )
        result.update(
            status="complete",
            all_fills_attributed=True,
            all_invariants_pass=True,
            scope="Same exposed synthetic records; descriptive execution-model provenance conditional on emitted fills; no true FIFO, calibration, causal counterfactual, toggle-run, profitability or execution-quality claim",
        )
    except Exception as error:
        result.update(
            status="stopped",
            error={"type": type(error).__name__, "message": str(error)},
            traceback=traceback.format_exc(),
        )
    content = encoded(result) + b"\n"
    require(len(content) <= 64 * 1024**2, "independent reference evidence cap")
    sys.stdout.buffer.write(content)
    return 0 if result.get("all_invariants_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
