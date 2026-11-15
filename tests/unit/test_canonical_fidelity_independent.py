"""Frozen canonical-boundary corpus and independently derived exact evidence.

No case is evaluated on import. The corpus node durably records every public
observation before asserting; the ordinary exposed control is a separate node.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from decimal import Decimal, getcontext
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

import polars as pl

import lobmm.data.loaders as loaders
import lobmm.data.schema as schema
import lobmm.validation as validation
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import MarketEvent

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "docs/evidence/canonical-fidelity-20261113/"
FIXTURES_SHA = "bbd59beaae6feb0cac0d0cf965ace94b055b113b984d8e505a008f00bbe9a3e7"
COLUMNS = (
    "timestamp_ns",
    "sequence_number",
    "event_type",
    "side",
    "price_ticks",
    "quantity",
)
INTEGER_FIELDS = ("timestamp_ns", "sequence_number", "price_ticks", "quantity", "side")
NATIVE_SCHEMA = {
    "timestamp_ns": pl.Int64,
    "sequence_number": pl.Int64,
    "event_type": pl.String,
    "side": pl.Int8,
    "price_ticks": pl.Int64,
    "quantity": pl.Int64,
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        parts = value.as_tuple()
        return {
            "decimal": str(value),
            "tuple": [parts.sign, list(parts.digits), parts.exponent],
        }
    if isinstance(value, float) and not math.isfinite(value):
        return {"float_nonfinite": str(value)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(value), indent=2, allow_nan=False) + "\n")


def load_spec() -> dict[str, Any]:
    path = ROOT / PREFIX / "fixtures.json"
    if digest(path) != FIXTURES_SHA:
        raise ValueError("frozen canonical fidelity fixtures changed")
    return json.loads(path.read_text())


def provenance() -> dict[str, Any]:
    return {
        "python": sys.version,
        "test_path": str(Path(__file__).resolve()),
        "test_sha256": digest(Path(__file__).resolve()),
        "fixtures_sha256": FIXTURES_SHA,
        "contract_sha256": digest(ROOT / PREFIX / "contract.json"),
        "sources": {
            name: {
                "path": str(Path(module.__file__).resolve()),
                "sha256": digest(Path(module.__file__).resolve()),
            }
            for name, module in {
                "data/schema": schema,
                "data/loaders": loaders,
                "validation": validation,
            }.items()
        },
    }


def lexical_fraction(text: str) -> Fraction:
    """Parse bounded original finite text without production or Decimal math."""
    match = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?", text)
    if match is None:
        raise ValueError("not finite decimal text")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    coefficient = int(whole + fraction) * (-1 if sign == "-" else 1)
    power = int(exponent or "0") - len(fraction)
    if abs(power) > 1000:
        raise ValueError("outside frozen bounded oracle exponent")
    return (
        Fraction(coefficient * 10**power)
        if power >= 0
        else Fraction(coefficient, 10**-power)
    )


def exact_integer(value: Any, *, route: str) -> int:
    if type(value) is bool:
        return 1 if value else 0
    if type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        numerator, denominator = value.as_integer_ratio()
        exact = Fraction(numerator, denominator)
    elif isinstance(value, Decimal):
        exact = lexical_fraction(str(value))
    elif isinstance(value, str):
        if route == "read_csv":
            exact = lexical_fraction(value)
        else:
            if re.fullmatch(r"[+-]?\d+", value.strip()) is None:
                raise ValueError("not integer-string grammar")
            return int(value)
    else:
        raise ValueError("unsupported provided scalar")
    if exact.denominator != 1:
        raise ValueError("nonintegral")
    return exact.numerator


def integer_oracle(rows: list[dict[str, Any]], route: str) -> dict[str, Any]:
    normalized = deepcopy(rows)
    for index, row in enumerate(rows):
        for field in INTEGER_FIELDS:
            value = row[field]
            if value is None and field == "side":
                continue
            try:
                exact = exact_integer(value, route=route)
                if route != "mapping":
                    lower, upper = (
                        (-128, 127) if field == "side" else (-(2**63), 2**63 - 1)
                    )
                    if not lower <= exact <= upper:
                        raise ValueError("outside target integer range")
                normalized[index][field] = exact
            except ValueError as error:
                return {
                    "status": "reject",
                    "rows": None,
                    "rejection_field": field,
                    "rejection_row": index,
                    "reason": str(error),
                }
    return {
        "status": "admit",
        "rows": normalized,
        "rejection_field": None,
        "rejection_row": None,
        "reason": None,
    }


def dictionary_reconstruction(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict dictionary replay, independent of production books and adapters."""
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    trace: list[dict[str, Any]] = []
    previous_time: int | None = None
    for index, row in enumerate(rows):
        timestamp = row["timestamp_ns"]
        if (
            type(timestamp) is not int
            or timestamp < 0
            or (previous_time is not None and timestamp < previous_time)
        ):
            raise ValueError("independent timestamp contract")
        if type(row["sequence_number"]) is not int or row["sequence_number"] != index:
            raise ValueError("independent sequence contract")
        kind, side, price, quantity = (
            row[key] for key in ("event_type", "side", "price_ticks", "quantity")
        )
        if kind == "RESET":
            if side is not None or price != 0 or quantity != 0:
                raise ValueError("independent reset contract")
            book = {1: {}, -1: {}}
        else:
            if (
                type(side) is not int
                or side not in (1, -1)
                or type(price) is not int
                or price <= 0
                or type(quantity) is not int
                or quantity <= 0
            ):
                raise ValueError("independent liquidity contract")
            before = book[side].get(price, 0)
            if kind in ("SNAPSHOT", "ADD"):
                if kind == "SNAPSHOT" and before:
                    raise ValueError("independent duplicate snapshot")
                book[side][price] = before + quantity
            elif kind in ("CANCEL", "TRADE"):
                if quantity > before:
                    raise ValueError("independent overdraw")
                if quantity == before:
                    del book[side][price]
                else:
                    book[side][price] = before - quantity
            else:
                raise ValueError("independent event type")
            if book[1] and book[-1] and max(book[1]) >= min(book[-1]):
                raise ValueError("independent crossed book")
        trace.append(
            {
                "sequence_number": index,
                "bids": [
                    [price, book[1][price]] for price in sorted(book[1], reverse=True)
                ],
                "asks": [[price, book[-1][price]] for price in sorted(book[-1])],
            }
        )
        previous_time = timestamp
    return {
        "valid": bool(rows),
        "event_count": len(rows),
        "trace": trace,
        "final_book": trace[-1] if trace else None,
    }


def make_rows(case: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = deepcopy(spec["base_rows"])
    kind = case["kind"]
    if kind in {
        "fractional_float",
        "fractional_text",
        "fractional_decimal",
        "nonfinite",
        "out_of_range",
    }:
        text = case["value"]
        value = (
            float(text)
            if kind in {"fractional_float", "nonfinite"}
            else Decimal(text)
            if kind == "fractional_decimal"
            else int(text)
            if kind == "out_of_range"
            else text
        )
        rows[case["row"]][case["field"]] = value
    elif kind in {"integral_float", "integral_decimal", "integer_strings"}:
        for row in rows:
            for field in INTEGER_FIELDS:
                if row[field] is not None:
                    row[field] = (
                        float(row[field])
                        if kind == "integral_float"
                        else Decimal(str(row[field]))
                        if kind == "integral_decimal"
                        else str(row[field])
                    )
    elif kind in {"integer_epoch", "int64_bound"}:
        start = 1700000000000000001 if kind == "integer_epoch" else 2**63 - 6
        for index, row in enumerate(rows):
            row["timestamp_ns"] = start + index
    elif kind == "integral_decimal_text":
        for row, token in zip(
            rows, ("0.0", "0e0", "0.00", "1e0", "2.0", "3.000"), strict=True
        ):
            row["timestamp_ns"] = token
    elif kind in {"mixed_epoch_fraction", "mixed_integral_epoch"}:
        for row, value in zip(rows, case["timestamps"], strict=True):
            row["timestamp_ns"] = (
                float(value.removeprefix("float:"))
                if isinstance(value, str) and value.startswith("float:")
                else value
            )
    elif kind != "ordinary":
        raise ValueError("unknown frozen canonical input kind")
    return rows


def raw_frame(rows: list[dict[str, Any]], case: dict[str, Any]) -> pl.DataFrame:
    columns: list[pl.Series] = []
    for name in COLUMNS:
        values = [row[name] for row in rows]
        dtype: Any = NATIVE_SCHEMA[name]
        if case["kind"] == "integral_float" and name in INTEGER_FIELDS:
            dtype = pl.Float64
        elif case["kind"] == "integral_decimal" and name in INTEGER_FIELDS:
            dtype = pl.Decimal(precision=38, scale=1)
        elif case["kind"] == "integer_strings" and name in INTEGER_FIELDS:
            dtype = pl.String
        elif name == case.get("field"):
            if case["kind"] in {"fractional_float", "nonfinite"}:
                dtype = pl.Float64
                values = [None if value is None else float(value) for value in values]
            elif case["kind"] == "fractional_decimal":
                dtype = pl.Decimal(precision=38, scale=1)
                values = [
                    None if value is None else Decimal(str(value)) for value in values
                ]
            elif case["kind"] == "out_of_range":
                dtype = pl.UInt64
        columns.append(pl.Series(name, values, dtype=dtype, strict=True))
    return pl.DataFrame(columns)


def csv_content(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def context_snapshot() -> dict[str, Any]:
    context = getcontext()
    return {
        "precision": context.prec,
        "rounding": context.rounding,
        "Emin": context.Emin,
        "Emax": context.Emax,
        "capitals": context.capitals,
        "clamp": context.clamp,
        "traps": {
            signal.__name__: enabled for signal, enabled in context.traps.items()
        },
        "flags": {
            signal.__name__: enabled for signal, enabled in context.flags.items()
        },
    }


def error_record(error: Exception, stage: str) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "module": type(error).__module__,
        "message": str(error),
        "stage": stage,
    }


def file_record(path: Path, evidence: Path) -> dict[str, str]:
    return {"path": str(path.relative_to(evidence)), "sha256": digest(path)}


def input_record(
    rows: list[dict[str, Any]],
    frame: pl.DataFrame | None,
    events: list[MarketEvent] | None,
) -> dict[str, Any]:
    result = {
        "rows": plain(rows),
        "types": [
            {key: type(value).__name__ for key, value in row.items()} for row in rows
        ],
        "schema": None
        if frame is None
        else [[name, str(dtype)] for name, dtype in frame.schema.items()],
    }
    result["float_details"] = [
        {
            key: {
                "hex": value.hex(),
                "integer_ratio": list(value.as_integer_ratio())
                if math.isfinite(value)
                else None,
            }
            for key, value in row.items()
            if isinstance(value, float)
        }
        for row in rows
    ]
    if events is not None:
        result["event_dicts_before_inference"] = [
            plain(event.as_dict()) for event in events
        ]
        result["event_field_types"] = [
            {key: type(value).__name__ for key, value in event.as_dict().items()}
            for event in events
        ]
    return result


def supplied_events(rows: list[dict[str, Any]]) -> list[MarketEvent]:
    return [
        MarketEvent(
            timestamp_ns=row["timestamp_ns"],
            sequence_number=row["sequence_number"],
            event_type=EventType(row["event_type"]),
            side=None if row["side"] is None else Side(row["side"]),
            price_ticks=row["price_ticks"],
            quantity=row["quantity"],
        )
        for row in rows
    ]


def exact_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            pl.Series(
                name,
                [row[name] for row in rows],
                dtype=NATIVE_SCHEMA[name],
                strict=True,
            )
            for name in COLUMNS
        ]
    )


def validation_record(result: Any) -> dict[str, Any]:
    return {
        "valid": result.valid,
        "mode": result.mode.value,
        "reconstructed_book": result.reconstructed_book,
        "required_nonempty": result.required_nonempty,
        "issue_count": result.issue_count,
        "event_count": result.event_count,
        "event_stream_sha256": result.event_stream_sha256,
        "diagnostics": dict(result.diagnostics),
        "issues": [plain(issue) for issue in result.issues],
        "final_book": plain(result.final_book),
    }


def capture_result(
    frame: pl.DataFrame,
    events: list[MarketEvent] | None,
    directory: Path,
    evidence: Path,
) -> dict[str, Any]:
    path = directory / "canonical.parquet"
    frame.write_parquet(path)
    rows = frame.to_dicts()
    result: dict[str, Any] = {
        "rows": rows,
        "schema": [[name, str(dtype)] for name, dtype in frame.schema.items()],
        "parquet_path": str(path.relative_to(evidence)),
        "parquet_sha256": digest(path),
        "event_stream_sha256": None if events is None else event_stream_sha256(events),
        "validation": None,
        "dictionary_reconstruction": None,
        "validation_error": None,
        "dictionary_error": None,
    }
    try:
        result["dictionary_reconstruction"] = dictionary_reconstruction(rows)
    except Exception as error:
        result["dictionary_error"] = error_record(
            error, "independent_dictionary_reconstruction"
        )
    if events is not None:
        certify_result(result, events)
    return result


def certify_result(result: dict[str, Any], events: list[MarketEvent]) -> None:
    result["event_stream_sha256"] = event_stream_sha256(events)
    try:
        result["validation"] = validation_record(
            validation.validate_event_stream(
                events,
                mode=ValidationMode.STRICT,
                reconstruct_book=True,
                require_nonempty=True,
            )
        )
    except Exception as error:
        result["validation_error"] = error_record(error, "strict_validation")


def is_strict_valid(result: dict[str, Any] | None) -> bool:
    if (
        result is None
        or result["validation"] is None
        or result["dictionary_reconstruction"] is None
    ):
        return False
    certificate = result["validation"]
    return bool(
        certificate["valid"]
        and certificate["mode"] == "strict"
        and certificate["reconstructed_book"]
        and certificate["required_nonempty"]
        and certificate["issue_count"] == 0
        and result["dictionary_reconstruction"]["valid"]
    )


def public_observation(
    case: dict[str, Any], spec: dict[str, Any], evidence: Path
) -> dict[str, Any]:
    directory = evidence / "cases" / case["id"]
    directory.mkdir(parents=True, exist_ok=True)
    rows = make_rows(case, spec)
    frame: pl.DataFrame | None = None
    events: list[MarketEvent] | None = None
    route = case["route"]
    sources: dict[str, Path] = {}
    if route == "read_csv":
        source = directory / "input.csv"
        source.write_text(csv_content(rows))
        rows = [
            {
                key: None if value == "" and key == "side" else value
                for key, value in row.items()
            }
            for row in csv.DictReader(io.StringIO(source.read_text()))
        ]
        sources["csv"] = source
    elif route == "events_to_frame":
        events = supplied_events(rows)
    elif route != "mapping":
        frame = raw_frame(rows, case)
        rows = frame.to_dicts()
        source = directory / "input.parquet"
        frame.write_parquet(source)
        sources["parquet"] = source
    expected = integer_oracle(rows, route)
    if expected["status"] != case["expect"]:
        raise ValueError("independent integer oracle disagrees with frozen case status")
    if expected["status"] == "admit":
        expected["dictionary_reconstruction"] = dictionary_reconstruction(
            expected["rows"]
        )
    else:
        expected["dictionary_reconstruction"] = None
    before = input_record(rows, frame, events)
    source_before = {key: file_record(path, evidence) for key, path in sources.items()}
    writer = directory / ("written.csv" if route == "write_csv" else "written.parquet")
    observed: dict[str, Any] = {
        "case_id": case["id"],
        "input": deepcopy(case),
        "route": route,
        "expected": expected,
        "result": None,
        "error": None,
        "infrastructure_error": None,
        "raw_input": before,
        "source_files": source_before,
        "output_files": {},
        "boundary_returned": False,
        "context_before": context_snapshot(),
        "guard_before": sys.get_int_max_str_digits(),
    }

    def invoke(operation: Callable[[], Any], stage: str) -> tuple[bool, Any]:
        try:
            return True, operation()
        except Exception as error:
            observed["error"] = error_record(error, stage)
            return False, None

    actual: pl.DataFrame | None = None
    decoded: list[MarketEvent] | None = None
    if route == "mapping":
        ok, decoded = invoke(
            lambda: [
                schema.event_from_mapping(row, row_index=index)
                for index, row in enumerate(rows)
            ],
            "event_from_mapping",
        )
        if ok:
            observed["boundary_returned"] = True
            actual = exact_frame([event.as_dict() for event in decoded])
    elif route == "frame":
        ok, decoded = invoke(lambda: schema.frame_to_events(frame), "frame_to_events")
        if ok:
            observed["boundary_returned"] = True
            actual = exact_frame([event.as_dict() for event in decoded])
    elif route == "events_to_frame":
        ok, actual = invoke(lambda: schema.events_to_frame(events), "events_to_frame")
        observed["boundary_returned"] = ok
    elif route in {"read_csv", "read_parquet"}:
        ok, actual = invoke(
            lambda: (
                loaders.read_csv(sources["csv"])
                if route == "read_csv"
                else loaders.read_parquet(sources["parquet"])
            ),
            route,
        )
        observed["boundary_returned"] = ok
    elif route in {"write_csv", "write_parquet"}:
        ok, _ = invoke(
            lambda: (
                loaders.write_csv(frame, writer)
                if route == "write_csv"
                else loaders.write_parquet(frame, writer)
            ),
            route,
        )
        observed["boundary_returned"] = ok
        if ok:
            _, actual = invoke(
                lambda: (
                    loaders.read_csv(writer)
                    if route == "write_csv"
                    else loaders.read_parquet(writer)
                ),
                "writer_readback",
            )
    else:
        raise ValueError("unknown frozen public route")
    if actual is not None:
        observed["result"] = capture_result(actual, None, directory, evidence)
        observed["output_files"]["canonical_parquet"] = file_record(
            directory / "canonical.parquet", evidence
        )
        if decoded is None:
            _, decoded = invoke(
                lambda: schema.frame_to_events(actual), "frame_to_events_after_boundary"
            )
        if decoded is None:
            observed["result"]["validation_error"] = observed["error"]
        else:
            certify_result(observed["result"], decoded)
    if writer.exists():
        observed["output_files"]["writer_" + route.removeprefix("write_")] = (
            file_record(writer, evidence)
        )
    observed["writer_output_exists"] = (
        writer.exists() if route.startswith("write_") else None
    )
    observed["raw_input_after"] = input_record(
        rows if frame is None else frame.to_dicts(), frame, events
    )
    observed["source_files_after"] = {
        key: file_record(path, evidence) for key, path in sources.items()
    }
    observed["context_after"] = context_snapshot()
    observed["guard_after"] = sys.get_int_max_str_digits()
    result = observed["result"]
    observed["strict_valid"] = is_strict_valid(result)
    error = observed["error"]
    domain = (
        "DataLoadError"
        if route in {"read_csv", "read_parquet", "write_csv", "write_parquet"}
        else "DataSchemaError"
    )
    matched = (
        (
            observed["strict_valid"]
            and result["rows"] == expected["rows"]
            and result["dictionary_reconstruction"]
            == expected["dictionary_reconstruction"]
        )
        if expected["status"] == "admit"
        else (
            error is not None
            and error["type"] == domain
            and expected["rejection_field"] in error["message"]
            and not observed["boundary_returned"]
            and result is None
        )
    )
    observed["status"] = (
        "admitted"
        if result is not None
        else "rejected"
        if error is not None and error["type"] == domain
        else "error"
    )
    observed["checks"] = {
        "oracle_match": bool(matched),
        "input_unchanged": before == observed["raw_input_after"]
        and source_before == observed["source_files_after"],
        "context_preserved": observed["context_before"] == observed["context_after"],
        "guard_preserved": observed["guard_before"] == observed["guard_after"],
        "rejected_writer_absent": not (
            route.startswith("write_") and case["expect"] == "reject"
        )
        or not observed["writer_output_exists"],
    }
    return observed


def evidence_root(tmp_path: Path) -> Path:
    configured = os.environ.get("LOB_CANONICAL_FIDELITY_EVIDENCE_DIR")
    path = Path(configured).resolve() if configured else tmp_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_corpus(evidence: Path) -> dict[str, Any]:
    spec = load_spec()
    path = evidence / "corpus.json"
    if path.exists():
        raise ValueError("refusing to overwrite canonical corpus evidence")
    record: dict[str, Any] = {
        "observations": [],
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    for case in spec["cases"]:
        try:
            observed = public_observation(case, spec, evidence)
        except Exception as error:
            observed = {
                "case_id": case["id"],
                "input": deepcopy(case),
                "route": case["route"],
                "status": "infrastructure_error",
                "error": None,
                "infrastructure_error": error_record(error, "setup_or_evidence"),
                "result": None,
                "checks": {"infrastructure_ok": False},
            }
        record["observations"].append(observed)
        write_json(path, record)
    record["complete"] = True
    write_json(path, record)
    return record


def run_normal_control(evidence: Path) -> dict[str, Any]:
    spec = load_spec()
    path = evidence / "normal-control.json"
    if path.exists():
        raise ValueError("refusing to overwrite the canonical normal control")
    record: dict[str, Any] = {
        "observation": None,
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    directory = evidence / "normal"
    directory.mkdir(parents=True, exist_ok=True)
    observed: dict[str, Any] = {
        "case_id": "normal_six_event_control",
        "result": None,
        "error": None,
        "infrastructure_error": None,
        "context_before": context_snapshot(),
        "guard_before": sys.get_int_max_str_digits(),
        "output_files": {},
    }
    try:
        rows = deepcopy(spec["base_rows"])
        expected = integer_oracle(rows, "frame")
        original = exact_frame(rows)
        before = input_record(rows, original, None)
        outputs = {
            "csv": directory / "canonical.csv",
            "parquet": directory / "written.parquet",
        }
        for key, target in outputs.items():
            if key == "csv":
                loaders.write_csv(original, target)
            else:
                loaders.write_parquet(original, target)
            observed["output_files"][key] = file_record(target, evidence)
        decoded = schema.frame_to_events(original)
        observed["result"] = capture_result(original, decoded, directory, evidence)
        observed["raw_input"] = before
        observed["raw_input_after"] = input_record(original.to_dicts(), original, None)
        observed["expected"] = expected
        observed["strict_valid"] = is_strict_valid(observed["result"])
        observed["checks"] = {
            "oracle_match": observed["strict_valid"]
            and observed["result"]["rows"] == expected["rows"]
            and observed["result"]["dictionary_reconstruction"]
            == dictionary_reconstruction(expected["rows"]),
            "row_count": len(observed["result"]["rows"]) == 6,
            "input_unchanged": before == observed["raw_input_after"],
        }
        observed["status"] = "admitted"
    except Exception as error:
        observed["error"] = error_record(error, "normal_control")
        observed["infrastructure_error"] = observed["error"]
        observed["checks"] = {"normal_completed": False}
        observed["status"] = "error"
    observed["context_after"] = context_snapshot()
    observed["guard_after"] = sys.get_int_max_str_digits()
    observed["checks"].update(
        context_preserved=observed["context_before"] == observed["context_after"],
        guard_preserved=observed["guard_before"] == observed["guard_after"],
    )
    record["observation"] = observed
    record["complete"] = True
    write_json(path, record)
    return record


def test_frozen_canonical_corpus_records_every_case_before_asserting(
    tmp_path: Path,
) -> None:
    record = run_corpus(evidence_root(tmp_path))
    failed = [
        {
            "case_id": row["case_id"],
            "status": row["status"],
            "error": row["error"],
            "infrastructure_error": row["infrastructure_error"],
            "checks": row["checks"],
        }
        for row in record["observations"]
        if row["infrastructure_error"] is not None or not all(row["checks"].values())
    ]
    assert record["complete"]
    assert len(record["observations"]) == 33
    assert not failed, failed


def test_existing_six_event_control_preserves_canonical_contract(
    tmp_path: Path,
) -> None:
    record = run_normal_control(evidence_root(tmp_path))
    assert record["complete"]
    assert all(record["observation"]["checks"].values()), record
