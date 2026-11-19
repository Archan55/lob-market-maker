"""Fixed-cohort offline maker-fill trigger provenance; no application imports.

--write-plan binds metadata without classification. --run is the explicit
two-cohort pass. --verify-evidence checks saved hashes and counters only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import itertools
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import traceback
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/audit_fill_triggers.py"
TEST = "tests/unit/test_fill_triggers.py"
DESIGN = "docs/FILL_TRIGGER_DESIGN.md"
RESOURCE = "docs/evidence/end-to-end-resources-20261117/run"
SOURCE = "0866bf13e449cd66e3862454eaaf424a5172c33a"
ORIGINAL = {
    RESOURCE
    + "/manifest.json": "a0022001dd042444b7f7500c55404bd14c744478e16829fa2ccab86d6fcdeda4",
    RESOURCE
    + "/plan.json": "ffcfa3bae32b4263c9e6d681b2291ffad69b0732f35e38ff62bf942792ef62b0",
}
COHORTS = ("001", "002")
INPUT_COLUMNS = (
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
ALLOWED = {
    "pending_arrival": {"live", "rejected"},
    "live": {"partially_filled", "filled", "cancelled", "expired"},
    "partially_filled": {"partially_filled", "filled", "cancelled", "expired"},
    "filled": set(),
    "cancelled": set(),
    "expired": set(),
    "rejected": set(),
}
LIMITS = {
    "input_rows": 100000,
    "order_rows": 100000,
    "fills": 20000,
    "worker_seconds": 120,
    "controller_seconds": 300,
    "evidence_bytes": 64 * 1024**2,
}
STOP_METADATA_RESERVE = 65536


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def rational(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def ratio(numerator: int, denominator: int) -> dict[str, int] | None:
    return rational(Fraction(numerator, denominator)) if denominator else None


def positive(value: Any) -> bool:
    return type(value) is int and value > 0


def reconstruct_tape(rows: list[dict[str, Any]]) -> dict[str, Any]:
    require(0 < len(rows) <= LIMITS["input_rows"], "input row admission/cap")
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    events_by_timestamp: dict[int, list[dict[str, Any]]] = {}
    hasher = hashlib.sha256()
    codes = {"ADD": 1, "CANCEL": 2, "TRADE": 3, "RESET": 4, "SNAPSHOT": 5}
    last_time = last_sequence = -1
    for index, row in enumerate(rows):
        require(set(row) == set(INPUT_COLUMNS), f"input fields differ at {index}")
        timestamp, sequence, kind, side, price, quantity = [
            row[name] for name in INPUT_COLUMNS
        ]
        require(
            type(timestamp) is int
            and timestamp >= 0
            and timestamp >= last_time
            and type(sequence) is int
            and sequence > last_sequence
            and sequence >= 0,
            f"input order differs at {index}",
        )
        require(type(kind) is str and kind in codes, f"unknown input kind at {index}")
        if kind == "RESET":
            require(
                side is None
                and type(price) is int
                and type(quantity) is int
                and price == quantity == 0,
                f"invalid RESET at {index}",
            )
            book = {1: {}, -1: {}}
        else:
            require(
                type(side) is int
                and side in (-1, 1)
                and positive(price)
                and positive(quantity),
                f"invalid book scalars at {index}",
            )
            before = book[side].get(price, 0)
            after = (
                before + quantity if kind in ("ADD", "SNAPSHOT") else before - quantity
            )
            require(after >= 0, f"external book overdraw at {index}")
            if after:
                book[side][price] = after
            else:
                book[side].pop(price, None)
            require(
                not book[1] or not book[-1] or max(book[1]) < min(book[-1]),
                f"crossed external book at {index}",
            )
        hasher.update(
            struct.pack(
                ">qqbbqq",
                timestamp,
                sequence,
                codes[kind],
                0 if side is None else side,
                price,
                quantity,
            )
        )
        events_by_timestamp.setdefault(timestamp, []).append(row)
        last_time, last_sequence = timestamp, sequence
    return {
        "event_stream_sha256": hasher.hexdigest(),
        "events_by_timestamp": events_by_timestamp,
        "events": rows,
    }


def classify_relation(side: int, limit: int, trade_price: int) -> str:
    require(
        type(side) is int
        and side in (-1, 1)
        and positive(limit)
        and positive(trade_price),
        "invalid price relation",
    )
    if limit == trade_price:
        return "exact_price"
    require(
        side * (limit - trade_price) > 0,
        "maker price is worse than the associated trade",
    )
    return "price_through"


def analyze_rows(
    events: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    tick: Fraction,
    emit: Any = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Join immutable identities/checkpoints; deliberately never read final metadata."""

    def check(condition: bool, message: str, **context: Any) -> None:
        if not condition:
            if emit is not None:
                emit("failure", {"message": message, **context})
            raise ValueError(message)

    tape = reconstruct_tape(events)
    check(
        expected_fingerprint is None
        or tape["event_stream_sha256"] == expected_fingerprint,
        "reconstructed input fingerprint differs before attribution",
    )
    check(
        len(transitions) <= LIMITS["order_rows"] and len(fills) <= LIMITS["fills"],
        "order/fill row cap",
    )
    check(isinstance(tick, Fraction) and tick > 0, "invalid literal tick value")
    catalog: dict[str, dict[str, Any]] = {}
    transition_ids: set[str] = set()
    client_ids: dict[str, str] = {}
    last_transition_time = -1
    for index, row in enumerate(transitions):
        identifier, order_id = row["transition_id"], row["order_id"]
        check(
            type(identifier) is str
            and bool(identifier)
            and identifier not in transition_ids,
            "duplicate/empty transition ID",
            transition_row=index,
        )
        check(
            type(order_id) is str and bool(order_id),
            "empty order ID",
            transition_row=index,
        )
        transition_ids.add(identifier)
        identity = {name: row[name] for name in IDENTITY}
        check(
            all(
                type(identity[name]) is str and bool(identity[name])
                for name in ("client_order_id", "strategy_id")
            ),
            "invalid immutable order identifiers",
            transition_row=index,
        )
        check(
            type(identity["side"]) is int
            and identity["side"] in (-1, 1)
            and positive(identity["price_ticks"])
            and positive(identity["original_quantity"]),
            "invalid immutable order quantities",
            transition_row=index,
        )
        clocks = [
            identity[name]
            for name in (
                "creation_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
            )
        ]
        check(
            all(type(value) is int and value >= 0 for value in clocks)
            and all(a <= b for a, b in itertools.pairwise(clocks)),
            "invalid immutable order clocks",
            transition_row=index,
        )
        timestamp, remaining, new, previous = (
            row["timestamp_ns"],
            row["remaining_quantity"],
            row["new_status"],
            row["previous_status"],
        )
        check(
            type(timestamp) is int
            and timestamp >= last_transition_time
            and timestamp >= 0
            and type(remaining) is int
            and 0 <= remaining <= identity["original_quantity"]
            and new in ALLOWED,
            "invalid transition checkpoint",
            transition_row=index,
        )
        last_transition_time = timestamp
        if order_id not in catalog:
            check(
                previous is None
                and new == "pending_arrival"
                and row["reason"] == "created"
                and remaining == identity["original_quantity"]
                and timestamp == clocks[-1],
                "invalid initial order transition",
                transition_row=index,
            )
            check(
                identity["client_order_id"] not in client_ids,
                "client ID belongs to multiple orders",
                transition_row=index,
            )
            client_ids[identity["client_order_id"]] = order_id
            catalog[order_id] = {
                "identity": identity,
                "status": new,
                "remaining": remaining,
                "fill_transitions": [],
            }
            continue
        order = catalog[order_id]
        check(
            identity == order["identity"]
            and previous == order["status"]
            and new in ALLOWED[previous],
            "order identity or status chain differs",
            transition_row=index,
        )
        check(
            (new in ("partially_filled", "filled")) == (row["reason"] == "fill"),
            "fill statuses require exactly reason=fill",
            transition_row=index,
        )
        if row["reason"] == "fill":
            check(
                new in ("partially_filled", "filled")
                and remaining < order["remaining"]
                and (new == "filled") == (remaining == 0),
                "invalid fill transition remaining/status",
                transition_row=index,
            )
            order["fill_transitions"].append(row)
        else:
            check(
                remaining == order["remaining"],
                "non-fill transition consumed quantity",
                transition_row=index,
            )
        order["status"], order["remaining"] = new, remaining
    records: list[dict[str, Any]] = []
    event_totals: dict[int, dict[str, Any]] = {}
    consumed = {order_id: 0 for order_id in catalog}
    matched = {order_id: 0 for order_id in catalog}
    fill_ids: set[str] = set()
    last_fill_time = -1
    for index, fill in enumerate(fills):
        fill_id, order_id = fill["fill_id"], fill["order_id"]
        check(
            type(fill_id) is str and bool(fill_id) and fill_id not in fill_ids,
            "duplicate/empty fill ID",
            fill_row=index,
        )
        fill_ids.add(fill_id)
        check(
            order_id in catalog and fill["liquidity_role"] == "maker",
            "unknown order or non-maker fill",
            fill_row=index,
        )
        order = catalog[order_id]
        identity = order["identity"]
        quantity, timestamp = fill["quantity"], fill["exchange_fill_timestamp_ns"]
        check(
            positive(quantity)
            and positive(fill["price_ticks"])
            and type(fill["side"]) is int
            and fill["side"] in (-1, 1)
            and type(timestamp) is int
            and timestamp >= last_fill_time,
            "invalid emitted fill scalars/order",
            fill_row=index,
        )
        last_fill_time = timestamp
        check(
            all(
                fill[name] == identity[name]
                for name in (
                    "client_order_id",
                    "strategy_id",
                    "side",
                    "price_ticks",
                    "send_timestamp_ns",
                    "exchange_arrival_timestamp_ns",
                )
            )
            and fill["decision_timestamp_ns"] == identity["creation_timestamp_ns"],
            "fill/order immutable identity mismatch",
            fill_row=index,
        )
        notification = fill["strategy_notification_timestamp_ns"]
        check(
            type(notification) is int
            and identity["exchange_arrival_timestamp_ns"] < timestamp <= notification,
            "maker arrival/trade/notification order differs",
            fill_row=index,
        )
        candidates = tape["events_by_timestamp"].get(timestamp, [])
        check(
            len(candidates) == 1,
            "fill timestamp does not identify exactly one canonical event",
            fill_row=index,
        )
        event = candidates[0]
        check(
            event["event_type"] == "TRADE" and event["side"] == fill["side"],
            "associated event is not a matching-side TRADE",
            fill_row=index,
        )
        try:
            classification = classify_relation(
                fill["side"], fill["price_ticks"], event["price_ticks"]
            )
        except ValueError:
            check(False, "worse-price maker association", fill_row=index)
            raise
        before = identity["original_quantity"] - consumed[order_id]
        check(
            quantity <= before
            and (classification != "price_through" or quantity == before),
            "fill violates independent remaining quantity",
            fill_row=index,
        )
        after = before - quantity
        position = matched[order_id]
        check(
            position < len(order["fill_transitions"]),
            "fill has no unmatched immutable fill transition",
            fill_row=index,
        )
        transition = order["fill_transitions"][position]
        check(
            transition["timestamp_ns"] == timestamp
            and transition["remaining_quantity"] == after
            and transition["new_status"]
            == ("filled" if after == 0 else "partially_filled"),
            "fill/transition checkpoint mismatch",
            fill_row=index,
        )
        matched[order_id] += 1
        consumed[order_id] += quantity
        record = {
            "fill_id": fill_id,
            "order_id": order_id,
            "trigger_sequence": event["sequence_number"],
            "classification": classification,
            "quantity": quantity,
            "pre_fill_remaining": before,
            "post_fill_remaining": after,
            "distance_ticks": abs(fill["price_ticks"] - event["price_ticks"]),
            "tick_notional": quantity * fill["price_ticks"],
            "matched_transition_id": transition["transition_id"],
        }
        records.append(record)
        if emit is not None:
            emit("fill", record)
        total = event_totals.setdefault(
            event["sequence_number"],
            {
                "trigger_sequence": event["sequence_number"],
                "timestamp_ns": timestamp,
                "printed_quantity": event["quantity"],
                "exact_quantity": 0,
                "through_quantity": 0,
            },
        )
        total[
            "exact_quantity" if classification == "exact_price" else "through_quantity"
        ] += quantity
    for order_id, order in catalog.items():
        check(
            matched[order_id] == len(order["fill_transitions"]),
            "unmatched immutable fill transition",
            order_id=order_id,
        )
    budget_records = []
    for event in events:
        if event["sequence_number"] not in event_totals:
            continue
        total = event_totals[event["sequence_number"]]
        total.update(
            combined_quantity=total["exact_quantity"] + total["through_quantity"],
            exact_within_printed=total["exact_quantity"] <= total["printed_quantity"],
            combined_exceeds_printed=total["exact_quantity"] + total["through_quantity"]
            > total["printed_quantity"],
            through_to_printed_ratio=ratio(
                total["through_quantity"], total["printed_quantity"]
            ),
        )
        budget_records.append(total)
        if emit is not None:
            emit("event", total)
        check(
            total["exact_within_printed"],
            "shared exact-price printed budget exceeded",
            trigger_sequence=total["trigger_sequence"],
        )

    def totals(selected: list[dict[str, Any]]) -> dict[str, Any]:
        notional = sum(row["tick_notional"] for row in selected)
        return {
            "fill_count": len(selected),
            "order_count": len({row["order_id"] for row in selected}),
            "trigger_event_count": len({row["trigger_sequence"] for row in selected}),
            "quantity": sum(row["quantity"] for row in selected),
            "tick_notional": notional,
            "currency_notional": rational(notional * tick),
        }

    total = totals(records)
    classes = {}
    for name in ("exact_price", "price_through"):
        selected = [row for row in records if row["classification"] == name]
        item = totals(selected)
        item.update(
            fill_share=ratio(item["fill_count"], total["fill_count"]),
            quantity_share=ratio(item["quantity"], total["quantity"]),
            tick_notional_share=ratio(item["tick_notional"], total["tick_notional"]),
            order_coverage=ratio(item["order_count"], total["order_count"]),
            trigger_event_coverage=ratio(
                item["trigger_event_count"], total["trigger_event_count"]
            ),
        )
        classes[name] = item
    exact = [row for row in records if row["classification"] == "exact_price"]
    through = [row for row in records if row["classification"] == "price_through"]
    summary = {
        "total": total,
        "classes": classes,
        "order_set_overlap": len(
            {row["order_id"] for row in exact} & {row["order_id"] for row in through}
        ),
        "trigger_set_overlap": len(
            {row["trigger_sequence"] for row in exact}
            & {row["trigger_sequence"] for row in through}
        ),
        "through_distance_ticks": {
            "minimum": min((row["distance_ticks"] for row in through), default=None),
            "maximum": max((row["distance_ticks"] for row in through), default=None),
            "unit_weighted_mean": ratio(
                sum(row["distance_ticks"] * row["quantity"] for row in through),
                sum(row["quantity"] for row in through),
            ),
        },
        "order_catalog_count": len(catalog),
        "set_scope": "Within this cohort only; class order/event sets may overlap. Exact-price fills are not calibrated venue executions.",
    }
    return {
        "fills": records,
        "events": budget_records,
        "summary": summary,
        "event_stream_sha256": tape["event_stream_sha256"],
    }


def source_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted((root / "src/lobmm").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def selected_paths(cohort: str) -> dict[str, str]:
    base = f"{RESOURCE}/attempts/{cohort}/"
    engine = base + "engine/resource-study/"
    return {
        "input": base + "input/events.parquet",
        "orders": engine + "orders.parquet",
        "fills": engine + "fills.parquet",
        "config": engine + "run_config.yaml",
        "result": base + "result.json",
    }


def make_plan(input_root: Path) -> dict[str, Any]:
    original, resource_plan = (
        read(input_root / RESOURCE / "manifest.json"),
        read(input_root / RESOURCE / "plan.json"),
    )
    require(
        all(digest(input_root / name) == sha for name, sha in ORIGINAL.items()),
        "original PR14 metadata differs",
    )
    require(
        original["status"] == "complete"
        and original["plan_sha256"] == ORIGINAL[RESOURCE + "/plan.json"]
        and resource_plan["source_revision"] == SOURCE
        and source_inventory(input_root) == resource_plan["source_sha256"],
        "original source chain differs",
    )
    cohorts = []
    for cohort in COHORTS:
        files = {}
        for name, path in selected_paths(cohort).items():
            sha = original["original_file_stats"][
                str(Path(path).relative_to(RESOURCE))
            ]["sha256"]
            require(
                digest(input_root / path) == sha, "selected immutable input differs"
            )
            files[name] = {"path": path, "sha256": sha}
        metadata = read(input_root / files["result"]["path"])
        cohorts.append(
            {
                "id": cohort,
                "files": files,
                "event_stream_sha256": metadata["input"]["event_stream_sha256"],
            }
        )
    return {
        "version": 1,
        "original_metadata": ORIGINAL,
        "source_revision": SOURCE,
        "source_sha256": resource_plan["source_sha256"],
        "cohorts": cohorts,
        "limits": LIMITS,
        "pins": {name: digest(ROOT / name) for name in (TOOL, TEST, DESIGN)},
    }


def validate_plan(
    plan: dict[str, Any], input_root: Path, harness_root: Path = ROOT
) -> None:
    require(
        plan["original_metadata"] == ORIGINAL
        and plan["source_revision"] == SOURCE
        and plan["limits"] == LIMITS
        and [row["id"] for row in plan["cohorts"]] == list(COHORTS),
        "fixed plan selection differs",
    )
    require(set(plan["pins"]) == {TOOL, TEST, DESIGN}, "harness pin inventory differs")
    for name, sha in plan["pins"].items():
        require(
            digest(harness_root / name) == sha, "frozen analyzer/control/design changed"
        )
    require(
        all(digest(input_root / name) == sha for name, sha in ORIGINAL.items()),
        "original metadata changed",
    )
    original, resource_plan = (
        read(input_root / RESOURCE / "manifest.json"),
        read(input_root / RESOURCE / "plan.json"),
    )
    require(
        original["status"] == "complete"
        and original["plan_sha256"] == ORIGINAL[RESOURCE + "/plan.json"]
        and resource_plan["source_revision"] == SOURCE
        and len(plan["source_sha256"]) == 38
        and source_inventory(input_root)
        == resource_plan["source_sha256"]
        == plan["source_sha256"],
        "source identity differs",
    )
    for cohort in plan["cohorts"]:
        require(
            {name: value["path"] for name, value in cohort["files"].items()}
            == selected_paths(cohort["id"]),
            "cohort paths differ",
        )
        for saved in cohort["files"].values():
            require(
                original["original_file_stats"][
                    str(Path(saved["path"]).relative_to(RESOURCE))
                ]["sha256"]
                == saved["sha256"]
                and digest(input_root / saved["path"]) == saved["sha256"],
                "selected input bytes differ",
            )
        require(
            read(input_root / cohort["files"]["result"]["path"])["input"][
                "event_stream_sha256"
            ]
            == cohort["event_stream_sha256"],
            "selected fingerprint metadata differs",
        )


def evidence_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def worker(plan_path: Path, cohort_id: str, input_root: Path, output: Path) -> int:
    require(not output.exists(), "refusing an existing cohort output")
    output.mkdir(parents=True)
    result: dict[str, Any] = {
        "cohort": cohort_id,
        "status": "started",
        "plan_sha256": digest(plan_path),
        "processed_prefix": {
            "classified_fills": 0,
            "event_records": 0,
            "encountered_failures": 0,
        },
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "dependencies": {
                name: importlib.metadata.version(name) for name in ("pyarrow", "PyYAML")
            },
        },
    }
    write(output / "result.json", result)
    streams = {
        name: (output / (name + ".jsonl")).open("x")
        for name in ("fill", "event", "failure")
    }
    associated_prefix: set[int] = set()

    def emit(kind: str, record: Any) -> None:
        line = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
        require(
            evidence_size(output.parent.parent) + len(line.encode())
            <= LIMITS["evidence_bytes"] - STOP_METADATA_RESERVE,
            "generated evidence cap with reserved stop metadata",
        )
        streams[kind].write(line)
        streams[kind].flush()
        os.fsync(streams[kind].fileno())
        counter = {
            "fill": "classified_fills",
            "event": "event_records",
            "failure": "encountered_failures",
        }[kind]
        result["processed_prefix"][counter] += 1
        if kind == "fill":
            associated_prefix.add(record["trigger_sequence"])
        write(output / "result.json", result)

    try:
        plan = read(plan_path)
        validate_plan(plan, input_root)
        require(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "unexpected application import",
        )
        result["no_application_imports_before"] = True
        import pyarrow as pa
        import pyarrow.parquet as pq
        import yaml

        selected = next(row for row in plan["cohorts"] if row["id"] == cohort_id)
        paths = {
            name: input_root / item["path"] for name, item in selected["files"].items()
        }
        raw = yaml.load(paths["config"].read_text(), Loader=yaml.BaseLoader)
        require(
            raw["queue_model"]["price_through_fills"] == "true"
            and raw["strategy"]["post_only"] == "true"
            and raw["backtest"]["session_end_policy"] == "mark",
            "fixed policy admission differs",
        )
        tick = Fraction(raw["instrument"]["tick_size"])
        tables = {
            name: pq.ParquetFile(paths[name]) for name in ("input", "orders", "fills")
        }
        require(
            tables["input"].metadata.num_rows <= LIMITS["input_rows"]
            and tables["orders"].metadata.num_rows <= LIMITS["order_rows"]
            and tables["fills"].metadata.num_rows <= LIMITS["fills"],
            "input/order/fill cap",
        )
        result["admitted_table_sizes"] = {
            name: table.metadata.num_rows for name, table in tables.items()
        }
        result["stage"] = "schema and provenance admission"
        write(output / "result.json", result)
        require(
            tables["input"].schema_arrow.names == list(INPUT_COLUMNS),
            "canonical input columns differ",
        )
        for name in INPUT_COLUMNS:
            dtype = tables["input"].schema_arrow.field(name).type
            require(
                (pa.types.is_string(dtype) or pa.types.is_large_string(dtype))
                if name == "event_type"
                else pa.types.is_int8(dtype)
                if name == "side"
                else pa.types.is_int64(dtype),
                "canonical input Arrow dtype differs",
            )
        required_ints = {
            "orders": (
                "timestamp_ns",
                "remaining_quantity",
                "side",
                "price_ticks",
                "original_quantity",
                "creation_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
            ),
            "fills": (
                "side",
                "quantity",
                "price_ticks",
                "exchange_fill_timestamp_ns",
                "decision_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
                "strategy_notification_timestamp_ns",
            ),
        }
        required_strings = {
            "orders": (
                "transition_id",
                "order_id",
                "previous_status",
                "new_status",
                "reason",
                "client_order_id",
                "strategy_id",
            ),
            "fills": (
                "fill_id",
                "order_id",
                "client_order_id",
                "strategy_id",
                "liquidity_role",
            ),
        }
        for table in ("orders", "fills"):
            for name in required_ints[table]:
                require(
                    pa.types.is_int64(tables[table].schema_arrow.field(name).type),
                    "required Arrow integer differs",
                )
            for name in required_strings[table]:
                dtype = tables[table].schema_arrow.field(name).type
                require(
                    pa.types.is_string(dtype) or pa.types.is_large_string(dtype),
                    "required Arrow string differs",
                )
        result["runtime"] = {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "dependencies": {
                name: importlib.metadata.version(name) for name in ("pyarrow", "PyYAML")
            },
        }
        result["config_context"] = {
            "tick_literal": raw["instrument"]["tick_size"],
            "currency": raw["instrument"]["currency"],
            "fees": raw["fees"],
            "exchange_validation_mode": raw["exchange"]["validation_mode"],
            "data_validation_mode": raw["data"]["validation_mode"],
            "independent_tape_proof": "Strict integer depth reconstruction; not a claim the original exchange mode was STRICT",
        }
        write(output / "result.json", result)
        result["stage"] = "independent tape, transition and fill attribution pass"
        write(output / "result.json", result)
        analyzed = analyze_rows(
            *(tables[name].read().to_pylist() for name in ("input", "orders", "fills")),
            tick,
            emit,
            expected_fingerprint=selected["event_stream_sha256"],
        )
        require(
            analyzed["event_stream_sha256"] == selected["event_stream_sha256"],
            "reconstructed input fingerprint differs",
        )
        validate_plan(plan, input_root)
        result.update(
            status="complete",
            summary=analyzed["summary"],
            event_stream_sha256=analyzed["event_stream_sha256"],
            classified_fills=len(analyzed["fills"]),
            associated_events=len(analyzed["events"]),
            untested_suffix=None,
        )
        returncode = 0
    except BaseException as exc:
        result.update(
            status="stopped", stop_reason=repr(exc), traceback=traceback.format_exc()
        )
        if result["processed_prefix"]["encountered_failures"] == 0:
            with contextlib.suppress(ValueError):
                emit(
                    "failure", {"message": repr(exc), "stage": "cohort worker stopped"}
                )
        result["untested_suffix"] = {
            "unclassified_fill_rows": max(
                0,
                result.get("admitted_table_sizes", {}).get("fills", 0)
                - result["processed_prefix"]["classified_fills"],
            ),
            "unprocessed_associated_event_records": max(
                0, len(associated_prefix) - result["processed_prefix"]["event_records"]
            ),
            "note": "The unsafe/ambiguous admission or failed invariant stops the pass. Remaining suffixes are untested; no record is silently accepted.",
        }
        returncode = 2
    finally:
        for stream in streams.values():
            stream.close()
        result["no_application_imports"] = not any(
            name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
        )
        write(output / "result.json", result)
    return returncode


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path != root / "manifest.json"
        and "__pycache__" not in path.parts
    }


def run(plan_path: Path, input_root: Path, output: Path) -> dict[str, Any]:
    started = time.monotonic_ns()
    require(not output.exists(), "refusing to overwrite an audit")
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "status": "started",
        "stage": "metadata admission",
        "started_ns": started,
        "plan_sha256": None,
        "attempts": [],
        "analyzer_sha256": digest(ROOT / TOOL),
    }
    write(output / "manifest.json", manifest)
    try:
        plan = read(plan_path)
        validate_plan(plan, input_root)
        shutil.copy2(plan_path, output / "plan.json")
        manifest["plan_sha256"] = digest(output / "plan.json")
        for name in plan["pins"]:
            target = output / "harness" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        manifest["stage"] = "fixed cohort pass"
        write(output / "manifest.json", manifest)
        for selected in plan["cohorts"]:
            remaining = (
                LIMITS["controller_seconds"] - (time.monotonic_ns() - started) / 1e9
            )
            require(remaining > 0, "controller wall-time cap")
            directory = output / "cohorts" / selected["id"]
            directory.parent.mkdir(exist_ok=True)
            command = [
                sys.executable,
                "-I",
                "-B",
                str(output / "harness" / TOOL),
                "--worker",
                "--plan",
                str(output / "plan.json"),
                "--input-root",
                str(input_root),
                "--cohort",
                selected["id"],
                "--output",
                str(directory),
            ]
            attempt = {
                "cohort": selected["id"],
                "command": command,
                "started_ns": time.monotonic_ns(),
                "cap_seconds": min(120, remaining),
            }
            manifest["attempts"].append(attempt)
            write(output / "manifest.json", manifest)
            try:
                with (output / (selected["id"] + ".log")).open("x") as log:
                    completed = subprocess.run(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=min(120, remaining),
                        check=False,
                    )
                attempt.update(
                    status="complete" if completed.returncode == 0 else "stopped",
                    returncode=completed.returncode,
                )
            except subprocess.TimeoutExpired:
                attempt.update(
                    status="timeout",
                    returncode=None,
                    stop_reason="offline cohort wall-time cap",
                )
                raise
            finally:
                attempt["finished_ns"] = time.monotonic_ns()
                write(output / "manifest.json", manifest)
            require(
                attempt["finished_ns"] - attempt["started_ns"]
                <= attempt["cap_seconds"] * 1e9,
                "cohort wall-time cap including process startup/teardown",
            )
            require(completed.returncode == 0, "cohort stopped; one pass only")
            require(
                evidence_size(output)
                <= LIMITS["evidence_bytes"] - STOP_METADATA_RESERVE,
                "generated evidence cap with stop reserve",
            )
        report = {
            "complete": True,
            "cohorts": [
                read(output / "cohorts" / name / "result.json") for name in COHORTS
            ],
            "scope": "Separate exposed synthetic cohorts; no calibration, missing-fill/FIFO proof, counterfactual engine run or economic claim",
        }
        write(output / "report.json", report)
        manifest.update(
            status="complete", stage="evidence finalization", files=inventory(output)
        )
        return report
    except BaseException as exc:
        manifest.update(
            status="stopped",
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
            files=inventory(output),
            untested_suffix="Only durable admitted per-fill/per-event prefixes are tested; interrupted and subsequent cohort suffixes remain untested.",
        )
        raise
    finally:
        manifest["finished_ns"] = time.monotonic_ns()
        manifest["elapsed_ns"] = manifest["finished_ns"] - started
        over_budget = manifest["elapsed_ns"] > LIMITS["controller_seconds"] * 10**9
        if over_budget:
            manifest.update(status="stopped")
            manifest.setdefault("stop_reason", "controller wall-time cap")
        write(output / "manifest.json", manifest)
        over_size = evidence_size(output) > LIMITS["evidence_bytes"]
        if over_size:
            manifest.update(status="stopped")
            manifest.setdefault("stop_reason", "final generated evidence cap")
            write(output / "manifest.json", manifest)
        if (over_budget or over_size) and sys.exc_info()[0] is None:
            raise ValueError(manifest["stop_reason"])


def verify_evidence(output: Path, input_root: Path) -> dict[str, Any]:
    manifest, plan = read(output / "manifest.json"), read(output / "plan.json")
    require(
        manifest["status"] == "complete"
        and manifest["files"] == inventory(output)
        and manifest["plan_sha256"] == digest(output / "plan.json"),
        "saved evidence differs or is incomplete",
    )
    validate_plan(plan, input_root)
    validate_plan(plan, input_root, output / "harness")
    require(
        [item["cohort"] for item in manifest["attempts"]] == list(COHORTS)
        and manifest["elapsed_ns"] == manifest["finished_ns"] - manifest["started_ns"]
        and manifest["elapsed_ns"] <= 300 * 10**9,
        "saved controller budget/order differs",
    )
    previous = manifest["started_ns"]
    results = []
    for cohort, attempt in zip(COHORTS, manifest["attempts"], strict=True):
        require(
            attempt["status"] == "complete"
            and attempt["returncode"] == 0
            and previous
            <= attempt["started_ns"]
            < attempt["finished_ns"]
            <= manifest["finished_ns"]
            and attempt["finished_ns"] - attempt["started_ns"] <= 120 * 10**9
            and attempt["command"][1:3] == ["-I", "-B"],
            "saved worker identity/budget differs",
        )
        previous = attempt["finished_ns"]
        result = read(output / "cohorts" / cohort / "result.json")
        require(
            result["status"] == "complete"
            and result["no_application_imports"]
            and result["no_application_imports_before"]
            and result["plan_sha256"] == manifest["plan_sha256"],
            "saved cohort status differs",
        )
        for kind, count_name in (
            ("fill", "classified_fills"),
            ("event", "associated_events"),
        ):
            records = [
                json.loads(line)
                for line in (output / "cohorts" / cohort / (kind + ".jsonl"))
                .read_text()
                .splitlines()
            ]
            require(
                len(records) == result[count_name],
                "saved classification/event counter differs",
            )
            prefix_key = "classified_fills" if kind == "fill" else "event_records"
            require(
                result["processed_prefix"][prefix_key] == len(records),
                "saved processed prefix differs",
            )
        require(
            (output / "cohorts" / cohort / "failure.jsonl").stat().st_size == 0,
            "completed cohort has invariant failures",
        )
        results.append(result)
    require(
        read(output / "report.json")["cohorts"] == results
        and evidence_size(output) <= LIMITS["evidence_bytes"],
        "saved report/cap differs",
    )
    return {
        "verified": True,
        "integrity_only_no_classification_rerun": True,
        "cohorts": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-plan", type=Path)
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--verify-evidence", type=Path)
    modes.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input-root", type=Path, default=ROOT)
    parser.add_argument("--cohort", choices=COHORTS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.write_plan is not None:
            require(not args.write_plan.exists(), "refusing to overwrite a plan")
            write(args.write_plan, make_plan(args.input_root.resolve()))
            print(json.dumps({"plan_sha256": digest(args.write_plan)}))
            return 0
        if args.worker:
            require(
                args.plan is not None
                and args.output is not None
                and args.cohort is not None,
                "missing worker arguments",
            )
            return worker(
                args.plan.resolve(),
                args.cohort,
                args.input_root.resolve(),
                args.output.resolve(),
            )
        if args.verify_evidence is not None:
            result = verify_evidence(
                args.verify_evidence.resolve(), args.input_root.resolve()
            )
        else:
            require(
                args.plan is not None and args.output is not None,
                "--run requires a frozen plan and new output",
            )
            result = run(
                args.plan.resolve(), args.input_root.resolve(), args.output.resolve()
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
