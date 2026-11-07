"""Frozen same-time engine witnesses; importing this module executes no replay.

Workers must import lobmm from their already selected archive before run_case.
Observers copy values and delegate each original call exactly once. Plotting,
publication, baseline selection and publication gates belong to the runner.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import struct
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import patch

FIXTURES_SHA256 = "01f3229ca7d5ddfeec646cbaaead2f5088360556d8c861e51a5107586acbdefc"
CONTRACT_SHA256 = "26b51977ca815629bb386333e824cd956384c1d0f15e06025f61ce995dca8685"
CASE_NAMES = (
    "maker_prefix",
    "maker_late_tail",
    "drawdown_order",
    "taker_phase_control",
)
EVIDENCE = Path("docs/evidence/same-time-reporting-20261107")
TABLE_NAMES = (
    "orders",
    "fills",
    "inventory",
    "pnl",
    "quotes",
    "risk_events",
    "market",
    "markouts",
)
CLOCKS = {"wall_clock_seconds", "events_per_second", "peak_memory_bytes"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {
            "decimal": [
                value.as_tuple().sign,
                list(value.as_tuple().digits),
                value.as_tuple().exponent,
            ]
        }
    if isinstance(value, Fraction):
        return {"fraction": [value.numerator, value.denominator]}
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, float):
        return {"binary64": struct.pack(">d", value).hex()}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"unsupported witness evidence type: {type(value)!r}")


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _book_state(book: Any) -> dict[str, Any]:
    bid, ask = book.best_bid, book.best_ask
    return {
        "best_bid_ticks": bid,
        "best_ask_ticks": ask,
        "midpoint_ticks": None if bid is None or ask is None else (bid + ask) / 2,
        "bids": [
            [price, quantity]
            for price, quantity in sorted(book.bids.items(), reverse=True)
        ],
        "asks": [[price, quantity] for price, quantity in sorted(book.asks.items())],
    }


def dictionary_book_oracle(raw_events: list[list[Any]]) -> list[dict[str, Any]]:
    """Independent external-depth arithmetic for the frozen valid event tape."""
    levels: dict[int, dict[int, int]] = {1: {}, -1: {}}

    def state() -> dict[str, Any]:
        bid = max(levels[1], default=None)
        ask = min(levels[-1], default=None)
        if bid is not None and ask is not None and bid >= ask:
            raise ValueError("frozen tape is locked or crossed")
        return {
            "best_bid_ticks": bid,
            "best_ask_ticks": ask,
            "midpoint_ticks": None if bid is None or ask is None else (bid + ask) / 2,
            "bids": [
                [price, quantity]
                for price, quantity in sorted(levels[1].items(), reverse=True)
            ],
            "asks": [
                [price, quantity] for price, quantity in sorted(levels[-1].items())
            ],
        }

    result = []
    previous_time, previous_sequence = -1, -1
    for row in raw_events:
        if type(row) is not list or len(row) != 6:
            raise ValueError("frozen event must have six fields")
        timestamp, sequence, kind, side, price, quantity = row
        if any(
            type(item) is not int
            for item in (timestamp, sequence, side, price, quantity)
        ):
            raise ValueError("frozen event integer fields must be builtin integers")
        if timestamp < previous_time or timestamp < 0 or sequence <= previous_sequence:
            raise ValueError("frozen event time/sequence order is invalid")
        if (
            kind not in {"ADD", "CANCEL", "TRADE"}
            or side not in (1, -1)
            or min(price, quantity) <= 0
        ):
            raise ValueError("unsupported or invalid frozen event")
        before = state()
        old = levels[side].get(price, 0)
        if kind == "ADD":
            new = old + quantity
        else:
            if quantity > old:
                raise ValueError("frozen reduction exceeds external quantity")
            new = old - quantity
        if new:
            levels[side][price] = new
        else:
            levels[side].pop(price, None)
        result.append({"event": list(row), "pre": before, "post": state()})
        previous_time, previous_sequence = timestamp, sequence
    return result


def _inputs(
    source_root: Path, fixtures_path: Path | None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    path = fixtures_path or source_root / EVIDENCE / "fixtures.json"
    contract_path = path.with_name("contract.json")
    if _digest(path) != FIXTURES_SHA256 or _digest(contract_path) != CONTRACT_SHA256:
        raise ValueError("frozen fixture/contract hash mismatch; stop without retuning")
    fixtures = json.loads(path.read_text())
    if set(fixtures["cases"]) != set(CASE_NAMES):
        raise ValueError("frozen case set changed")
    names = (
        "config",
        "enums",
        "events",
        "exchange",
        "orders",
        "scheduler",
        "backtest",
        "risk",
        "metrics",
        "report",
    )
    modules = {name: importlib.import_module("lobmm." + name) for name in names}
    identities = {}
    for name, module in modules.items():
        actual = Path(module.__file__).resolve()
        expected = (source_root / "src/lobmm" / (name + ".py")).resolve()
        if actual != expected:
            raise RuntimeError(f"worker imported wrong {name} source: {actual}")
        identities[name] = {"path": str(actual), "sha256": _digest(actual)}
    return fixtures, modules, identities


def _physical(
    case_name: str,
    tables: dict[str, list[dict[str, Any]]],
    trace: dict[str, Any],
    oracle: list[dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, Any]]:
    fills = tables.get("fills", [])
    market_calls = [row for row in trace["exchange"] if row["kind"] == "market"]
    expected_book = [(row["pre"], row["post"]) for row in oracle]
    actual_book = [(row["pre"], row["post"]) for row in market_calls]
    checks = {"external_book_matches_dictionary_oracle": actual_book == expected_book}
    if case_name == "taker_phase_control":
        commands = [row for row in trace["exchange"] if row["kind"] == "command"]
        command = commands[-1] if commands else {}
        checks.update(
            {
                "one_taker_105_quantity1": len(fills) == 1
                and fills[0]["price_ticks"] == 105
                and fills[0]["quantity"] == 1
                and fills[0]["liquidity_role"] == "taker",
                "entry_and_post_midpoint102": command.get("pre", {}).get(
                    "midpoint_ticks"
                )
                == 102
                and command.get("post", {}).get("midpoint_ticks") == 102,
                "command_phase_after_all_same_time_market": bool(commands)
                and commands[-1]["scheduler_key"][2] == 20
                and all(
                    row["scheduler_key"] < commands[-1]["scheduler_key"]
                    for row in market_calls
                ),
            }
        )
        return checks, {
            "entry_book": command.get("pre"),
            "post_book": command.get("post"),
            "fills": fills,
        }
    pnl = tables["pnl"]
    mark20 = [row["net_pnl"] for row in pnl if row["timestamp_ns"] == 20]
    events = tables["risk_events"]
    checks.update(
        {
            "one_maker_99_quantity1": len(fills) == 1
            and fills[0]["price_ticks"] == 99
            and fills[0]["quantity"] == 1
            and fills[0]["liquidity_role"] == "maker",
            "initial_pnl_zero": bool(pnl) and pnl[0]["net_pnl"] == 0,
            "held_inventory_one": bool(pnl) and pnl[-1]["inventory"] == 1,
        }
    )
    causing = next(
        (row for row in market_calls if row["event"]["sequence_number"] == 5), {}
    )
    checks["maker_cause_pre_post_midpoint100"] = (
        causing.get("pre", {}).get("midpoint_ticks")
        == causing.get("post", {}).get("midpoint_ticks")
        == 100
    )
    if case_name == "drawdown_order":
        checks.update(
            {
                "time20_peak3_then_drop1": mark20 == [3, 1],
                "terminal_pnl_one": bool(pnl) and pnl[-1]["net_pnl"] == 1,
                "max_drawdown_two": trace["metrics"]["pnl"]["maximum_drawdown"] == 2,
                "risk_drawdown_two_at20_limit1_5": any(
                    row["reason"] == "max_drawdown"
                    and row["timestamp_ns"] == 20
                    and row["observed_value"] == 2
                    and row["limit_value"] == 1.5
                    for row in events
                ),
            }
        )
    return checks, {
        "time20_net_pnl": mark20,
        "first_net_pnl": pnl[0]["net_pnl"] if pnl else None,
        "terminal_net_pnl": pnl[-1]["net_pnl"] if pnl else None,
        "fills": fills,
        "risk_events": events,
        "causing_market": causing,
    }


def run_case(
    case_name: str,
    source_root: str | Path,
    output_dir: str | Path,
    *,
    fixtures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute one named frozen witness explicitly, returning retained evidence.

    The archive worker owns sys.path and source selection. For a baseline archive
    without the new evidence folder, pass its retained frozen fixtures_path.
    False physical checks are retained, never repaired by changing fixture data.
    """
    if case_name not in CASE_NAMES:
        raise ValueError(f"unknown frozen case: {case_name}")
    source = Path(source_root).resolve()
    destination = Path(output_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("witness output directory must be new or empty")
    destination.mkdir(parents=True, exist_ok=True)
    fixtures, modules, identities = _inputs(
        source, None if fixtures_path is None else Path(fixtures_path).resolve()
    )
    spec = fixtures["cases"][case_name]
    raw = deepcopy(spec["events"])
    oracle = dictionary_book_oracle(raw)
    raw_config = deepcopy(fixtures["common_config"])
    if "risk_max_drawdown" in spec:
        raw_config["risk"]["max_drawdown"] = spec["risk_max_drawdown"]
    config = modules["config"].AppConfig.model_validate(raw_config)
    config_json = config.model_dump(mode="json")
    enum = modules["enums"]
    events = [
        modules["events"].MarketEvent(
            timestamp_ns=t,
            sequence_number=s,
            event_type=enum.EventType[kind],
            side=enum.Side(side),
            price_ticks=price,
            quantity=quantity,
        )
        for t, s, kind, side, price, quantity in raw
    ]
    validation_module = importlib.import_module("lobmm.validation")
    certificate = validation_module.validate_event_stream(
        events, mode=enum.ValidationMode.STRICT
    )
    fingerprint_module = importlib.import_module("lobmm.data.fingerprint")
    event_hash = fingerprint_module.event_stream_sha256(events)
    trace: dict[str, Any] = {"scheduler": [], "exchange": [], "risk": []}
    active: dict[str, Any] = {"key": None}
    Exchange = modules["exchange"].Exchange
    Scheduler = modules["scheduler"].Scheduler
    RiskManager = modules["risk"].RiskManager
    original_market, original_command = (
        Exchange.handle_market_event,
        Exchange.handle_command,
    )
    original_run, original_risk = Scheduler.run, RiskManager.observe_pnl

    def observed_run(scheduler: Any, handler: Any, **kwargs: Any) -> Any:
        def observe(item: Any, instance: Any) -> Any:
            key = list(item.ordering_key)
            trace["scheduler"].append(
                {
                    "key": key,
                    "parent_schedule_id": item.parent_schedule_id,
                    "payload_type": type(item.payload).__name__,
                    "payload": _canonical(item.payload),
                }
            )
            previous = active["key"]
            active["key"] = key
            try:
                return handler(item, instance)
            finally:
                active["key"] = previous

        return original_run(scheduler, observe, **kwargs)

    def observed_market(exchange: Any, event: Any) -> Any:
        before = _book_state(exchange.book)
        result = original_market(exchange, event)
        trace["exchange"].append(
            {
                "kind": "market",
                "scheduler_key": deepcopy(active["key"]),
                "event": event.as_dict(),
                "pre": before,
                "post": _book_state(exchange.book),
                "fills": _canonical(result.fills),
                "reports": _canonical(result.reports),
            }
        )
        return result

    def observed_command(
        exchange: Any, command: Any, *, exchange_arrival_timestamp_ns: int
    ) -> Any:
        before = _book_state(exchange.book)
        result = original_command(
            exchange,
            command,
            exchange_arrival_timestamp_ns=exchange_arrival_timestamp_ns,
        )
        trace["exchange"].append(
            {
                "kind": "command",
                "scheduler_key": deepcopy(active["key"]),
                "command": _canonical(command),
                "arrival_timestamp_ns": exchange_arrival_timestamp_ns,
                "pre": before,
                "post": _book_state(exchange.book),
                "fills": _canonical(result.fills),
                "reports": _canonical(result.reports),
            }
        )
        return result

    def observed_risk(manager: Any, net_pnl: Any, *, timestamp_ns: int) -> Any:
        before = {
            "high_watermark": _canonical(manager.high_watermark),
            "kill": manager.kill_switch_active,
            "reason": _canonical(manager.kill_reason),
            "events": _canonical(manager.events),
        }
        decision = original_risk(manager, net_pnl, timestamp_ns=timestamp_ns)
        trace["risk"].append(
            {
                "scheduler_key": deepcopy(active["key"]),
                "timestamp_ns": timestamp_ns,
                "net_pnl": _canonical(net_pnl),
                "before": before,
                "decision": _canonical(decision),
                "after": {
                    "high_watermark": _canonical(manager.high_watermark),
                    "kill": manager.kill_switch_active,
                    "reason": _canonical(manager.kill_reason),
                    "events": _canonical(manager.events),
                },
            }
        )
        return decision

    artifact_directory: Path | None = None
    tables: dict[str, list[dict[str, Any]]] = {}
    metrics: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(Scheduler, "run", observed_run))
            stack.enter_context(
                patch.object(Exchange, "handle_market_event", observed_market)
            )
            stack.enter_context(
                patch.object(Exchange, "handle_command", observed_command)
            )
            stack.enter_context(patch.object(RiskManager, "observe_pnl", observed_risk))
            if spec["kind"] == "full_backtest":
                result = modules["backtest"].run_backtest(
                    config,
                    events,
                    run_name=case_name,
                    output_root=destination,
                    generate_plots=False,
                    input_validation=certificate,
                    persist_artifacts=True,
                )
                artifact_directory = result.run_directory
                import polars as pl

                actual = {path.stem for path in artifact_directory.glob("*.parquet")}
                if actual != set(TABLE_NAMES):
                    raise RuntimeError(
                        "full witness did not persist exactly eight tables"
                    )
                tables = {
                    name: pl.read_parquet(
                        artifact_directory / (name + ".parquet")
                    ).to_dicts()
                    for name in TABLE_NAMES
                }
                hashes = {
                    name + ".parquet": _digest(artifact_directory / (name + ".parquet"))
                    for name in TABLE_NAMES
                }
                metrics = json.loads((artifact_directory / "metrics.json").read_text())
                diagnostics = json.loads(
                    (artifact_directory / "diagnostics.json").read_text()
                )
                summary = json.loads((artifact_directory / "summary.json").read_text())
            elif spec["kind"] == "exchange_scheduler_control":
                exchange = Exchange(
                    exchange_config=config.exchange,
                    queue_config=config.queue_model,
                    fee_config=config.fees,
                    instrument_config=config.instrument,
                )
                scheduler = Scheduler()
                for event in events:
                    scheduler.schedule(
                        timestamp_ns=event.timestamp_ns,
                        phase=enum.SchedulerPhase.MARKET,
                        source_sequence=event.sequence_number,
                        payload=event,
                    )
                command_spec = spec["command"]
                command = modules["orders"].NewOrderRequest(
                    client_order_id="frozen-taker",
                    side=enum.Side(command_spec["side"]),
                    price_ticks=command_spec["price_ticks"],
                    quantity=command_spec["quantity"],
                    strategy_id="frozen-taker",
                    decision_timestamp_ns=command_spec["timestamp_ns"],
                    send_timestamp_ns=command_spec["timestamp_ns"],
                    post_only=command_spec["post_only"],
                    rest_unfilled=command_spec["rest_unfilled"],
                )
                scheduler.schedule(
                    timestamp_ns=command_spec["timestamp_ns"],
                    phase=enum.SchedulerPhase.EXCHANGE_COMMAND,
                    payload=command,
                )

                def handle(item: Any, _: Any) -> None:
                    if item.phase is enum.SchedulerPhase.MARKET:
                        exchange.handle_market_event(item.payload)
                    else:
                        exchange.handle_command(
                            item.payload,
                            exchange_arrival_timestamp_ns=item.timestamp_ns,
                        )

                scheduler.run(handle)
                tables = {
                    "fills": [_canonical(fill) for fill in exchange.registry.fills]
                }
            else:
                raise ValueError("unsupported frozen case kind")
    except BaseException as exc:
        _write(
            destination / "partial-witness.json",
            {
                "case": case_name,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "source_identities": identities,
                "oracle": oracle,
                "trace": trace,
            },
        )
        raise
    trace["metrics"] = metrics
    physical_checks, physical_observations = _physical(case_name, tables, trace, oracle)
    stable_metrics = deepcopy(metrics)
    for clock in CLOCKS:
        stable_metrics.get("engineering", {}).pop(clock, None)
    stable_diagnostics = {
        key: value for key, value in diagnostics.items() if key not in CLOCKS
    }
    effect = _canonical(
        {
            "case": case_name,
            "config": config_json,
            "events": raw,
            "tables": tables,
            "metrics": stable_metrics,
            "diagnostics": stable_diagnostics,
            "summary": summary,
            "scheduler": trace["scheduler"],
            "exchange": trace["exchange"],
            "risk": trace["risk"],
            "dictionary_book_oracle": oracle,
        }
    )
    result = {
        "case": case_name,
        "kind": spec["kind"],
        "artifact_directory": None
        if artifact_directory is None
        else str(artifact_directory),
        "parquet_sha256": hashes,
        "tables": tables,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "summary": summary,
        "config": config_json,
        "config_sha256": hashlib.sha256(
            json.dumps(config_json, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "event_stream_sha256": event_hash,
        "source_identities": identities,
        "fixtures_sha256": FIXTURES_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "dictionary_book_oracle": oracle,
        "hand_queue_oracle": {
            "ahead": 1,
            "own": 1,
            "external_behind_added": 2,
            "trade_budget": 2,
            "expected_maker_quantity": 1,
        },
        "scheduler": trace["scheduler"],
        "exchange": trace["exchange"],
        "risk": trace["risk"],
        "physical_checks": physical_checks,
        "physical_observations": physical_observations,
        "canonical_effect": effect,
        "canonical_effect_sha256": hashlib.sha256(
            json.dumps(
                effect, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest(),
    }
    _write(destination / "witness.json", result)
    return result
