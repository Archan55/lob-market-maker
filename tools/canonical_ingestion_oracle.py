"""Independent, untimed canonical input/output audits and paired arithmetic.

This module imports no application code and generates no events. All public
functions are deferred until the protocol, helper and controller are frozen.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import struct
from collections import Counter
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

COLUMNS = (
    "timestamp_ns",
    "sequence_number",
    "event_type",
    "side",
    "price_ticks",
    "quantity",
)
INTEGER_FIELDS = ("timestamp_ns", "sequence_number", "side", "price_ticks", "quantity")
SCHEMA = [
    ["timestamp_ns", "Int64"],
    ["sequence_number", "Int64"],
    ["event_type", "String"],
    ["side", "Int8"],
    ["price_ticks", "Int64"],
    ["quantity", "Int64"],
]
EVENT_CODES = {"ADD": 1, "CANCEL": 2, "TRADE": 3, "RESET": 4, "SNAPSHOT": 5}
EVENT_STRUCT = struct.Struct(">qqbbqq")
EVENT_COUNT = 100000
PRIMARY = "parquet_load_events"


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _type_name(value: Any) -> str:
    return type(value).__module__ + "." + type(value).__qualname__


def audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Check every original scalar and replay depth with plain dictionaries."""
    if type(rows) is not list or len(rows) != EVENT_COUNT:
        raise ValueError("expected exactly 100000 original rows")
    field_types: dict[str, Counter[str]] = {name: Counter() for name in COLUMNS}
    event_counts: Counter[str] = Counter()
    fingerprint = hashlib.sha256()
    row_digest = hashlib.sha256()
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    previous_time: int | None = None
    previous_sequence: int | None = None
    for index, row in enumerate(rows):
        if type(row) is not dict or set(row) != set(COLUMNS):
            raise ValueError(f"unexpected canonical fields at row {index}")
        for name in COLUMNS:
            field_types[name][_type_name(row[name])] += 1
        event = row["event_type"]
        if type(event) is not str or event not in EVENT_CODES:
            raise ValueError(f"noncanonical event type at row {index}")
        for name in INTEGER_FIELDS:
            value = row[name]
            if value is None and name == "side":
                continue
            if type(value) is not int:
                raise ValueError(f"{name} is not a native exact integer at row {index}")
            if abs(value) >= 2**53:
                raise ValueError(
                    f"{name} exceeds the frozen safe-integer bound at row {index}"
                )
            lower, upper = (-128, 127) if name == "side" else (-(2**63), 2**63 - 1)
            if not lower <= value <= upper:
                raise ValueError(f"{name} exceeds its target type at row {index}")
        timestamp, sequence = row["timestamp_ns"], row["sequence_number"]
        if (
            timestamp < 0
            or sequence < 0
            or (previous_time is not None and timestamp < previous_time)
            or (previous_sequence is not None and sequence <= previous_sequence)
        ):
            raise ValueError(f"canonical ordering failed at row {index}")
        side, price, quantity = row["side"], row["price_ticks"], row["quantity"]
        if event == "RESET":
            if side is not None or price != 0 or quantity != 0:
                raise ValueError(
                    f"RESET must have null side and zero fields at row {index}"
                )
            book = {1: {}, -1: {}}
        else:
            if side not in (1, -1) or price <= 0 or quantity <= 0:
                raise ValueError(f"invalid active liquidity at row {index}")
            before = book[side].get(price, 0)
            if event in ("SNAPSHOT", "ADD"):
                if event == "SNAPSHOT" and before:
                    raise ValueError(f"duplicate snapshot price at row {index}")
                book[side][price] = before + quantity
            else:
                if quantity > before:
                    raise ValueError(f"strict dictionary depth overdraw at row {index}")
                if quantity == before:
                    del book[side][price]
                else:
                    book[side][price] = before - quantity
            if book[1] and book[-1] and max(book[1]) >= min(book[-1]):
                raise ValueError(f"strict dictionary crossed book at row {index}")
        fingerprint.update(
            EVENT_STRUCT.pack(
                timestamp,
                sequence,
                EVENT_CODES[event],
                0 if side is None else side,
                price,
                quantity,
            )
        )
        row_digest.update(_json_bytes([row[name] for name in COLUMNS]))
        event_counts[event] += 1
        previous_time, previous_sequence = timestamp, sequence
    return {
        "row_count": len(rows),
        "event_stream_sha256": fingerprint.hexdigest(),
        "rows_sha256": row_digest.hexdigest(),
        "field_types": {
            name: dict(sorted(counts.items())) for name, counts in field_types.items()
        },
        "schema": SCHEMA,
        "event_type_counts": dict(sorted(event_counts.items())),
        "book": {
            "valid": True,
            "reconstructed_book": True,
            "event_count": len(rows),
            "final_bids": [
                [price, book[1][price]] for price in sorted(book[1], reverse=True)
            ],
            "final_asks": [[price, book[-1][price]] for price in sorted(book[-1])],
        },
        "checks": {
            "native_integer_types": True,
            "safe_integer_bound": True,
            "target_bounds": True,
            "reset_null_side": True,
            "ordered_stream": True,
            "strict_dictionary_reconstruction": True,
        },
    }


def _arrow_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("timestamp_ns", pa.int64()),
            pa.field("sequence_number", pa.int64()),
            pa.field("event_type", pa.string()),
            pa.field("side", pa.int8()),
            pa.field("price_ticks", pa.int64()),
            pa.field("quantity", pa.int64()),
        ]
    )


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    """Serialize already audited actual values; this is not a public writer."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if path.exists():
        raise ValueError("refusing to overwrite retained Parquet evidence")
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=_arrow_schema())
    pq.write_table(
        table,
        path,
        version="2.6",
        data_page_version="1.0",
        compression="zstd",
        use_dictionary=["event_type"],
        write_statistics=True,
        row_group_size=EVENT_COUNT,
        data_page_size=1048576,
    )


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    if not table.schema.equals(_arrow_schema(), check_metadata=True):
        raise ValueError("retained Parquet has an unexpected original schema")
    return table.to_pylist()


def _file(path: Path, directory: Path) -> dict[str, str]:
    return {"path": str(path.relative_to(directory)), "sha256": digest(path)}


def prepare_inputs(
    rows: list[dict[str, Any]], directory: str | Path, config: dict[str, Any]
) -> dict[str, Any]:
    """Write the three frozen representations of one generated, audited input."""
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("input directory must be new or empty")
    directory.mkdir(parents=True, exist_ok=True)
    audit = audit_rows(rows)
    _write_parquet(rows, directory / "canonical.parquet")
    for name, decimal in (("integer.csv", False), ("decimal.csv", True)):
        with (directory / name).open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow(
                    [
                        row[field]
                        if field == "event_type"
                        else ""
                        if row[field] is None
                        else str(row[field]) + (".0" if decimal else "")
                        for field in COLUMNS
                    ]
                )
    (directory / "normalized-config.json").write_bytes(_json_bytes(config))
    manifest = {
        "format_version": 1,
        "files": {
            name: _file(directory / name, directory)
            for name in (
                "canonical.parquet",
                "integer.csv",
                "decimal.csv",
                "normalized-config.json",
            )
        },
        "audit": audit,
        "config": config,
    }
    _, observed = read_inputs(directory)
    if observed != manifest:
        raise ValueError("prepared physical files differ from original generated rows")
    return manifest


def _lexical_integer(token: str, decimal: bool) -> int:
    match = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?", token)
    if match is None:
        raise ValueError("CSV original token is not finite decimal text")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    power = int(exponent or "0") - len(fraction)
    if abs(power) > 1000:
        raise ValueError("CSV token exceeds the frozen bounded exponent")
    coefficient = int(whole + fraction) * (-1 if sign == "-" else 1)
    value = (
        Fraction(coefficient * 10**power)
        if power >= 0
        else Fraction(coefficient, 10**-power)
    )
    if value.denominator != 1:
        raise ValueError("CSV token is not exactly integral")
    result = value.numerator
    if token != str(result) + (".0" if decimal else ""):
        raise ValueError("CSV encoding differs from its frozen integer/.0 format")
    return result


def _read_csv(path: Path, decimal: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(COLUMNS):
            raise ValueError("original CSV column order differs")
        for raw in reader:
            if set(raw) != set(COLUMNS) or any(value is None for value in raw.values()):
                raise ValueError("original CSV has missing/extra fields")
            rows.append(
                {
                    field: raw[field]
                    if field == "event_type"
                    else None
                    if field == "side" and raw[field] == ""
                    else _lexical_integer(raw[field], decimal)
                    for field in COLUMNS
                }
            )
    return rows


def read_inputs(
    directory: str | Path, expected_manifest: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read only: rehash and independently decode every original input row."""
    directory = Path(directory)
    rows = _read_parquet(directory / "canonical.parquet")
    audit = audit_rows(rows)
    for name, decimal in (("integer.csv", False), ("decimal.csv", True)):
        decoded = _read_csv(directory / name, decimal)
        other = audit_rows(decoded)
        if decoded != rows or other != audit:
            raise ValueError("original physical input representations differ")
    config = json.loads((directory / "normalized-config.json").read_text())
    manifest = {
        "format_version": 1,
        "files": {
            name: _file(directory / name, directory)
            for name in (
                "canonical.parquet",
                "integer.csv",
                "decimal.csv",
                "normalized-config.json",
            )
        },
        "audit": audit,
        "config": config,
    }
    if expected_manifest is not None and manifest != expected_manifest:
        raise ValueError("retained input manifest or physical bytes changed")
    return rows, manifest


def _event_rows(value: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    if type(value) is not list or len(value) != EVENT_COUNT:
        raise ValueError("public event result must be a list of 100000 events")
    original_types: dict[str, Counter[str]] = {name: Counter() for name in COLUMNS}
    rows: list[dict[str, Any]] = []
    for index, event in enumerate(value):
        raw = {name: getattr(event, name) for name in COLUMNS}
        for name, scalar in raw.items():
            original_types[name][_type_name(scalar)] += 1
        for field in ("timestamp_ns", "sequence_number", "price_ticks", "quantity"):
            if type(raw[field]) is not int:
                raise ValueError(
                    f"original returned {field} is not a native int at row {index}"
                )
        kind = raw["event_type"]
        side = raw["side"]
        if (
            not isinstance(kind, Enum)
            or type(kind.value) is not str
            or kind.value not in EVENT_CODES
        ):
            raise ValueError(
                f"original returned event_type is not the enum value at row {index}"
            )
        if side is not None and (
            not isinstance(side, Enum)
            or type(side.value) is not int
            or side.value not in (1, -1)
        ):
            raise ValueError(
                f"original returned side is not the exact enum at row {index}"
            )
        rows.append(
            {
                **raw,
                "event_type": kind.value,
                "side": None if side is None else side.value,
            }
        )
    return rows, {
        name: dict(sorted(counts.items())) for name, counts in original_types.items()
    }


def audit_returned(
    value: Any,
    expected_rows: list[dict[str, Any]],
    parquet_path: str | Path | None = None,
) -> dict[str, Any]:
    """Audit original return types and values before optional audit serialization."""
    if type(value) is list:
        rows, original_types = _event_rows(value)
        representation = "events"
    else:
        if (
            type(value).__module__ != "polars.dataframe.frame"
            or type(value).__name__ != "DataFrame"
        ):
            raise ValueError("unexpected public return type")
        actual_schema = [[name, str(dtype)] for name, dtype in value.schema.items()]
        if actual_schema != SCHEMA:
            raise ValueError(
                "actual returned frame schema differs before serialization"
            )
        rows = value.to_dicts()
        original_types = None
        representation = "frame"
    result = audit_rows(rows)
    if rows != expected_rows:
        raise ValueError("actual returned full rows/order differ from the frozen input")
    result["checks"]["full_rows_equal"] = True
    result.update(representation=representation, original_field_types=original_types)
    if parquet_path is not None:
        path = Path(parquet_path)
        _write_parquet(rows, path)
        if _read_parquet(path) != rows:
            raise ValueError(
                "actual audit Parquet changed already validated returned rows"
            )
        result.update(
            parquet_path=str(path),
            parquet_sha256=digest(path),
            serialization="Independent PyArrow audit of actual returned values outside timing; public function did not write this file",
        )
    return result


def audit_parquet(
    path: str | Path, expected_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Read only: validate all physically retained timed output rows."""
    path = Path(path)
    rows = _read_parquet(path)
    result = audit_rows(rows)
    if rows != expected_rows:
        raise ValueError("retained actual output full rows/order differ")
    result["checks"]["full_rows_equal"] = True
    result.update(
        representation="parquet", parquet_path=str(path), parquet_sha256=digest(path)
    )
    return result


def _fraction(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def summarize(
    records: list[dict[str, Any]], contract: dict[str, Any]
) -> dict[str, Any]:
    """Recompute fixed paired wall arithmetic, without timing or app calls."""
    if (
        len(records) != 40
        or [
            {key: record[key] for key in ("operation", "pair", "role", "sample")}
            for record in records
        ]
        != contract["schedule"]
    ):
        raise ValueError(
            "normal records do not match the entire frozen 40-worker schedule"
        )
    for record in records:
        if (
            record["status"] != "complete"
            or type(record["wall_ns"]) is not int
            or record["wall_ns"] <= 0
            or type(record["cpu_ns"]) is not int
            or record["cpu_ns"] < 0
        ):
            raise ValueError("normal raw duration/status is invalid")
    operations: dict[str, Any] = {}
    for operation in contract["operations"]:
        pairs = []
        ratios: list[Fraction] = []
        deltas: list[int] = []
        walls: dict[str, list[int]] = {"main": [], "pr11": []}
        for pair in range(1, 6):
            selected = {
                record["role"]: record
                for record in records
                if record["operation"] == operation and record["pair"] == pair
            }
            if set(selected) != {"main", "pr11"}:
                raise ValueError("missing exact paired source records")
            main, candidate = selected["main"], selected["pr11"]
            ratio = Fraction(candidate["wall_ns"], main["wall_ns"])
            delta = candidate["wall_ns"] - main["wall_ns"]
            ratios.append(ratio)
            deltas.append(delta)
            for role in walls:
                walls[role].append(selected[role]["wall_ns"])
            pairs.append(
                {
                    "pair": pair,
                    "main_sample": main["sample"],
                    "pr11_sample": candidate["sample"],
                    "main_wall_ns": main["wall_ns"],
                    "pr11_wall_ns": candidate["wall_ns"],
                    "ratio": _fraction(ratio),
                    "delta_ns": delta,
                }
            )
        median = sorted(ratios)[2]
        median_delta = sorted(deltas)[2]
        material = (
            median >= Fraction(3, 2)
            and all(ratio >= Fraction(6, 5) for ratio in ratios)
            and median_delta >= 50000000
        )
        low = median <= Fraction(5, 4) and all(
            ratio <= Fraction(3, 2) for ratio in ratios
        )
        operations[operation] = {
            "pairs": pairs,
            "median_ratio": _fraction(median),
            "minimum_ratio": _fraction(min(ratios)),
            "maximum_ratio": _fraction(max(ratios)),
            "median_delta_ns": median_delta,
            "minimum_delta_ns": min(deltas),
            "maximum_delta_ns": max(deltas),
            "wall_ns": {
                role: {
                    "minimum": min(values),
                    "median": sorted(values)[2],
                    "maximum": max(values),
                }
                for role, values in walls.items()
            },
            "material_cost": material,
            "low_overhead": low,
            "classification": "material_cost"
            if material
            else "low_overhead_band"
            if low
            else "indeterminate",
        }
    return {
        "sample_count": 40,
        "pair_count_per_operation": 5,
        "operations": operations,
        "primary_operation": PRIMARY,
        "primary_material_cost": operations[PRIMARY]["material_cost"],
        "primary_classification": operations[PRIMARY]["classification"],
        "profile_allowed": operations[PRIMARY]["material_cost"],
        "raw_samples": [
            {
                key: record[key]
                for key in ("sample", "pair", "role", "operation", "wall_ns", "cpu_ns")
            }
            for record in records
        ],
    }
