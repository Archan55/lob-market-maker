"""Frozen paired-file LOBSTER fidelity oracle and durable observations.

Importing defines the corpus interface without evaluating a case. Each profile
records all twelve public conversions before any assertion. The owned fictional
inputs support adapter fidelity checks, not market or execution claims.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    FloatOperation,
    Inexact,
    Rounded,
    getcontext,
    localcontext,
)
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import lobmm.data.lobster as lobster_module
import lobmm.validation as validation_module
from lobmm.data import LOBSTERCSVAdapter, LOBSTERData
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.schema import frame_to_events
from lobmm.enums import ValidationMode

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "docs/evidence/lobster-fidelity-20261112/"
FIXTURES_SHA = "1987016a419c065535b2ac5590305e1966328c39e495feda8eb487dbf2dbf670"
PROFILE_IDS = ("default28", "reduced3", "reduced3_trapped")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
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


def load_spec(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(fixtures_path)
        if fixtures_path is not None
        else ROOT / PREFIX / "fixtures.json"
    )
    if digest(path) != FIXTURES_SHA:
        raise ValueError("frozen LOBSTER fidelity fixture hash changed")
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
                "data/lobster": lobster_module,
                "validation": validation_module,
            }.items()
        },
    }


def lexical_fraction(text: str) -> Fraction:
    """Read original finite decimal text using integer arithmetic only."""
    match = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?", text)
    if match is None:
        raise ValueError("unsupported finite oracle lexeme")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    coefficient = int(whole + fraction) * (-1 if sign == "-" else 1)
    power = int(exponent or "0") - len(fraction)
    if abs(power) > 1000:
        raise ValueError("oracle exponent exceeds the frozen bounded corpus")
    return (
        Fraction(coefficient * 10**power)
        if power >= 0
        else Fraction(coefficient, 10**-power)
    )


class OracleRejection(ValueError):
    """Independent field, reason and row for a semantic source rejection."""


def exact_integer(value: Fraction, reason: str) -> int:
    if value.denominator != 1:
        raise OracleRejection(reason)
    return value.numerator


def midnight_ns(configuration: dict[str, Any]) -> int:
    """Integer datetime epoch arithmetic; no float timestamp or adapter helper."""
    day = date.fromisoformat(configuration["trading_date"])
    local = datetime(
        day.year, day.month, day.day, tzinfo=ZoneInfo(configuration["timezone"])
    )
    delta = local.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000


def oracle_time(raw: str, row: int, start_ns: int) -> int:
    seconds = lexical_fraction(raw)
    if not 0 <= seconds < 86400:
        raise OracleRejection(f"time/[0, 86400)/row {row}")
    offset = exact_integer(
        seconds * 1_000_000_000, f"time/finer than one nanosecond/row {row}"
    )
    return start_ns + offset


def oracle_price(raw: str, configuration: dict[str, Any], field: str, row: int) -> int:
    value = lexical_fraction(raw)
    if value <= 0:
        raise OracleRejection(f"{field}/positive/row {row}")
    price = value / (
        lexical_fraction(configuration["lobster_price_scale"])
        * lexical_fraction(configuration["tick_size"])
    )
    return exact_integer(price, f"{field}/not aligned/row {row}")


def oracle_quantity(
    raw: str, configuration: dict[str, Any], field: str, row: int, *, allow_zero: bool
) -> int:
    value = lexical_fraction(raw)
    if value < 0 or (value == 0 and not allow_zero):
        raise OracleRejection(
            f"{field}/{'nonnegative' if allow_zero else 'positive'}/row {row}"
        )
    return exact_integer(
        value * lexical_fraction(configuration["quantity_multiplier"]),
        f"{field}/cannot be represented exactly/row {row}",
    )


def oracle_book(
    row: list[str], configuration: dict[str, Any], index: int
) -> dict[int, dict[int, int]]:
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    for offset in range(0, len(row), 4):
        for side, price_raw, quantity_raw in (
            (-1, row[offset], row[offset + 1]),
            (1, row[offset + 2], row[offset + 3]),
        ):
            field = "bid" if side == 1 else "ask"
            quantity = oracle_quantity(
                quantity_raw, configuration, field + "_size", index, allow_zero=True
            )
            if quantity == 0:
                continue
            price = oracle_price(price_raw, configuration, field + "_price", index)
            if price in book[side]:
                raise ValueError("duplicate price in frozen independent book")
            book[side][price] = quantity
    if book[1] and book[-1] and max(book[1]) >= min(book[-1]):
        raise ValueError("crossed frozen independent book")
    return book


def book_record(book: dict[int, dict[int, int]]) -> dict[str, Any]:
    return {
        "bids": [[price, book[1][price]] for price in sorted(book[1], reverse=True)],
        "asks": [[price, book[-1][price]] for price in sorted(book[-1])],
    }


def dictionary_reconstruction(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Independent strict finite dictionary replay, without production books."""
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
            raise ValueError("invalid independent replay timestamp")
        if type(row["sequence_number"]) is not int or row["sequence_number"] != index:
            raise ValueError("invalid independent replay sequence")
        event, side, price, quantity = (
            row[key] for key in ("event_type", "side", "price_ticks", "quantity")
        )
        if event == "RESET":
            if side is not None or price != 0 or quantity != 0:
                raise ValueError("invalid independent reset")
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
                raise ValueError("invalid independent liquidity event")
            before = book[side].get(price, 0)
            if event in ("SNAPSHOT", "ADD"):
                if event == "SNAPSHOT" and before:
                    raise ValueError("duplicate independent snapshot level")
                book[side][price] = before + quantity
            elif event in ("TRADE", "CANCEL"):
                if quantity > before:
                    raise ValueError("independent depth overdraw")
                if quantity == before:
                    del book[side][price]
                else:
                    book[side][price] = before - quantity
            else:
                raise ValueError("unknown independent event type")
            if book[1] and book[-1] and max(book[1]) >= min(book[-1]):
                raise ValueError("crossed independent replay book")
        trace.append({"sequence_number": index, "book": book_record(book)})
        previous_time = timestamp
    return {
        "valid": bool(rows),
        "event_count": len(rows),
        "trace": trace,
        "final_book": book_record(book),
    }


def paired_oracle(
    case: dict[str, Any], configuration: dict[str, Any]
) -> dict[str, Any]:
    """Independent oracle limited to the frozen one/two-row message designs."""
    specs: list[tuple[int, str, int | None, int, int]] = []
    timestamps: list[int] = []
    books: list[dict[int, dict[int, int]]] = []
    visible: list[list[int]] = []
    try:
        start = midnight_ns(configuration)
        for index, (message, raw_book) in enumerate(
            zip(case["messages"], case["orderbook"], strict=True)
        ):
            timestamp = oracle_time(message[0], index, start)
            if timestamps and timestamp < timestamps[-1]:
                raise OracleRejection(f"time regressed/row {index}")
            timestamps.append(timestamp)
            target = oracle_book(raw_book, configuration, index)
            books.append(target)
            side = int(message[5])
            if side not in (1, -1):
                raise ValueError("frozen visible direction must be a side")
            price = oracle_price(message[4], configuration, "price", index)
            quantity = oracle_quantity(
                message[3], configuration, "size", index, allow_zero=False
            )
            visible.append([side, price, quantity])
            if index == 0:
                specs.append((timestamp, "RESET", None, 0, 0))
                for resting in (1, -1):
                    for level in sorted(target[resting], reverse=resting == 1):
                        specs.append(
                            (
                                timestamp,
                                "SNAPSHOT",
                                resting,
                                level,
                                target[resting][level],
                            )
                        )
            else:
                if index != 1 or message[1] != "4" or side != 1 or quantity != 1:
                    raise ValueError("unexpected frozen two-row design")
                expected = deepcopy(books[0])
                if expected[side].get(price, 0) <= quantity:
                    raise ValueError("frozen execution must leave displayed depth")
                expected[side][price] -= quantity
                if target != expected:
                    raise ValueError("frozen paired book differs from causal execution")
                specs.append((timestamp, "TRADE", side, price, quantity))
        rows = [
            dict(
                timestamp_ns=timestamp,
                sequence_number=index,
                event_type=event,
                side=side,
                price_ticks=price,
                quantity=quantity,
            )
            for index, (timestamp, event, side, price, quantity) in enumerate(specs)
        ]
        diagnostics = {
            "source_rows": len(books),
            "snapshot_resets": 1,
            "snapshot_levels": len(books[0][1]) + len(books[0][-1]),
            "first_message_represented_by_snapshot": 1,
            "output_events": len(rows),
        }
        if len(books) == 2:
            diagnostics["causal_trade_events"] = 1
        return {
            "status": "admit",
            "rows": rows,
            "diagnostics": diagnostics,
            "error_reason": None,
            "timestamps": timestamps,
            "visible_details": visible,
            "books": [book_record(book) for book in books],
            "oracle_trace": dictionary_reconstruction(rows),
        }
    except OracleRejection as error:
        return {
            "status": "reject",
            "rows": None,
            "diagnostics": None,
            "error_reason": str(error),
            "timestamps": timestamps,
            "visible_details": visible,
            "books": [book_record(book) for book in books],
            "oracle_trace": None,
        }


def context_snapshot(context: Context) -> dict[str, Any]:
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


def profile_context(profile: dict[str, Any]) -> Context:
    context = Context(
        prec=profile["precision"],
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
    )
    if profile["rounding"] != "ROUND_HALF_EVEN":
        raise ValueError("unexpected frozen rounding profile")
    context.traps[Inexact] = profile["trap_inexact_rounded"]
    context.traps[Rounded] = profile["trap_inexact_rounded"]
    context.clear_flags()
    context.flags[FloatOperation] = True
    return context


def error_record(error: Exception) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "module": type(error).__module__,
        "message": str(error),
    }


def observe(profile: dict[str, Any], operation: Callable[[], Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "result": None,
        "error": None,
        "infrastructure_error": None,
        "outer_context_before": context_snapshot(getcontext()),
        "guard_before": sys.get_int_max_str_digits(),
    }
    with localcontext(profile_context(profile)) as context:
        result["context_before"] = context_snapshot(context)
        try:
            result["result"] = operation()
        except Exception as error:
            result["error"] = error_record(error)
        finally:
            result["context_after"] = context_snapshot(context)
    result["outer_context_after"] = context_snapshot(getcontext())
    result["guard_after"] = sys.get_int_max_str_digits()
    result["checks"] = {
        "context_preserved": result["context_before"] == result["context_after"],
        "outer_context_preserved": result["outer_context_before"]
        == result["outer_context_after"],
        "guard_preserved": result["guard_before"] == result["guard_after"],
    }
    return result


def csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode()


def validation_summary(result: Any) -> dict[str, Any]:
    return {
        "valid": result.valid,
        "issue_count": result.issue_count,
        "event_count": result.event_count,
        "event_stream_sha256": result.event_stream_sha256,
        "mode": result.mode.value,
        "reconstructed_book": result.reconstructed_book,
        "required_nonempty": result.required_nonempty,
        "issues": [plain(issue) for issue in result.issues],
        "diagnostics": dict(result.diagnostics),
        "final_book": plain(result.final_book),
    }


def persist_frame(
    frame: Any, adapter: Any, path: Path, evidence_root: Path
) -> dict[str, Any]:
    # Retain actual canonical output before either independent or public strict
    # validation, so even a surprising rejected certificate leaves raw evidence.
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    rows = frame.to_dicts()
    result: dict[str, Any] = {
        "rows": rows,
        "schema": [[name, str(dtype)] for name, dtype in frame.schema.items()],
        "parquet_path": str(path.relative_to(evidence_root)),
        "parquet_sha256": digest(path),
        "diagnostics": dict(adapter.last_diagnostics),
        "validation": None,
        "dictionary_reconstruction": None,
        "validation_error": None,
    }
    try:
        result["dictionary_reconstruction"] = dictionary_reconstruction(rows)
        events = frame_to_events(frame)
        result["event_stream_sha256"] = event_stream_sha256(events)
        result["validation"] = validation_summary(
            validation_module.validate_event_stream(
                events,
                mode=ValidationMode.STRICT,
                reconstruct_book=True,
                require_nonempty=True,
            )
        )
    except Exception as error:
        result["validation_error"] = error_record(error)
    return result


def boundary_wrappers(
    calls: list[dict[str, Any]],
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    originals = {
        name: getattr(LOBSTERCSVAdapter, name)
        for name in ("_timestamp_ns", "_add_level", "_visible_message_details")
    }

    def invoke(
        name: str,
        adapter: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        record: dict[str, Any],
    ) -> Any:
        calls.append(record)
        record.update(result=None, error=None)
        try:
            actual = originals[name](adapter, *args, **kwargs)
            record["result"] = plain(actual)
            return actual
        except Exception as error:
            record["error"] = error_record(error)
            raise

    def timestamp(adapter: Any, value: Any, *, row_index: int) -> Any:
        return invoke(
            "_timestamp_ns",
            adapter,
            (value,),
            {"row_index": row_index},
            {
                "helper": "_timestamp_ns",
                "row_index": row_index,
                "value": value,
                "type": type(value).__name__,
            },
        )

    def add_level(
        adapter: Any, levels: dict[int, int], ordered_prices: list[int], **kwargs: Any
    ) -> Any:
        record = {
            "helper": "_add_level",
            **plain(kwargs),
            "price_type": type(kwargs["price_raw"]).__name__,
            "size_type": type(kwargs["size_raw"]).__name__,
            "levels_before": [[price, quantity] for price, quantity in levels.items()],
            "ordered_before": list(ordered_prices),
        }
        try:
            return invoke(
                "_add_level", adapter, (levels, ordered_prices), kwargs, record
            )
        finally:
            record["levels_after"] = [
                [price, quantity] for price, quantity in levels.items()
            ]
            record["ordered_after"] = list(ordered_prices)

    def visible(adapter: Any, message_row: Any, *, row_index: int) -> Any:
        return invoke(
            "_visible_message_details",
            adapter,
            (message_row,),
            {"row_index": row_index},
            {
                "helper": "_visible_message_details",
                "row_index": row_index,
                "message_row": list(message_row),
                "types": [type(value).__name__ for value in message_row],
            },
        )

    return timestamp, add_level, visible


def raw_boundary_checks(
    case: dict[str, Any], expected: dict[str, Any], calls: list[dict[str, Any]]
) -> dict[str, bool]:
    times = [call for call in calls if call["helper"] == "_timestamp_ns"]
    levels = [call for call in calls if call["helper"] == "_add_level"]
    visible = [call for call in calls if call["helper"] == "_visible_message_details"]
    level_inputs = [
        (
            index,
            side,
            row[offset + (0 if side == -1 else 2)],
            row[offset + (1 if side == -1 else 3)],
        )
        for index, row in enumerate(case["orderbook"])
        for offset in range(0, len(row), 4)
        for side in (-1, 1)
    ]
    raw = bool(times) and all(
        call["row_index"] == index
        and call["value"] == case["messages"][index][0]
        and call["type"] == "str"
        for index, call in enumerate(times)
    )
    raw = (
        raw
        and len(levels) <= len(level_inputs)
        and all(
            (call["row_index"], call["side"], call["price_raw"], call["size_raw"])
            == level_inputs[index]
            and call["price_type"] == call["size_type"] == "str"
            for index, call in enumerate(levels)
        )
    )
    raw = raw and all(
        call["message_row"] == case["messages"][call["row_index"]]
        and all(kind == "str" for kind in call["types"])
        for call in visible
    )
    checks = {"raw_boundary_literals_preserved": raw}
    if expected["status"] != "admit":
        return checks
    complete = (
        len(times) == len(case["messages"])
        and len(levels) == len(level_inputs)
        and len(visible) == len(case["messages"])
    )
    results = complete and all(
        call["error"] is None and call["result"] == expected["timestamps"][index]
        for index, call in enumerate(times)
    )
    results = results and all(
        call["error"] is None
        and call["result"] == expected["visible_details"][call["row_index"]]
        for call in visible
    )
    dummy = all(
        call["error"] is None
        and call["result"] is None
        and (
            call["size_raw"] != "0"
            or (
                call["levels_before"] == call["levels_after"]
                and call["ordered_before"] == call["ordered_after"]
            )
        )
        for call in levels
    )
    checks.update(
        boundary_calls_complete=complete,
        boundary_results_exact=results,
        zero_size_prices_skipped=dummy,
    )
    return checks


def public_observation(
    profile: dict[str, Any],
    case: dict[str, Any],
    spec: dict[str, Any],
    evidence_root: Path,
) -> dict[str, Any]:
    directory = evidence_root / "groups" / profile["id"] / case["id"]
    directory.mkdir(parents=True, exist_ok=True)
    paths = {name: directory / (name + ".csv") for name in ("messages", "orderbook")}
    for name, path in paths.items():
        path.write_bytes(csv_bytes(case[name]))
    expected = paired_oracle(case, spec["configuration"])
    if (
        expected["status"] != case["expected"]
        or expected["error_reason"] != case["error_reason"]
    ):
        raise ValueError(
            "independent paired oracle disagrees with frozen case classification"
        )
    calls: list[dict[str, Any]] = []
    timestamp, add_level, visible = boundary_wrappers(calls)

    def operation() -> Any:
        adapter = LOBSTERCSVAdapter(**spec["configuration"], levels=case["levels"])
        with (
            patch.object(LOBSTERCSVAdapter, "_timestamp_ns", timestamp),
            patch.object(LOBSTERCSVAdapter, "_add_level", add_level),
            patch.object(LOBSTERCSVAdapter, "_visible_message_details", visible),
        ):
            frame = adapter.checked(
                LOBSTERData(messages=paths["messages"], orderbook=paths["orderbook"])
            )
        return persist_frame(
            frame, adapter, directory / "canonical.parquet", evidence_root
        )

    observed = observe(profile, operation)
    actual = observed["result"]
    strict_valid = bool(
        actual is not None
        and actual["validation"] is not None
        and actual["validation"]["valid"]
        and actual["validation"]["mode"] == "strict"
        and actual["validation"]["reconstructed_book"]
        and actual["validation"]["required_nonempty"]
        and actual["validation"]["issue_count"] == 0
        and actual["dictionary_reconstruction"]["valid"]
    )
    error = observed["error"]
    error_matches = (
        error is not None
        and error["type"] == "MarketDataAdapterError"
        and all(
            token in error["message"]
            for token in (expected["error_reason"] or "").split("/")
        )
    )
    oracle_matches = (
        (
            strict_valid
            and actual["rows"] == expected["rows"]
            and actual["diagnostics"] == expected["diagnostics"]
            and actual["dictionary_reconstruction"] == expected["oracle_trace"]
        )
        if expected["status"] == "admit"
        else error_matches
    )
    observed["checks"].update(
        oracle_match=bool(oracle_matches), **raw_boundary_checks(case, expected, calls)
    )
    observed.update(
        case_id=case["id"],
        input=deepcopy(case),
        expected=expected,
        status="admitted"
        if actual is not None
        else "rejected"
        if error is not None and error["type"] == "MarketDataAdapterError"
        else "error",
        canonical=actual,
        strict_valid=strict_valid,
        boundary_calls=calls,
        csv_paths={
            name: str(path.relative_to(evidence_root)) for name, path in paths.items()
        },
        csv_sha256={name: digest(path) for name, path in paths.items()},
    )
    return observed


def evidence_root(tmp_path: Path) -> Path:
    configured = os.environ.get("LOB_LOBSTER_FIDELITY_EVIDENCE_DIR")
    root = Path(configured).resolve() if configured else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_profile(
    profile_id: str, evidence: Path, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    spec = load_spec(fixtures_path)
    profile = next(item for item in spec["profiles"] if item["id"] == profile_id)
    path = evidence / "groups" / (profile_id + ".json")
    if path.exists():
        raise ValueError("refusing to overwrite a fidelity profile ledger")
    record: dict[str, Any] = {
        "profile": profile_id,
        "profile_spec": profile,
        "observations": [],
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    for case in spec["cases"]:
        try:
            observed = public_observation(profile, case, spec, evidence)
        except Exception as error:
            observed = {
                "case_id": case["id"],
                "input": deepcopy(case),
                "result": None,
                "error": None,
                "status": "infrastructure_error",
                "infrastructure_error": error_record(error),
                "checks": {"infrastructure_ok": False},
            }
        record["observations"].append(observed)
        write_json(path, record)
    record["complete"] = True
    write_json(path, record)
    return record


def run_normal_control(
    evidence: Path, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    spec = load_spec(fixtures_path)
    case = spec["normal_control"]
    profile = next(item for item in spec["profiles"] if item["id"] == case["profile"])
    path = evidence / "normal-controls.json"
    if path.exists():
        raise ValueError("refusing to overwrite the normal LOBSTER control")
    record: dict[str, Any] = {
        "observations": [],
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    try:
        contract = json.loads((ROOT / PREFIX / "contract.json").read_text())
        hashes = {
            case[name]: digest(ROOT / case[name]) for name in ("messages", "orderbook")
        }
        if hashes != contract["control_input_sha256"]:
            raise ValueError("original normal-control input bytes changed")
        inputs = {
            name: evidence / "normal" / (name + ".csv")
            for name in ("messages", "orderbook")
        }
        for name, target in inputs.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / case[name]).read_bytes())

        def operation() -> Any:
            config = {
                key: value
                for key, value in case.items()
                if key not in {"messages", "orderbook", "profile"}
            }
            adapter = LOBSTERCSVAdapter(**config)
            frame = adapter.checked(LOBSTERData(**inputs))
            return persist_frame(
                frame, adapter, evidence / "normal" / "canonical.parquet", evidence
            )

        observed = observe(profile, operation)
        actual = observed.pop("result")
        observed.update(
            id="lobster_existing",
            status="admitted" if actual is not None else "error",
            input_sha256=hashes,
            csv_paths={
                name: str(target.relative_to(evidence))
                for name, target in inputs.items()
            },
            csv_sha256={name: digest(target) for name, target in inputs.items()},
        )
        if actual is not None:
            observed.update(actual)
        observed["checks"].update(
            completed=observed["error"] is None and actual is not None,
            normal_row_count=actual is not None and len(actual["rows"]) == 17,
            strict_valid=actual is not None
            and actual["validation"] is not None
            and actual["validation"]["valid"]
            and actual["validation"]["mode"] == "strict"
            and actual["validation"]["reconstructed_book"]
            and actual["validation"]["required_nonempty"]
            and actual["dictionary_reconstruction"]["valid"],
        )
    except Exception as error:
        observed = {
            "id": "lobster_existing",
            "status": "infrastructure_error",
            "error": error_record(error),
            "checks": {"completed": False},
        }
    record["observations"].append(observed)
    record["complete"] = True
    write_json(path, record)
    return record


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_paired_csv_fidelity_records_all_cases_before_asserting(
    profile_id: str, tmp_path: Path
) -> None:
    record = run_profile(profile_id, evidence_root(tmp_path))
    failed = [
        {
            "case_id": row["case_id"],
            "error": row["error"],
            "checks": row["checks"],
            "infrastructure_error": row["infrastructure_error"],
        }
        for row in record["observations"]
        if row["infrastructure_error"] is not None or not all(row["checks"].values())
    ]
    assert record["complete"]
    assert len(record["observations"]) == 12
    assert not failed, failed


def test_existing_lobster_control_preserves_canonical_contract(tmp_path: Path) -> None:
    record = run_normal_control(evidence_root(tmp_path))
    assert record["complete"]
    assert len(record["observations"]) == 1
    assert all(record["observations"][0]["checks"].values()), record
