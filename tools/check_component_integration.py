"""Reproduce the six preregistered component-composition observations.

Importing this module does not import lobmm or execute a case. The controller
copies pinned source bytes and uses one isolated subprocess per observation.
The rational ledger and artifact checks run after the observed engine returns.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import json
import platform
import shutil
import struct
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, getcontext, localcontext
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path("docs/evidence/component-integration-20261115")
TOOL = Path("tools/check_component_integration.py")
TEST = Path("tests/integration/test_component_integration.py")
CASES = (
    ("below_drawdown", "0.99999999999999999999999999999", "100", "1"),
    ("inclusive_loss", "1", "1", "100"),
    ("ordinary", "0.01", "100", "100"),
)
EVENTS = (
    (0, 0, "SNAPSHOT", 1, 99, 1),
    (0, 1, "SNAPSHOT", -1, 101, 1),
    (10, 2, "ADD", 1, 100, 1),
    (20, 3, "TRADE", 1, 100, 1),
    (30, 4, "ADD", -1, 103, 1),
    (40, 5, "ADD", -1, 103, 1),
)
COLUMNS = (
    "timestamp_ns",
    "sequence_number",
    "event_type",
    "side",
    "price_ticks",
    "quantity",
)
TABLES = (
    "orders",
    "fills",
    "inventory",
    "pnl",
    "quotes",
    "risk_events",
    "market",
    "markouts",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def encode(value: Any) -> Any:
    """Retain Decimal quantum and float bits without numeric normalization."""
    if isinstance(value, Decimal):
        item = value.as_tuple()
        return {
            "type": "decimal.Decimal",
            "text": str(value),
            "tuple": {
                "sign": item.sign,
                "digits": list(item.digits),
                "exponent": item.exponent,
            },
        }
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, Enum):
        return {
            "enum": type(value).__module__ + "." + type(value).__qualname__,
            "value": value.value,
        }
    if type(value) is float:
        return {
            "type": "builtins.float",
            "hex": value.hex(),
            "bits": struct.pack(">d", value).hex(),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__module__ + "." + type(value).__qualname__,
            **{
                field.name: encode(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, Mapping):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"unsupported observer value: {type(value)!r}")


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def context_state() -> dict[str, Any]:
    context = getcontext()
    return {
        **{
            name: getattr(context, name)
            for name in ("prec", "rounding", "Emin", "Emax", "capitals", "clamp")
        },
        "traps": {
            signal.__name__: enabled for signal, enabled in context.traps.items()
        },
        "flags": {
            signal.__name__: enabled for signal, enabled in context.flags.items()
        },
        "integer_string_guard": sys.get_int_max_str_digits(),
    }


def source_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted((root / "src/lobmm").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def check_source(root: Path, expected: dict[str, str]) -> None:
    require(
        len(expected) == 38 and source_inventory(root) == expected,
        "source package inventory or bytes differ from the frozen manifest",
    )


def import_proof(source: Path, expected: dict[str, str]) -> dict[str, Any]:
    observed = {}
    for name, module in sorted(sys.modules.items()):
        if name != "lobmm" and not name.startswith("lobmm."):
            continue
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(source), f"editable-install import leak: {name}")
        relative = path.relative_to(source).as_posix()
        require(
            relative in expected and digest(path) == expected[relative],
            f"unexpected application import: {name}",
        )
        observed[name] = {
            "path": str(path),
            "relative_path": relative,
            "sha256": expected[relative],
        }
    require(bool(observed), "missing application import proof")
    return observed


def frozen_pins(manifests: Path) -> dict[str, str]:
    paths = [
        ROOT / TOOL,
        ROOT / TEST,
        ROOT / "docs/COMPONENT_INTEGRATION_DESIGN.md",
        manifests,
        ROOT / EVIDENCE / "risk-main.py",
    ]
    return {str(path.resolve()): digest(path) for path in paths}


def case_spec(name: str) -> dict[str, Any]:
    selected = next(item for item in CASES if item[0] == name)
    return {
        "case": name,
        "events": [dict(zip(COLUMNS, row, strict=True)) for row in EVENTS],
        "config_overrides": {
            "instrument": {"tick_size": "1"},
            "latency": {
                "market_data_ns": 0,
                "order_entry_ns": 0,
                "cancellation_ns": 5,
                "fill_report_ns": 0,
                "market_data_jitter_ns": 0,
                "order_entry_jitter_ns": 0,
                "cancellation_jitter_ns": 0,
                "fill_report_jitter_ns": 0,
            },
            "fees": {
                "maker_fee_per_unit": selected[1],
                "maker_rebate_per_unit": "0",
                "taker_fee_per_unit": "0",
                "proportional_fee_rate": "0",
            },
            "strategy": {
                "order_size": 1,
                "minimum_quote_lifetime_ns": 0,
                "refresh_interval_ns": 1000,
                "stale_after_ns": 1000,
            },
            "risk": {
                "max_abs_inventory": 2,
                "max_order_size": 1,
                "max_total_open_quantity": 4,
                "max_open_orders": 4,
                "max_loss": selected[2],
                "max_drawdown": selected[3],
                "max_quote_age_ns": 1000,
                "minimum_spread_ticks": 1,
                "max_messages_per_second": None,
                "max_volatility_ticks": None,
                "prohibit_new_quotes_last_ns": 0,
            },
            "backtest": {
                "warmup_events": 0,
                "timer_interval_ns": 10,
                "mark_price": "midpoint",
                "session_end_policy": "mark",
                "track_memory": False,
            },
            "metrics": {"markout_horizons_ns": [5, 30], "near_limit_fraction": 0.5},
            "output": {"write_plots": False},
        },
    }


def rational_ledger(spec: dict[str, Any]) -> dict[str, Any]:
    """Derive the fixed fill and market path from literal input, not app helpers."""
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    states = []
    for row in spec["events"]:
        levels = book[row["side"]]
        price, quantity = row["price_ticks"], row["quantity"]
        before = levels.get(price, 0)
        if row["event_type"] in ("SNAPSHOT", "ADD"):
            require(
                row["event_type"] != "SNAPSHOT" or not before,
                "oracle duplicate initial depth",
            )
            levels[price] = before + quantity
        else:
            require(before >= quantity, "oracle historical depth overdraw")
            if before == quantity:
                del levels[price]
            else:
                levels[price] = before - quantity
        bid = max(book[1], default=None)
        ask = min(book[-1], default=None)
        require(bid is None or ask is None or bid < ask, "oracle crossed book")
        states.append(
            {
                **row,
                "best_bid": bid,
                "best_ask": ask,
                "midpoint": None
                if bid is None or ask is None
                else Fraction(bid + ask, 2),
                "historical_quantity_before": before,
                "historical_quantity_after": levels.get(price, 0),
            }
        )
    fee = Fraction(spec["config_overrides"]["fees"]["maker_fee_per_unit"])
    limit = spec["config_overrides"]["risk"]
    # At submission there is no historical depth at BID100. The t10 ADD is
    # behind the unit own order. The t20 unit TRADE therefore fills that order.
    fill = {
        "side": 1,
        "quantity": 1,
        "price_ticks": 100,
        "exchange_fill_timestamp_ns": 20,
        "liquidity_role": "maker",
    }
    return {
        "market_states": states,
        "fill": fill,
        "fee": fee,
        "trade_cash": Fraction(-100),
        "cash_after_fill": Fraction(-100) - fee,
        "inventory_after_fill": 1,
        "mark_after_fill": Fraction(100),
        "net_after_fill": -fee,
        "high_watermark": Fraction(0),
        "max_loss": Fraction(limit["max_loss"]),
        "max_drawdown": Fraction(limit["max_drawdown"]),
        "exposure_start_ns": 20,
        "exposure_end_ns": 40,
        "near_limit_duration_ns": 20,
        "observation_duration_ns": 40,
        "horizon_5": {
            "target_timestamp_ns": 25,
            "future_timestamp_ns": 30,
            "observed_quantity": 1,
            "markout_ticks": Fraction(0),
        },
        "horizon_30": {
            "target_timestamp_ns": 50,
            "future_timestamp_ns": None,
            "observed_quantity": 0,
            "markout_ticks": None,
        },
    }


def expected_risk(ledger: dict[str, Any], timestamp_ns: int) -> dict[str, Any]:
    net = ledger["net_after_fill"] if timestamp_ns >= 20 else Fraction(0)
    reason = None
    if net <= -ledger["max_loss"]:
        reason = "max_loss"
    elif -net >= ledger["max_drawdown"]:
        reason = "max_drawdown"
    return {"net_pnl": net, "approved": reason is None, "kill_reason": reason}


def decimal_fraction(value: Any) -> Fraction | None:
    """Treat a wrong original numeric type as a recorded domain mismatch."""
    if not isinstance(value, dict) or value.get("type") != "decimal.Decimal":
        return None
    try:
        return Fraction(value["text"])
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def decimal_projection_bits(value: Any) -> str | None:
    rational = decimal_fraction(value)
    return None if rational is None else struct.pack(">d", float(rational)).hex()


class Observer:
    """Delegating wrappers with a durable prefix before every original call.

    A call entry without ``delegations`` is still in flight. Return updates are
    flushed by the next emit and by the worker's finally block.
    """

    def __init__(self, trace_path: Path) -> None:
        self.trace: list[dict[str, Any]] = []
        self.trace_path = trace_path
        self.key: tuple[int, ...] | None = None
        self.restores: list[tuple[Any, str, Any]] = []
        self.origins: list[dict[str, Any]] = []
        self.latest_snapshot: Any = None
        self.latest_snapshot_ordinal: int | None = None

    def emit(self, kind: str, **values: Any) -> dict[str, Any]:
        row = {
            "ordinal": len(self.trace),
            "kind": kind,
            "scheduler_key": encode(self.key),
            **{name: encode(value) for name, value in values.items()},
        }
        self.trace.append(row)
        write(self.trace_path, self.trace)
        return row

    def install(self, owner: Any, name: str, wrapper: Any) -> Any:
        original = getattr(owner, name)
        self.restores.append((owner, name, original))
        path = Path(inspect.getsourcefile(original)).resolve()
        self.origins.append(
            {
                "module": original.__module__,
                "qualname": original.__qualname__,
                "path": str(path),
                "line": original.__code__.co_firstlineno,
                "sha256": digest(path),
            }
        )
        setattr(owner, name, functools.wraps(original)(wrapper(original)))
        return original

    def restore(self) -> None:
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)


def install_observers(observer: Observer, app: Any) -> None:
    from lobmm.channels import OrderedChannel
    from lobmm.exchange import Exchange
    from lobmm.portfolio import Portfolio, PortfolioSnapshot
    from lobmm.risk import RiskManager
    from lobmm.scheduler import Scheduler
    from lobmm.strategy_runtime import StrategyRuntime

    def scheduled(original: Any) -> Any:
        def run(self: Any, handler: Any, **kwargs: Any) -> Any:
            def delegated(event: Any, scheduler: Any) -> Any:
                previous = observer.key
                observer.key = event.ordering_key
                observer.emit(
                    "scheduled",
                    payload=event.payload,
                    parent_schedule_id=event.parent_schedule_id,
                )
                try:
                    return handler(event, scheduler)
                finally:
                    observer.key = previous

            return original(self, delegated, **kwargs)

        return run

    observer.install(Scheduler, "run", scheduled)

    def observed_risk(original: Any) -> Any:
        def observe(self: Any, net_pnl: Any, *, timestamp_ns: int) -> Any:
            before = context_state()
            entry = observer.emit(
                "risk_ingress",
                timestamp_ns=timestamp_ns,
                net_pnl=net_pnl,
                context_before=before,
                high_watermark_before=self.high_watermark,
                kill_before=self.kill_switch_active,
                snapshot_ordinal=observer.latest_snapshot_ordinal,
                identical_snapshot_net_object=(
                    observer.latest_snapshot is not None
                    and net_pnl is observer.latest_snapshot.net_pnl
                ),
            )
            try:
                decision = original(self, net_pnl, timestamp_ns=timestamp_ns)
            except BaseException as exc:
                entry.update(
                    error={"type": type(exc).__name__, "message": str(exc)},
                    context_after=context_state(),
                    delegations=1,
                )
                raise
            entry.update(
                decision=encode(decision),
                context_after=context_state(),
                high_watermark_after=encode(self.high_watermark),
                kill_after=self.kill_switch_active,
                kill_reason=encode(self.kill_reason),
                events=encode(self.events),
                delegations=1,
            )
            return decision

        return observe

    observer.install(RiskManager, "observe_pnl", observed_risk)

    def snapshot_wrapper(name: str) -> Any:
        def make(original: Any) -> Any:
            def method(self: Any, *args: Any, **kwargs: Any) -> Any:
                call = observer.emit(
                    "portfolio_call", method=name, args=args, kwargs=kwargs
                )
                result = original(self, *args, **kwargs)
                row = observer.emit(
                    "portfolio_return",
                    method=name,
                    call_ordinal=call["ordinal"],
                    snapshot=result,
                    delegations=1,
                )
                observer.latest_snapshot = result
                observer.latest_snapshot_ordinal = row["ordinal"]
                return result

            return method

        return make

    for name in ("record_fill", "mark_to_market", "snapshot"):
        observer.install(Portfolio, name, snapshot_wrapper(name))

    def projection(original: Any) -> Any:
        def project(value: Any) -> Any:
            before = encode(value) if isinstance(value, PortfolioSnapshot) else None
            result = original(value)
            if before is not None:
                observer.emit(
                    "snapshot_projection",
                    original=before,
                    projected=result,
                    delegations=1,
                )
            return result

        return project

    observer.install(app, "_dataclass_row", projection)

    def runtime(original: Any) -> Any:
        def decide(
            self: Any, *, timestamp_ns: int, allow_new_quotes: bool = True
        ) -> Any:
            before = observer.emit(
                "runtime_decision",
                timestamp_ns=timestamp_ns,
                allow_new_quotes=allow_new_quotes,
                known_inventory=self.known_inventory,
                observed_sequence=self.observed_sequence,
            )
            result = original(
                self, timestamp_ns=timestamp_ns, allow_new_quotes=allow_new_quotes
            )
            before.update(actions=encode(result), delegations=1)
            return result

        return decide

    observer.install(StrategyRuntime, "decide", runtime)

    def exchange_method(name: str) -> Any:
        def make(original: Any) -> Any:
            def method(self: Any, *args: Any, **kwargs: Any) -> Any:
                row = observer.emit(
                    "exchange_call",
                    method=name,
                    args=args,
                    kwargs=kwargs,
                    live_before=self.live_orders,
                )
                result = original(self, *args, **kwargs)
                row.update(
                    result=encode(result),
                    live_after=encode(self.live_orders),
                    best_bid_after=self.book.best_bid,
                    best_ask_after=self.book.best_ask,
                    delegations=1,
                )
                return result

            return method

        return make

    for name in ("handle_market_event", "handle_command", "expire_order"):
        observer.install(Exchange, name, exchange_method(name))

    def channel(original: Any) -> Any:
        def send(self: Any, *args: Any, **kwargs: Any) -> Any:
            row = observer.emit(
                "channel_send", channel=self.name, args=args, kwargs=kwargs
            )
            result = original(self, *args, **kwargs)
            row.update(returned=encode(result), delegations=1)
            return result

        return send

    observer.install(OrderedChannel, "send", channel)


def evaluate_case(
    spec: dict[str, Any],
    trace: list[dict[str, Any]],
    tables: dict[str, list[dict[str, Any]]],
    metrics: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    ledger = rational_ledger(spec)
    checks: dict[str, bool] = {}
    fills = tables["fills"]
    checks["one_expected_maker_fill"] = len(fills) == 1 and all(
        fills[0].get(key) == value for key, value in ledger["fill"].items()
    )
    exchange_fills = [
        fill
        for row in trace
        if row["kind"] == "exchange_call"
        for fill in row["result"]["fills"]
    ]
    checks["exact_literal_exchange_fee"] = len(exchange_fills) == 1 and (
        decimal_fraction(exchange_fills[0]["fee"]) == ledger["fee"]
        and decimal_fraction(exchange_fills[0]["rebate"]) == 0
    )
    risks = [row for row in trace if row["kind"] == "risk_ingress"]
    checks["complete_ordered_risk_call_vector"] = [
        row["timestamp_ns"] for row in risks
    ] == [0, 0, 10, 10, 20, 20, 20, 30, 30, 40, 40]
    linked_returns = []
    for row in risks:
        previous = next(
            (
                item
                for item in reversed(trace[: row["ordinal"]])
                if item["kind"] == "portfolio_return"
            ),
            None,
        )
        linked_returns.append(previous)
    checks["risk_links_latest_original_snapshot"] = bool(risks) and all(
        previous is not None
        and row["snapshot_ordinal"] == previous["ordinal"]
        and row["net_pnl"] == previous["snapshot"]["net_pnl"]
        and row["timestamp_ns"] == previous["snapshot"]["timestamp_ns"]
        for row, previous in zip(risks, linked_returns, strict=True)
    )
    risk_results = []
    for row in risks:
        oracle = expected_risk(ledger, row["timestamp_ns"])
        reason = row["decision"]["reason"]
        actual_reason = None if reason is None else reason["value"]
        risk_results.append(
            {
                "ordinal": row["ordinal"],
                "timestamp_ns": row["timestamp_ns"],
                "oracle": encode(oracle),
                "exact_net_matches": decimal_fraction(row["net_pnl"])
                == oracle["net_pnl"],
                "decision_matches": row["decision"]["approved"] == oracle["approved"]
                and actual_reason == oracle["kill_reason"],
                "context_preserved": row["context_before"] == row["context_after"],
                "same_snapshot_decimal": row["identical_snapshot_net_object"],
            }
        )
    checks["all_exact_risk_handoffs"] = bool(risks) and all(
        row["exact_net_matches"] and row["same_snapshot_decimal"]
        for row in risk_results
    )
    checks["all_risk_calls_default_precision"] = bool(risks) and all(
        row["context_before"]["prec"] == 28
        and row["context_before"]["rounding"] == "ROUND_HALF_EVEN"
        for row in risks
    )
    checks["exact_zero_high_watermark"] = bool(risks) and all(
        decimal_fraction(row["high_watermark_before"]) == 0
        and decimal_fraction(row["high_watermark_after"]) == 0
        for row in risks
    )
    returns = [row["snapshot"] for row in trace if row["kind"] == "portfolio_return"]
    snapshot_checks = []
    for snapshot in returns:
        after_fill = snapshot["timestamp_ns"] >= 20
        expected = {
            "net_pnl": ledger["net_after_fill"] if after_fill else Fraction(0),
            "trade_cash": ledger["trade_cash"] if after_fill else Fraction(0),
            "cash": ledger["cash_after_fill"] if after_fill else Fraction(0),
            "fees": ledger["fee"] if after_fill else Fraction(0),
        }
        snapshot_checks.append(
            snapshot["inventory"] == int(after_fill)
            and all(
                decimal_fraction(snapshot[key]) == value
                for key, value in expected.items()
            )
        )
    checks["all_snapshot_ledgers_exact"] = bool(snapshot_checks) and all(
        snapshot_checks
    )
    projections = [row for row in trace if row["kind"] == "snapshot_projection"]
    projected_risk_calls = [
        (row, previous)
        for row, previous in zip(risks, linked_returns, strict=True)
        if previous is not None
        and any(
            previous["ordinal"] < projection["ordinal"] < row["ordinal"]
            and projection["original"] == previous["snapshot"]
            for projection in projections
        )
    ]
    projected_methods = [previous["method"] for _, previous in projected_risk_calls]
    checks["six_projection_backed_market_and_fill_calls"] = (
        len(projections) == len(projected_risk_calls) == 6
        and projected_methods.count("record_fill") == 1
        and projected_methods.count("mark_to_market") == 5
        and sum(
            previous is not None and previous["method"] == "snapshot"
            for previous in linked_returns
        )
        == 5
    )
    checks["float_projections_only"] = bool(projections) and all(
        row["projected"]["net_pnl"]["bits"]
        == decimal_projection_bits(row["original"]["net_pnl"])
        for row in projections
    )
    checks["pnl_artifact_matches_projection_order"] = len(tables["pnl"]) == len(
        projections
    ) and all(
        item["timestamp_ns"] == projected["projected"]["timestamp_ns"]
        and struct.pack(">d", item["net_pnl"]).hex()
        == projected["projected"]["net_pnl"]["bits"]
        for item, projected in zip(tables["pnl"], projections, strict=False)
    )
    inventory = metrics["inventory"]
    checks["elapsed_inventory_exposure"] = (
        inventory["near_limit_duration_ns"] == ledger["near_limit_duration_ns"]
        and inventory["observation_duration_ns"] == ledger["observation_duration_ns"]
        and inventory["time_window_start_ns"] == 0
        and inventory["time_window_end_ns"] == 40
        and inventory["time_near_limits_fraction"] == float(Fraction(20, 40))
    )
    summary = metrics["execution_quality"]["markout_summary_by_horizon_ns"]
    checks["horizon_quantity_coverage"] = all(
        summary[str(h)]["all"]["total_fill_quantity"] == 1
        and summary[str(h)]["all"]["observed_quantity"] == observed
        and summary[str(h)]["all"]["missing_quantity"] == 1 - observed
        and summary[str(h)]["all"]["quantity_coverage"] == float(observed)
        for h, observed in ((5, 1), (30, 0))
    )
    checks["two_markout_rows_link_the_only_bid_fill"] = (
        len(tables["markouts"]) == 2
        and len(fills) == 1
        and all(
            row["fill_id"] == fills[0]["fill_id"] and row["side"] == 1
            for row in tables["markouts"]
        )
    )
    markouts = {row["horizon_ns"]: row for row in tables["markouts"]}
    checks["asof_and_censoring"] = set(markouts) == {5, 30} and all(
        markouts[h]["target_timestamp_ns"]
        == ledger[f"horizon_{h}"]["target_timestamp_ns"]
        and markouts[h]["future_timestamp_ns"]
        == ledger[f"horizon_{h}"]["future_timestamp_ns"]
        and markouts[h]["markout_ticks"] == (0.0 if h == 5 else None)
        for h in (5, 30)
    )
    checks["end_inventory_one"] = diagnostics["end_inventory"] == 1
    runtime = [row for row in trace if row["kind"] == "runtime_decision"]
    at20 = [row for row in runtime if row["timestamp_ns"] == 20]
    checks["post_fill_runtime_gate_recorded"] = bool(at20)
    accepted_asks = [
        (row, report, order)
        for row in trace
        if row["kind"] == "exchange_call"
        for report in row["result"]["reports"]
        if report["report_type"]["value"] == "accepted"
        and report["order_status"]["value"] == "live"
        for order in row["live_after"]
        if order["order_id"] == report["order_id"] and order["side"]["value"] == -1
    ]
    original_ask = accepted_asks[0][1]["order_id"] if accepted_asks else None
    accepted = accepted_asks[0] if accepted_asks else None
    checks["original_ask_acceptance_identity_and_time"] = accepted is not None and (
        accepted[0]["method"] == "handle_command"
        and accepted[0]["kwargs"]["exchange_arrival_timestamp_ns"] < 20
        and accepted[1]["exchange_timestamp_ns"]
        == accepted[0]["kwargs"]["exchange_arrival_timestamp_ns"]
        == accepted[2]["exchange_arrival_timestamp_ns"]
        and accepted[2]["price_ticks"] == 102
        and accepted[2]["original_quantity"] == accepted[2]["remaining_quantity"] == 1
        and accepted[2]["send_timestamp_ns"]
        <= accepted[2]["exchange_arrival_timestamp_ns"]
        and accepted[0]["args"][0]["client_order_id"]
        == accepted[1]["client_order_id"]
        == accepted[2]["client_order_id"]
        and accepted[0]["args"][0]["price_ticks"] == 102
        and accepted[0]["args"][0]["quantity"] == 1
    )
    cancel_calls = [
        row
        for row in trace
        if row["kind"] == "exchange_call"
        and row["method"] == "handle_command"
        and row["args"]
        and row["args"][0]["type"].endswith("CancelRequest")
        and row["args"][0]["order_id"] == original_ask
    ]
    checks["accepted_original_ask_recorded"] = original_ask is not None
    cancellation_at25 = any(
        row["kwargs"]["exchange_arrival_timestamp_ns"] == 25
        and row["args"][0]["send_timestamp_ns"] == 20
        and any(
            order["order_id"] == original_ask
            and order["price_ticks"] == 102
            and order["remaining_quantity"] == 1
            for order in row["live_before"]
        )
        and all(order["order_id"] != original_ask for order in row["live_after"])
        and any(
            report["report_type"]["value"] == "cancelled"
            and report["order_id"] == original_ask
            and report["order_status"]["value"] == "cancelled"
            and report["remaining_quantity"] == 1
            and report["exchange_timestamp_ns"] == 25
            for report in row["result"]["reports"]
        )
        for row in cancel_calls
    )
    cancellation_at25 = cancellation_at25 and any(
        row["kind"] == "channel_send"
        and row["channel"] == "cancellations"
        and row["args"][0]["order_id"] == original_ask
        and row["kwargs"]["send_timestamp_ns"] == 20
        and row["returned"]["timestamp_ns"] == 25
        and row["returned"]["payload"]["sampled_latency_ns"] == 5
        for row in trace
    )
    first_kill = next(
        (
            {"timestamp_ns": row["timestamp_ns"], "reason": row["kill_reason"]["value"]}
            for row in risks
            if not row["kill_before"] and row["kill_after"]
        ),
        None,
    )
    return {
        "checks": checks,
        "ledger": encode(ledger),
        "risk_calls": risk_results,
        "risk_oracle_agreement": bool(risk_results)
        and all(row["decision_matches"] for row in risk_results),
        "risk_context_preserved": bool(risk_results)
        and all(row["context_preserved"] for row in risk_results),
        "kill_switch_active": diagnostics["kill_switch_active"],
        "first_kill": first_kill,
        "runtime_at20": at20,
        "cancelled_original_ask_at25": cancellation_at25,
        "original_ask_order_id": original_ask,
        "all_later_runtime_gates_suppressed": all(
            not row["allow_new_quotes"] for row in runtime if row["timestamp_ns"] >= 20
        ),
        "all_at20_runtime_gates_enabled": bool(at20)
        and all(row["allow_new_quotes"] for row in at20),
    }


def run_worker(
    source_root: Path,
    source: str,
    case: str,
    output: Path,
    manifests: Path,
    pin_file: Path,
) -> int:
    require(
        output.is_dir()
        and {path.name for path in output.iterdir()} <= {"worker.log"}
        and (
            not (output / "worker.log").exists()
            or (output / "worker.log").stat().st_size == 0
        ),
        "worker output already contains evidence; refusing to retry or overwrite",
    )
    expected = read(manifests)[source.lower()]
    pins = read(pin_file)
    for path, sha in pins.items():
        require(digest(Path(path)) == sha, "frozen harness input changed")
    check_source(source_root, expected)
    require(
        not any(name == "lobmm" or name.startswith("lobmm.") for name in sys.modules),
        "application imported before source selection",
    )
    sys.path.insert(0, str(source_root / "src"))
    import polars as pl

    import lobmm.backtest as app
    from lobmm.config import AppConfig
    from lobmm.data.loaders import load_events
    from lobmm.data.schema import events_to_frame
    from lobmm.enums import EventType, Side
    from lobmm.events import MarketEvent
    from lobmm.strategies.base import DesiredQuote, MarketMakingStrategy
    from lobmm.validation import validate_event_stream

    observer = Observer(output / "trace.json")
    result: dict[str, Any] = {
        "source": source,
        "case": case,
        "status": "started",
        "started_utc": now(),
        "source_root": str(source_root),
        "source_sha256": expected,
        "frozen_pins": pins,
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "imports_before": import_proof(source_root, expected),
    }
    spec = case_spec(case)
    write(output / "spec.json", spec)
    write(output / "result.json", result)
    original_factory = app.make_strategy

    class FixtureStrategy(MarketMakingStrategy):
        def desired_quotes(
            self, *, view: Any, known_inventory: int, max_abs_inventory: int
        ) -> tuple[Any, ...]:
            return (DesiredQuote(Side.BID, 100, 1), DesiredQuote(Side.ASK, 102, 1))

    try:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)) as context:
            context.clear_flags()
            result["case_start_context"] = context_state()
            config = AppConfig.model_validate(spec["config_overrides"])
            events = [
                MarketEvent(
                    timestamp_ns=row["timestamp_ns"],
                    sequence_number=row["sequence_number"],
                    event_type=EventType(row["event_type"]),
                    side=Side(row["side"]),
                    price_ticks=row["price_ticks"],
                    quantity=row["quantity"],
                )
                for row in spec["events"]
            ]
            frame = events_to_frame(events)
            frame.write_parquet(output / "input.parquet")
            roundtrip = load_events(output / "input.parquet")
            result["canonical_roundtrip"] = {
                "original_rows": [event.as_dict() for event in events],
                "roundtrip_rows": [event.as_dict() for event in roundtrip],
                "schema": [[name, str(dtype)] for name, dtype in frame.schema.items()],
                "input_parquet_sha256": digest(output / "input.parquet"),
            }
            validation = validate_event_stream(events, mode=config.data.validation_mode)
            result["validation"] = encode(validation)
            result["config"] = config.model_dump(mode="json")
            install_observers(observer, app)
            app.make_strategy = lambda selected: FixtureStrategy(selected)
            engine = app.run_backtest(
                config,
                events,
                run_name="engine",
                output_root=output,
                generate_plots=False,
                input_validation=validation,
            )
            result["case_finish_context"] = context_state()
        # The complete raw observations are durable before any numeric verdict.
        write(output / "trace.json", observer.trace)
        tables = {
            name: pl.read_parquet(engine.run_directory / f"{name}.parquet").to_dicts()
            for name in TABLES
        }
        result["artifacts"] = {
            name: {
                "path": f"engine/{name}.parquet",
                "sha256": digest(engine.run_directory / f"{name}.parquet"),
                "rows": len(tables[name]),
                "schema": [
                    [key, str(value)]
                    for key, value in pl.read_parquet(
                        engine.run_directory / f"{name}.parquet"
                    ).schema.items()
                ],
            }
            for name in TABLES
        }
        write(output / "tables.json", encode(tables))
        result["evaluation"] = evaluate_case(
            spec, observer.trace, tables, engine.metrics, engine.diagnostics
        )
        result["evaluation"]["checks"]["canonical_roundtrip_exact"] = (
            result["canonical_roundtrip"]["original_rows"]
            == spec["events"]
            == result["canonical_roundtrip"]["roundtrip_rows"]
        )
        result["status"] = "complete"
        returncode = 0
    except BaseException as exc:
        result.update(
            status="infrastructure_error",
            error={
                "type": type(exc).__name__,
                "module": type(exc).__module__,
                "message": str(exc),
            },
            traceback=traceback.format_exc(),
        )
        returncode = 2
    finally:
        app.make_strategy = original_factory
        observer.restore()
        write(output / "trace.json", observer.trace)
        write(output / "observer-origins.json", observer.origins)
        result["imports_after"] = import_proof(source_root, expected)
        check_source(source_root, expected)
        for path, sha in pins.items():
            require(digest(Path(path)) == sha, "frozen bytes changed during worker")
        result["finished_utc"] = now()
        write(output / "result.json", result)
    return returncode


def study(
    output: Path, c1_root: Path, c0_root: Path | None, manifests: Path
) -> dict[str, Any]:
    require(not output.exists(), "refusing to overwrite a previous integration attempt")
    maps = read(manifests)
    require(set(maps) == {"c0", "c1"}, "unexpected source roles")
    check_source(c1_root, maps["c1"])
    output.mkdir(parents=True)
    pins = frozen_pins(manifests)
    write(output / "frozen-pins.json", pins)
    manifest: dict[str, Any] = {
        "status": "started",
        "started_utc": now(),
        "frozen_pins": pins,
        "attempts": [],
    }
    write(output / "manifest.json", manifest)
    started = time.monotonic()
    try:
        for role, supplied in (("c1", c1_root), ("c0", c0_root or c1_root)):
            if role == "c0" and c0_root is not None:
                check_source(supplied, maps[role])
            for relative in maps[role]:
                destination = output / "sources" / role / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                origin = (
                    ROOT / EVIDENCE / "risk-main.py"
                    if role == "c0"
                    and c0_root is None
                    and relative == "src/lobmm/risk.py"
                    else supplied / relative
                )
                shutil.copy2(origin, destination)
            check_source(output / "sources" / role, maps[role])
        for case, *_ in CASES:
            for source in ("C0", "C1"):
                remaining = 900 - (time.monotonic() - started)
                require(remaining > 0, "integration stage exhausted its 900-second cap")
                directory = output / source.lower() / case
                directory.mkdir(parents=True)
                command = [
                    sys.executable,
                    "-I",
                    "-B",
                    str(ROOT / TOOL),
                    "--worker",
                    "--source-root",
                    str(output / "sources" / source.lower()),
                    "--source",
                    source,
                    "--case",
                    case,
                    "--output",
                    str(directory),
                    "--source-manifests",
                    str(manifests),
                    "--frozen-pins",
                    str(output / "frozen-pins.json"),
                ]
                attempt = {
                    "source": source,
                    "case": case,
                    "command": command,
                    "started_utc": now(),
                    "cap_seconds": min(120, remaining),
                }
                manifest["attempts"].append(attempt)
                write(output / "manifest.json", manifest)
                with (directory / "worker.log").open("w") as stream:
                    completed = subprocess.run(
                        command,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=min(120, remaining),
                        check=False,
                    )
                attempt.update(
                    returncode=completed.returncode,
                    finished_utc=now(),
                    log_sha256=digest(directory / "worker.log"),
                )
                write(output / "manifest.json", manifest)
                require(
                    completed.returncode == 0,
                    f"worker infrastructure failure: {source}/{case}",
                )
        observations = [
            read(output / source / case / "result.json")
            for case, *_ in CASES
            for source in ("c0", "c1")
        ]
        # Only now, with every fixed observation durable, form the assertions.
        checks = {
            "six_complete_observations": len(observations) == 6
            and all(row["status"] == "complete" for row in observations),
            "all_core_mechanics": all(
                all(row["evaluation"]["checks"].values()) for row in observations
            ),
        }
        for row in observations:
            key = f"{row['source']}/{row['case']}"
            evaluation = row["evaluation"]
            if row["source"] == "C1":
                checks[key + ":exact_risk_and_context"] = (
                    evaluation["risk_oracle_agreement"]
                    and evaluation["risk_context_preserved"]
                )
            should_kill = row["case"] == "inclusive_loss" or (
                row["source"] == "C0" and row["case"] == "below_drawdown"
            )
            checks[key + ":kill_control"] = (
                evaluation["kill_switch_active"] == should_kill
            )
            checks[key + ":first_kill_time_and_reason"] = evaluation["first_kill"] == (
                {
                    "timestamp_ns": 20,
                    "reason": "max_loss"
                    if row["case"] == "inclusive_loss"
                    else "max_drawdown",
                }
                if should_kill
                else None
            )
            checks[key + ":runtime_t20"] = (
                evaluation["all_later_runtime_gates_suppressed"]
                if should_kill
                else evaluation["all_at20_runtime_gates_enabled"]
            )
            if should_kill:
                checks[key + ":accepted_ask_cancel_delay"] = evaluation[
                    "cancelled_original_ask_at25"
                ]
            if row["source"] == "C0" and row["case"] == "below_drawdown":
                checks[key + ":negative_risk_control"] = not evaluation[
                    "risk_oracle_agreement"
                ]
            elif row["source"] == "C0":
                checks[key + ":risk_controls"] = evaluation["risk_oracle_agreement"]
        summary = {
            "checks": checks,
            "criterion": all(checks.values()),
            "observations": [
                {
                    "source": row["source"],
                    "case": row["case"],
                    "evaluation": row["evaluation"],
                }
                for row in observations
            ],
        }
        write(output / "report.json", summary)
        manifest["status"] = "complete"
        return summary
    except BaseException as exc:
        manifest.update(
            status="stopped", error=repr(exc), traceback=traceback.format_exc()
        )
        raise
    finally:
        manifest["finished_utc"] = now()
        manifest["elapsed_seconds"] = time.monotonic() - started
        manifest["files"] = {
            path.relative_to(output).as_posix(): digest(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
            and path != output / "manifest.json"
            and "__pycache__" not in path.parts
        }
        write(output / "manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--c1-source", type=Path, default=ROOT)
    parser.add_argument("--c0-source", type=Path)
    parser.add_argument(
        "--source-manifests",
        type=Path,
        default=ROOT / EVIDENCE / "source-manifests.json",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--source", choices=("C0", "C1"), help=argparse.SUPPRESS)
    parser.add_argument(
        "--case", choices=tuple(row[0] for row in CASES), help=argparse.SUPPRESS
    )
    parser.add_argument("--frozen-pins", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            require(
                all((args.source_root, args.source, args.case, args.frozen_pins)),
                "worker requires an explicit source, case and frozen pins",
            )
            return run_worker(
                args.source_root.resolve(),
                args.source,
                args.case,
                args.output.resolve(),
                args.source_manifests.resolve(),
                args.frozen_pins.resolve(),
            )
        result = study(
            args.output.resolve(),
            args.c1_source.resolve(),
            None if args.c0_source is None else args.c0_source.resolve(),
            args.source_manifests.resolve(),
        )
        print(json.dumps(result, indent=2))
        return 0 if result["criterion"] else 1
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
