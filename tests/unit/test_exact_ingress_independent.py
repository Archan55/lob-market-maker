"""Frozen exact-ingress corpus, independent oracle and durable observations.

Nothing is evaluated on import. Each context group records all 40 calls before
asserting. Owned CSV witnesses exercise the public adapter and explicit strict
stream certification; neither their acceptance nor these controls proves data
rights, real execution, economics or a whole-adapter transaction guarantee.
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
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Context,
    Decimal,
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

import pytest

import lobmm.data._adapter_utils as adapter_utils
import lobmm.data.lobster as lobster_module
import lobmm.data.tardis as tardis_module
import lobmm.types as price_module
import lobmm.validation as validation_module
from lobmm.data import LOBSTERCSVAdapter, LOBSTERData, TardisCSVAdapter, TardisData
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.schema import frame_to_events
from lobmm.enums import ValidationMode

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "docs/evidence/exact-ingress-20261111/"
FIXTURES = PREFIX + "fixtures.json"
FIXTURES_SHA = "dbc8f57942b24a94a9fd67816d0e9d350c12cdfe35f40e35b2e5d1c545962144"
PROFILE_IDS = ("default28", "low3_even", "low3_up", "low3_down", "high50", "trapped28")
ROUNDING = {
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_UP": ROUND_UP,
    "ROUND_DOWN": ROUND_DOWN,
}
PRICE_HAND = {
    "compact_large_balanced": 1,
    "compact_small_balanced": 1,
    "compact_small_fraction": None,
}
QUANTITY_HAND = {
    "compact_balanced": 1,
    "compact_reversed": 1,
    "compact_fraction": None,
    "zero_compact_multiplier": 0,
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        parts = value.as_tuple()
        return {
            "decimal": str(value),
            "tuple": [parts.sign, list(parts.digits), parts.exponent],
        }
    if isinstance(value, Enum):
        return value.value
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


def load_spec(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(fixtures_path) if fixtures_path is not None else ROOT / FIXTURES
    if digest(path) != FIXTURES_SHA:
        raise ValueError("frozen exact-ingress fixture hash changed")
    return json.loads(path.read_text())


def provenance() -> dict[str, Any]:
    modules = {
        "types": price_module,
        "data/_adapter_utils": adapter_utils,
        "data/tardis": tardis_module,
        "data/lobster": lobster_module,
        "validation": validation_module,
    }
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
            for name, module in modules.items()
        },
    }


def lexical_fraction(text: str) -> Fraction:
    """Parse the bounded ordinary finite corpus without Decimal arithmetic.

    Extreme compact cases use separate hand expectations before reaching here;
    no giant uncancelled operand power is constructed by this oracle.
    """
    match = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?", text)
    if match is None:
        raise ValueError("unsupported finite oracle lexeme")
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    coefficient = int(whole + fraction) * (-1 if sign == "-" else 1)
    power = int(exponent or "0") - len(fraction)
    if abs(power) > 1000:
        raise ValueError("compact exponent must use its frozen hand oracle")
    return (
        Fraction(coefficient * 10**power)
        if power >= 0
        else Fraction(coefficient, 10**-power)
    )


def strict_oracle(
    kind: str, case: dict[str, Any], value: Any
) -> tuple[int | None, str]:
    hand = PRICE_HAND if kind == "price" else QUANTITY_HAND
    if case["id"] in hand:
        return hand[case["id"]], "frozen_compact_hand_expectation"
    amount = lexical_fraction(str(value))
    unit = lexical_fraction(case["tick"] if kind == "price" else case["multiplier"])
    if unit <= 0:
        raise ValueError("oracle unit must be positive")
    if kind == "quantity" and (amount < 0 or (amount == 0 and not case["allow_zero"])):
        return None, "independent_lexical_rational"
    result = amount / unit if kind == "price" else amount * unit
    return (
        result.numerator if result.denominator == 1 else None
    ), "independent_lexical_rational"


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
        rounding=ROUNDING[profile["rounding"]],
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
    )
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
    record: dict[str, Any] = {
        "result": None,
        "error": None,
        "infrastructure_error": None,
        "outer_context_before": context_snapshot(getcontext()),
        "guard_before": sys.get_int_max_str_digits(),
    }
    with localcontext(profile_context(profile)) as context:
        record["context_before"] = context_snapshot(context)
        try:
            record["result"] = operation()
        except Exception as error:
            record["error"] = error_record(error)
        finally:
            record["context_after"] = context_snapshot(context)
    record["outer_context_after"] = context_snapshot(getcontext())
    record["guard_after"] = sys.get_int_max_str_digits()
    record["checks"] = {
        "context_preserved": record["context_before"] == record["context_after"],
        "outer_context_preserved": record["outer_context_before"]
        == record["outer_context_after"],
        "guard_preserved": record["guard_before"] == record["guard_after"],
    }
    return record


def scalar_observation(
    profile: dict[str, Any], kind: str, case: dict[str, Any]
) -> dict[str, Any]:
    value = case["value"]
    if case.get("input_type") == "float":
        value = float(value)
    elif case.get("input_type") == "decimal":
        value = Decimal(value)
    oracle_error = None
    try:
        expected, method = strict_oracle(kind, case, value)
        hand = (
            None if case["expected_integer"] is None else int(case["expected_integer"])
        )
        if expected != hand:
            raise ValueError("independent oracle and frozen hand expectation disagree")
    except Exception as error:
        expected, method, oracle_error = None, "oracle_error", error_record(error)
    if kind == "price":

        def operation() -> Any:
            return price_module.price_to_ticks(value, case["tick"], strict=True)

        error_type = "ValueError"
    else:
        multiplier = Decimal(case["multiplier"])

        def operation() -> Any:
            return adapter_utils.scaled_quantity(
                value,
                multiplier=multiplier,
                field="amount",
                row_index=0,
                allow_zero=case["allow_zero"],
            )

        error_type = "MarketDataAdapterError"
    observed = observe(profile, operation)
    matched = (
        (
            observed["error"] is None
            and type(observed["result"]) is int
            and observed["result"] == expected
        )
        if expected is not None
        else (observed["error"] is not None and observed["error"]["type"] == error_type)
    )
    observed["checks"]["oracle_match"] = oracle_error is None and matched
    observed.update(
        kind=kind,
        case_id=case["id"],
        input=deepcopy(case),
        input_lexeme=str(value),
        expected={
            "result": expected,
            "error_type": error_type if expected is None else None,
            "method": method,
        },
        status="returned" if observed["error"] is None else "raised",
        infrastructure_error=oracle_error,
    )
    return observed


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def write_csv_case(
    spec: dict[str, Any], case: dict[str, Any], directory: Path
) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    template = spec["csv_template"]
    rows = deepcopy(template["initial_book_rows"])
    edit = case["edit"]
    if edit in {"book_price", "book_amount"}:
        rows[0][6 if edit == "book_price" else 7] = case["literal"]
    trade = deepcopy(template["trade_row"])
    if edit.startswith("trade"):
        rows.append(deepcopy(template["reduction_row"]))
        if edit in {"trade_price", "trade_amount"}:
            trade[6 if edit == "trade_price" else 7] = case["literal"]
    if edit == "book_zero_update":
        rows.append(deepcopy(template["zero_update_row"]))
    paths = {"book": directory / "book.csv"}
    paths["book"].write_text(csv_text(template["book_header"], rows))
    if edit.startswith("trade"):
        paths["trades"] = directory / "trades.csv"
        paths["trades"].write_text(csv_text(template["trade_header"], [trade]))
    return paths


def validation_summary(result: Any) -> dict[str, Any]:
    return {
        "valid": result.valid,
        "issue_count": result.issue_count,
        "event_count": result.event_count,
        "event_stream_sha256": result.event_stream_sha256,
        "mode": result.mode.value,
        "reconstructed_book": result.reconstructed_book,
        "required_nonempty": result.required_nonempty,
        "issues": [plain(item) for item in result.issues],
        "diagnostics": dict(result.diagnostics),
        "final_book": plain(result.final_book),
    }


def persist_frame(
    frame: Any, adapter: Any, path: Path, evidence_root: Path
) -> dict[str, Any]:
    events = frame_to_events(frame)
    certified = validation_module.validate_event_stream(
        events, mode=ValidationMode.STRICT, reconstruct_book=True, require_nonempty=True
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return {
        "rows": frame.to_dicts(),
        "schema": [[name, str(dtype)] for name, dtype in frame.schema.items()],
        "parquet_path": str(path.relative_to(evidence_root)),
        "parquet_sha256": digest(path),
        "event_stream_sha256": event_stream_sha256(events),
        "validation": validation_summary(certified),
        "diagnostics": dict(adapter.last_diagnostics),
        "instrument": plain(getattr(adapter, "last_instrument", None)),
    }


def csv_observation(
    profile: dict[str, Any],
    case: dict[str, Any],
    spec: dict[str, Any],
    evidence_root: Path,
) -> dict[str, Any]:
    directory = evidence_root / "groups" / profile["id"] / "csv" / case["id"]
    paths = write_csv_case(spec, case, directory)
    calls: list[dict[str, Any]] = []
    originals = {
        name: getattr(tardis_module, name)
        for name in ("ticks_from_price", "scaled_quantity")
    }

    def price_observer(value: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(
            {
                "helper": "ticks_from_price",
                "field": kwargs.get("field"),
                "row_index": kwargs.get("row_index"),
                "value": str(value),
                "type": type(value).__name__,
            }
        )
        return originals["ticks_from_price"](value, *args, **kwargs)

    def quantity_observer(value: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(
            {
                "helper": "scaled_quantity",
                "field": kwargs.get("field"),
                "row_index": kwargs.get("row_index"),
                "value": str(value),
                "type": type(value).__name__,
            }
        )
        return originals["scaled_quantity"](value, *args, **kwargs)

    def operation() -> Any:
        template = spec["csv_template"]
        adapter = TardisCSVAdapter(
            tick_size=template["tick_size"],
            quantity_multiplier=template["quantity_multiplier"],
            timestamp_source=template["timestamp_source"],
            trade_match_window_ns=template["trade_match_window_ns"],
        )
        with (
            patch.object(tardis_module, "ticks_from_price", price_observer),
            patch.object(tardis_module, "scaled_quantity", quantity_observer),
        ):
            frame = adapter.checked(
                TardisData(book=paths["book"], trades=paths.get("trades"))
            )
        return persist_frame(
            frame, adapter, directory / "canonical.parquet", evidence_root
        )

    observed = observe(profile, operation)
    canonical = observed["result"]
    literal = case.get("literal")
    helper = "ticks_from_price" if case["edit"].endswith("price") else "scaled_quantity"
    preserved = all(item["type"] == "str" for item in calls) and (
        literal is None
        or any(item["helper"] == helper and item["value"] == literal for item in calls)
    )
    admitted = observed["error"] is None
    semantic_reject = (
        observed["error"] is not None
        and observed["error"]["type"] == "MarketDataAdapterError"
    )
    valid = (
        admitted
        and canonical["validation"]["valid"]
        and canonical["validation"]["mode"] == "strict"
        and canonical["validation"]["reconstructed_book"]
        and canonical["validation"]["required_nonempty"]
    )
    observed["checks"].update(
        oracle_match=(valid if case["expected"] == "admit" else semantic_reject),
        rawliteral_preserved=preserved,
    )
    observed.update(
        kind="csv",
        case_id=case["id"],
        input=deepcopy(case),
        expected={
            "status": case["expected"],
            "error_type": "MarketDataAdapterError"
            if case["expected"] == "reject"
            else None,
        },
        status="admitted" if admitted else "rejected" if semantic_reject else "error",
        strict_valid=valid,
        canonical=canonical,
        rawliteral={"expected": literal, "observed": calls, "preserved": preserved},
        csv_paths={
            name: str(path.relative_to(evidence_root)) for name, path in paths.items()
        },
        csv_sha256={name: digest(path) for name, path in paths.items()},
    )
    return observed


def failed_infrastructure(
    kind: str, case: dict[str, Any], error: Exception
) -> dict[str, Any]:
    return {
        "kind": kind,
        "case_id": case["id"],
        "input": deepcopy(case),
        "result": None,
        "error": None,
        "status": "infrastructure_error",
        "infrastructure_error": error_record(error),
        "checks": {"infrastructure_ok": False},
    }


def run_context_group(
    profile_id: str, evidence_root: Path, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    spec = load_spec(fixtures_path)
    profile = next(item for item in spec["profiles"] if item["id"] == profile_id)
    path = evidence_root / "groups" / (profile_id + ".json")
    if path.exists():
        raise ValueError("refusing to overwrite an existing ingress context ledger")
    record: dict[str, Any] = {
        "profile": profile_id,
        "profile_spec": profile,
        "observations": [],
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    for kind, cases in (
        ("price", spec["price_cases"]),
        ("quantity", spec["quantity_cases"]),
        ("csv", spec["csv_cases"]),
    ):
        for case in cases:
            try:
                observed = (
                    csv_observation(profile, case, spec, evidence_root)
                    if kind == "csv"
                    else scalar_observation(profile, kind, case)
                )
            except Exception as error:
                observed = failed_infrastructure(kind, case, error)
            record["observations"].append(observed)
            write_json(path, record)
    record["complete"] = True
    write_json(path, record)
    return record


def assert_group_matches(record: dict[str, Any]) -> None:
    failed = [
        {
            "kind": item["kind"],
            "case_id": item["case_id"],
            "result": item["result"],
            "error": item["error"],
            "checks": item["checks"],
            "infrastructure_error": item["infrastructure_error"],
        }
        for item in record["observations"]
        if item["infrastructure_error"] is not None or not all(item["checks"].values())
    ]
    assert record["complete"]
    assert len(record["observations"]) == 40
    assert not failed, failed


def run_normal_controls(
    evidence_root: Path, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    spec = load_spec(fixtures_path)
    path = evidence_root / "normal-controls.json"
    if path.exists():
        raise ValueError("refusing to overwrite normal ingress controls")
    record: dict[str, Any] = {
        "observations": [],
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    contract = json.loads((ROOT / PREFIX / "contract.json").read_text())
    profile = spec["profiles"][0]
    for case in spec["normal_controls"]:
        try:
            names = (
                ("book", "trades")
                if case["class"] == "TardisCSVAdapter"
                else ("messages", "orderbook")
            )
            inputs = {name: ROOT / case[name] for name in names}
            hashes = {case[name]: digest(inputs[name]) for name in names}
            if any(
                value != contract["normal_fixture_sha256"][name]
                for name, value in hashes.items()
            ):
                raise ValueError("normal fictional fixture bytes changed")

            def operation(
                case: dict[str, Any] = case, inputs: dict[str, Path] = inputs
            ) -> Any:
                if case["class"] == "TardisCSVAdapter":
                    adapter: Any = TardisCSVAdapter(tick_size=case["tick_size"])
                    source: Any = TardisData(
                        book=inputs["book"], trades=inputs["trades"]
                    )
                else:
                    adapter = LOBSTERCSVAdapter(
                        tick_size=case["tick_size"],
                        trading_date=case["date"],
                        levels=case["levels"],
                    )
                    source = LOBSTERData(
                        messages=inputs["messages"], orderbook=inputs["orderbook"]
                    )
                return persist_frame(
                    adapter.checked(source),
                    adapter,
                    evidence_root / "normal" / (case["id"] + ".parquet"),
                    evidence_root,
                )

            observation = observe(profile, operation)
            actual = observation.pop("result")
            observation.update(
                id=case["id"],
                status="admitted" if actual is not None else "error",
                input_sha256=hashes,
            )
            if actual is not None:
                observation.update(actual)
            # Whole LOBSTER preprocessing is outside the strict-helper context
            # invariant. Retain its context facts without asserting a new claim.
            observation["checks"] = {
                "completed": observation["error"] is None and actual is not None,
                "strict_valid": actual is not None
                and actual["validation"]["valid"]
                and actual["validation"]["mode"] == "strict",
                "outer_context_preserved": observation["checks"][
                    "outer_context_preserved"
                ],
                "guard_preserved": observation["checks"]["guard_preserved"],
            }
        except Exception as error:
            observation = {
                "id": case["id"],
                "status": "infrastructure_error",
                "error": error_record(error),
                "checks": {"completed": False},
            }
        record["observations"].append(observation)
        write_json(path, record)
    record["complete"] = True
    write_json(path, record)
    return record


def run_legacy_controls(
    evidence_root: Path, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    spec = load_spec(fixtures_path)
    path = evidence_root / "legacy-controls.json"
    if path.exists():
        raise ValueError("refusing to overwrite legacy ingress controls")
    profile = spec["profiles"][0]
    cases = [
        ("ties_positive_even", "nonstrict", "2.5", "1", 2),
        ("ties_positive_odd", "nonstrict", "3.5", "1", 4),
        ("ties_negative_even", "nonstrict", "-2.5", "1", -2),
        ("ties_negative_odd", "nonstrict", "-3.5", "1", -4),
        ("existing_nonstrict", "nonstrict", "100.005", "0.01", 10000),
        ("existing_floor", "floor", "100.019", "0.01", 10001),
        ("signed_floor", "floor", "-1.01", "1", -2),
        ("existing_inverse", "inverse", "10001", "0.01", "100.01"),
    ]
    record: dict[str, Any] = {
        "observations": [],
        "checks": {},
        "complete": False,
        "provenance": provenance(),
    }
    write_json(path, record)
    for name, kind, value, unit, expected in cases:
        if kind == "nonstrict":

            def operation(value: str = value, unit: str = unit) -> Any:
                return price_module.price_to_ticks(value, unit, strict=False)

        elif kind == "floor":

            def operation(value: str = value, unit: str = unit) -> Any:
                return price_module.floor_price_to_ticks(value, unit)

        else:

            def operation(value: str = value, unit: str = unit) -> Any:
                return price_module.ticks_to_price(int(value), unit)

        observed = observe(profile, operation)
        actual = observed["result"]
        matched = (
            actual.as_tuple() == Decimal(expected).as_tuple()
            if kind == "inverse" and isinstance(actual, Decimal)
            else type(actual) is int and actual == expected
        )
        observed.update(
            id=name, kind=kind, input={"value": value, "unit": unit}, expected=expected
        )
        observed["checks"]["result_matches"] = observed["error"] is None and matched
        record["observations"].append(observed)
        record["checks"][name] = all(observed["checks"].values())
        write_json(path, record)
    record["complete"] = True
    write_json(path, record)
    return record


def evidence_root(tmp_path: Path) -> Path:
    configured = os.environ.get("LOB_INGRESS_EVIDENCE_DIR")
    root = Path(configured).resolve() if configured else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_frozen_strict_ingress_group_records_every_observation_before_asserting(
    profile_id: str, tmp_path: Path
) -> None:
    assert_group_matches(run_context_group(profile_id, evidence_root(tmp_path)))


def test_existing_adapter_controls_preserve_canonical_contract(tmp_path: Path) -> None:
    record = run_normal_controls(evidence_root(tmp_path))
    assert record["complete"]
    assert len(record["observations"]) == 2
    assert all(all(item["checks"].values()) for item in record["observations"]), record


def test_legacy_conversion_controls_keep_default_context_semantics(
    tmp_path: Path,
) -> None:
    record = run_legacy_controls(evidence_root(tmp_path))
    assert record["complete"]
    assert record["checks"]
    assert all(record["checks"].values()), record
