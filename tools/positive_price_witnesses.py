"""Frozen low-price witnesses, invoked explicitly by an archive worker only.

Importing this module performs no validation, strategy call or replay. Observers
copy values and delegate each original operation once. Independent dictionary
book arithmetic and the fixed FIFO budget provide the physical reference;
terminal exported queue/cumulative fields are not trajectory evidence.
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
from pathlib import Path
from typing import Any
from unittest.mock import patch

CONTRACT_SHA256 = "8862b5a2d346e65248e8a561c488dfe3c2bdf1b85d54de75a85dd85fe017c037"
FIXTURES_SHA256 = "96c30af69e182c3218c9f892ac5276f773b3a10393adf0782441869470cf727c"
EVIDENCE = Path("docs/evidence/positive-price-20261109")
POLICIES = ("fixed_spread", "inventory_aware", "microprice")
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
EXPECTED_ERROR = "desired quote price and quantity must be positive"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _copy(value: Any) -> Any:
    """Copy raw values immediately; do not retain mutable engine references."""
    if isinstance(value, Enum):
        return _copy(value.value)
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        return {"decimal": [sign, list(digits), exponent]}
    if isinstance(value, float):
        return {"binary64": struct.pack(">d", value).hex()}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "_type": type(value).__name__,
            **{
                field.name: _copy(getattr(value, field.name)) for field in fields(value)
            },
        }
    if isinstance(value, dict):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_copy(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"unsupported copied witness value: {type(value)!r}")


def _error(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overrides.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else deepcopy(value)
        )
    return result


def _book(book: Any) -> dict[str, Any]:
    return {
        "bid": book.best_bid,
        "ask": book.best_ask,
        "bids": [
            [price, quantity]
            for price, quantity in sorted(book.bids.items(), reverse=True)
        ],
        "asks": [[price, quantity] for price, quantity in sorted(book.asks.items())],
    }


def dictionary_book_oracle(raw_events: list[list[Any]]) -> list[dict[str, Any]]:
    """Independent strict external-depth reference for the entire frozen tape."""
    levels: dict[int, dict[int, int]] = {1: {}, -1: {}}

    def state() -> dict[str, Any]:
        bid, ask = max(levels[1], default=None), min(levels[-1], default=None)
        if bid is not None and ask is not None and bid >= ask:
            raise ValueError("frozen dictionary book is locked/crossed")
        return {
            "bid": bid,
            "ask": ask,
            "bids": [[p, q] for p, q in sorted(levels[1].items(), reverse=True)],
            "asks": [[p, q] for p, q in sorted(levels[-1].items())],
        }

    result = []
    previous_time = previous_sequence = -1
    for raw in raw_events:
        if type(raw) is not list or len(raw) != 6:
            raise ValueError("frozen event must have six fields")
        timestamp, sequence, kind, side, price, quantity = raw
        if (
            type(timestamp) is not int
            or type(sequence) is not int
            or timestamp < 0
            or timestamp < previous_time
            or sequence <= previous_sequence
        ):
            raise ValueError("frozen time/sequence is invalid")
        before = state()
        if kind == "RESET":
            if side is not None or price != 0 or quantity != 0:
                raise ValueError("invalid frozen reset")
            levels = {1: {}, -1: {}}
        else:
            if (
                any(type(item) is not int for item in (side, price, quantity))
                or side not in (1, -1)
                or price <= 0
                or quantity <= 0
            ):
                raise ValueError("invalid frozen price/quantity/side")
            old = levels[side].get(price, 0)
            if kind in ("ADD", "SNAPSHOT"):
                new = old + quantity
            elif kind in ("CANCEL", "TRADE"):
                if quantity > old:
                    raise ValueError("frozen reduction exceeds strict external depth")
                new = old - quantity
            else:
                raise ValueError("unsupported frozen event kind")
            if new:
                levels[side][price] = new
            else:
                levels[side].pop(price, None)
        result.append({"event": list(raw), "pre": before, "post": state()})
        previous_time, previous_sequence = timestamp, sequence
    return result


def _strategy_state(strategy: Any) -> dict[str, Any]:
    return {
        "known_inventory": strategy.known_inventory,
        "observed_sequence": strategy.observed_sequence,
        "latest_view": _copy(strategy.latest_view),
        "managed": [_copy(quote) for quote in strategy.active_quotes],
    }


def _exchange_state(exchange: Any) -> dict[str, Any]:
    return {
        "book": _book(exchange.book),
        "orders": [
            {
                name: _copy(getattr(order, name))
                for name in (
                    "order_id",
                    "client_order_id",
                    "side",
                    "price_ticks",
                    "original_quantity",
                    "remaining_quantity",
                    "status",
                    "creation_timestamp_ns",
                    "send_timestamp_ns",
                    "exchange_arrival_timestamp_ns",
                )
            }
            for order in exchange.registry.orders
        ],
        "queues": [
            {
                "side": int(side),
                "price_ticks": price,
                "segments": _copy(exchange.queue_model.snapshot_segments(side, price)),
            }
            for side, price in exchange.queue_model.active_levels
        ],
    }


def _queue_at(state: dict[str, Any], side: int, price: int) -> list[Any]:
    return next(
        (
            row["segments"]
            for row in state.get("queues", [])
            if row["side"] == side and row["price_ticks"] == price
        ),
        [],
    )


def _checks(
    trace: dict[str, Any],
    oracle: list[dict[str, Any]],
    certificate: dict[str, Any],
    error: dict[str, str] | None,
    tables: dict[str, Any],
    diagnostics: dict[str, Any],
    parquet: dict[str, str],
    minimum_lifetime: int,
) -> dict[str, Any]:
    market = [row for row in trace["exchange"] if row["kind"] == "market"]
    prefix = len(market) <= len(oracle) and all(
        actual["event"]
        == dict(
            zip(
                (
                    "timestamp_ns",
                    "sequence_number",
                    "event_type",
                    "side",
                    "price_ticks",
                    "quantity",
                ),
                expected["event"],
                strict=True,
            )
        )
        and actual["pre"]["book"] == expected["pre"]
        and actual.get("post", {}).get("book") == expected["post"]
        for actual, expected in zip(market, oracle, strict=False)
    )
    common = {
        "full_strict_input_certificate_valid": certificate["valid"]
        and certificate["issue_count"] == 0
        and certificate["event_count"] == len(oracle)
        and certificate["mode"] == "strict"
        and certificate["reconstructed_book"] is True
        and certificate["required_nonempty"] is True,
        "processed_historical_prefix_matches_dictionary": prefix,
    }
    commands = [row for row in trace["exchange"] if row["kind"] == "command"]
    initial = [
        row
        for row in commands
        if row["command"]["_type"] == "NewOrderRequest"
        and row["arrival_timestamp_ns"] == 0
        and any(
            report["report_type"] == "accepted" for report in row.get("reports", [])
        )
    ]
    prior = [
        {
            "side": row["command"]["side"],
            "price_ticks": row["command"]["price_ticks"],
            "quantity": row["command"]["quantity"],
            "order_id": next(
                report["order_id"]
                for report in row["reports"]
                if report["report_type"] == "accepted"
            ),
            "client_order_id": row["command"]["client_order_id"],
        }
        for row in initial
    ]
    initial_pair = sorted(
        (row["side"], row["price_ticks"], row["quantity"]) for row in prior
    ) == [(-1, 3, 1), (1, 1, 1)]
    bid = next((row for row in prior if row["side"] == 1), {})
    ask = next((row for row in prior if row["side"] == -1), {})
    failed_decisions = [
        row for row in trace["decisions"] if row.get("error") is not None
    ]
    failed = failed_decisions[-1] if failed_decisions else {}
    failed_attempts = [
        row for row in trace["desired_attempts"] if row.get("error") is not None
    ]
    attempt = failed_attempts[-1] if failed_attempts else {}
    failure_time = failed.get("timestamp_ns")
    view = failed.get("before", {}).get("latest_view") or {}
    bbo = (
        {"bid": view.get("best_bid_ticks"), "ask": view.get("best_ask_ticks")}
        if failed
        else None
    )
    failure_managed = failed.get("before", {}).get("managed", [])
    accepted_shadow = initial_pair and all(
        any(
            q["side"] == side
            and q["exchange_order_id"] == expected.get("order_id")
            and q["accepted_timestamp_ns"] is not None
            for q in failure_managed
        )
        for side, expected in ((1, bid), (-1, ask))
    )
    rounded10 = [
        row
        for row in trace["rounded_calls"]
        if row.get("timestamp_ns") == 10 and row.get("result") == [0, 3]
    ]
    expected_error = {"type": "ValueError", "message": EXPECTED_ERROR}
    main = {
        **common,
        "initial_bid1_ask3_accepted": initial_pair,
        "accepted_managed_context_at_failure": accepted_shadow,
        "exact_constructor_ValueError": error == expected_error
        and attempt.get("error") == expected_error,
        "failure_decision_at10_observed_bid1_ask2": failure_time == 10
        and bbo == {"bid": 1, "ask": 2},
        "attempted_bid0_quantity1_at10": attempt.get("side") == 1
        and attempt.get("price_ticks") == 0
        and attempt.get("quantity") == 1
        and attempt.get("timestamp_ns") == 10,
        "raw_rounded_bid0_ask3_at10": bool(rounded10),
        "no_command_dispatched_by_failed_decision": not any(
            row["arrival_timestamp_ns"] == 10 for row in commands
        ),
    }
    cancels = [
        row
        for row in commands
        if row["command"]["_type"] == "CancelRequest"
        and row["command"].get("order_id") == bid.get("order_id")
    ]
    decision10 = next(
        (
            row
            for row in trace["decisions"]
            if row["timestamp_ns"] == 10 and row.get("error") is None
        ),
        {},
    )
    before10, after10 = (
        decision10.get("before", {}).get("managed", []),
        decision10.get("after", {}).get("managed", []),
    )
    old_bid = next(
        (q for q in before10 if q["exchange_order_id"] == bid.get("order_id")), {}
    )
    old_ask = next(
        (q for q in before10 if q["exchange_order_id"] == ask.get("order_id")), {}
    )
    after_ask = next(
        (q for q in after10 if q["exchange_order_id"] == ask.get("order_id")), {}
    )
    ask_kept = bool(ask) and bool(old_ask) and old_ask == after_ask
    cancel_completed = (
        len(cancels) == 1
        and cancels[0].get("fills") == []
        and any(
            report["order_id"] == bid.get("order_id")
            and report["report_type"] == "cancelled"
            and report["order_status"] == "cancelled"
            and report["remaining_quantity"] == 1
            for report in cancels[0].get("reports", [])
        )
        and any(
            order["order_id"] == bid.get("order_id")
            and order["status"] == "cancelled"
            and order["remaining_quantity"] == 1
            for order in cancels[0].get("post", {}).get("orders", [])
        )
    )
    fills = [fill for row in trace["exchange"] for fill in row.get("fills", [])]
    ask_fill = (
        len(fills) == 1
        and fills[0]["side"] == -1
        and fills[0]["price_ticks"] == 3
        and fills[0]["quantity"] == 1
        and fills[0]["liquidity_role"] == "maker"
        and fills[0]["exchange_fill_timestamp_ns"] == 40
        and fills[0]["order_id"] == ask.get("order_id")
    )
    no_ask_cancel = not any(
        row["command"]["_type"] == "CancelRequest"
        and row["command"].get("order_id") == ask.get("order_id")
        and row["arrival_timestamp_ns"] <= 40
        for row in commands
    )
    at20 = next((row for row in market if row["event"]["timestamp_ns"] == 20), {})
    at30 = next((row for row in market if row["event"]["timestamp_ns"] == 30), {})
    at40 = next((row for row in market if row["event"]["timestamp_ns"] == 40), {})
    fifo = [
        ["external", None, 1],
        ["own", ask.get("order_id"), 1],
        ["external", None, 1],
    ]
    candidate = {
        **common,
        "completed_all_historical_inputs": error is None and len(market) == len(oracle),
        "eight_actual_parquet_outputs": set(parquet)
        == {name + ".parquet" for name in TABLE_NAMES},
        "initial_bid1_ask3_accepted": initial_pair,
        "first_bid_cancel_once_at10": len(cancels) == 1
        and cancels[0]["arrival_timestamp_ns"] == 10,
        "first_bid_cancel_completed_in_this_zero_delay_case": cancel_completed,
        "first_bid_cancel_before_minimum_lifetime": bool(old_bid)
        and 0 <= 10 - old_bid["sent_timestamp_ns"] < minimum_lifetime,
        "manager_existing_absent_side_reason": any(
            row["side"] == 1
            and row["action"] == "cancel"
            and row["reason"] == "risk_side_suppression"
            for row in decision10.get("new_decisions", [])
        ),
        "valid_original_ask3_retained_at10": ask_kept,
        "no_original_ask_cancel_before_fill": bool(ask) and no_ask_cancel,
        "queue_at20_and_pre40_matches_ahead1_own1_behind1": bool(ask)
        and _queue_at(at20.get("post", {}), -1, 3) == fifo
        and _queue_at(at40.get("pre", {}), -1, 3) == fifo,
        "trade_ask2_at30_does_not_fill_ask3": bool(at30) and not at30.get("fills"),
        "one_original_maker_ask3_quantity1_fill_at40": ask_fill,
        "no_bid_fills": not any(fill["side"] == 1 for fill in fills),
        "terminal_inventory_minus1": diagnostics.get("end_inventory") == -1
        and bool(tables.get("inventory"))
        and tables["inventory"][-1]["inventory"] == -1,
        "no_kill_switch": diagnostics.get("kill_switch_active") is False,
        "all_constructed_desired_prices_positive": bool(trace["desired_attempts"])
        and all(
            row["price_ticks"] > 0 and row.get("error") is None
            for row in trace["desired_attempts"]
        ),
        "all_submitted_new_prices_positive": bool(initial)
        and all(
            row["command"]["price_ticks"] > 0
            for row in commands
            if row["command"]["_type"] == "NewOrderRequest"
        ),
    }
    return {
        "main_checks": main,
        "candidate_checks": candidate,
        "failure_timestamp_ns": failure_time,
        "attempted_quote": {
            name: attempt.get(name) for name in ("side", "price_ticks", "quantity")
        }
        if attempt
        else None,
        "observed_bbo": bbo,
        "prior_accepted_orders": prior,
    }


def run_low_case(
    policy: str,
    source_root: str | Path,
    output_dir: str | Path,
    *,
    fixtures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run exactly one frozen low tape; callers own source selection and budget."""
    if policy not in POLICIES:
        raise ValueError("unknown frozen policy")
    source, destination = Path(source_root).resolve(), Path(output_dir).resolve()
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("witness destination must be new or empty")
    destination.mkdir(parents=True, exist_ok=True)
    fixture_file = (
        Path(fixtures_path).resolve()
        if fixtures_path is not None
        else source / EVIDENCE / "fixtures.json"
    )
    contract_file = fixture_file.with_name("contract.json")
    if (
        _digest(fixture_file) != FIXTURES_SHA256
        or _digest(contract_file) != CONTRACT_SHA256
    ):
        raise ValueError("frozen fixture/contract hash differs")
    fixtures = json.loads(fixture_file.read_text())
    contract = json.loads(contract_file.read_text())
    if tuple(fixtures["policies"]) != POLICIES:
        raise ValueError("frozen policies differ")
    modules, identities = {}, {}
    for name in contract["source_sha256"]:
        module = importlib.import_module("lobmm." + name.replace("/", "."))
        path = Path(module.__file__).resolve()
        if path != (source / "src/lobmm" / (name + ".py")).resolve():
            raise RuntimeError(f"wrong imported source: {path}")
        modules[name] = module
        identities[name] = {"path": str(path), "sha256": _digest(path)}
    auxiliaries = {}
    for name in ("validation", "queue_model", "data/fingerprint"):
        module = importlib.import_module("lobmm." + name.replace("/", "."))
        path = Path(module.__file__).resolve()
        if path != (source / "src/lobmm" / (name + ".py")).resolve():
            raise RuntimeError(f"wrong auxiliary source: {path}")
        modules[name] = module
        auxiliaries[name] = {"path": str(path), "sha256": _digest(path)}
    config_raw = _merge(
        modules["config"].AppConfig().model_dump(mode="json"),
        fixtures["low_engine_config_overrides"],
    )
    config_raw["strategy"]["name"] = policy
    config = modules["config"].AppConfig.model_validate(config_raw)
    resolved = config.model_dump(mode="json")
    config_bytes = json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode()
    tape = deepcopy(fixtures["low_engine_tape"])
    oracle = dictionary_book_oracle(tape)
    enum = modules["events"].EventType
    side_enum = modules["events"].Side
    events = [
        modules["events"].MarketEvent(
            t,
            seq,
            enum[kind],
            None if side is None else side_enum(side),
            price,
            quantity,
        )
        for t, seq, kind, side, price, quantity in tape
    ]
    certificate = modules["validation"].validate_event_stream(
        events, mode=modules["config"].ValidationMode.STRICT
    )
    cert = {
        "valid": certificate.valid,
        "issue_count": certificate.issue_count,
        "issues": _copy(certificate.issues),
        "event_count": certificate.event_count,
        "event_stream_sha256": certificate.event_stream_sha256,
        "mode": certificate.mode.value,
        "reconstructed_book": certificate.reconstructed_book,
        "required_nonempty": certificate.required_nonempty,
        "final_book": _copy(certificate.final_book),
        "diagnostics": dict(certificate.diagnostics),
    }
    event_sha = modules["data/fingerprint"].event_stream_sha256(events)
    inputs = {
        "policy": policy,
        "resolved_config": resolved,
        "effective_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "tape": tape,
        "event_stream_sha256": event_sha,
        "fixture_sha256": FIXTURES_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "provider_sha256": _digest(Path(__file__).resolve()),
        "imports": identities,
        "auxiliary_imports": auxiliaries,
        "certificate": cert,
    }
    _write(destination / "inputs.json", inputs)
    _write(
        destination / "oracle.json",
        {
            "historical_book": oracle,
            "fifo_budget": {
                "ask_price": 3,
                "initial_external_ahead": 1,
                "own_quantity": 1,
                "later_external_behind": 1,
                "trade40_quantity": 2,
                "expected_external_ahead_consumed": 1,
                "expected_own_fill": 1,
            },
            "note": "Queue pre/post snapshots are runtime copies. Terminal exported queue/cumulative order fields are not used as trajectory evidence.",
        },
    )
    trace: dict[str, Any] = {
        "scheduler": [],
        "decisions": [],
        "rounded_calls": [],
        "desired_attempts": [],
        "reports": [],
        "exchange": [],
    }
    active: dict[str, Any] = {"key": None, "decision": None}
    base = modules["strategies/base"].MarketMakingStrategy
    desired_type = modules["strategies/base"].DesiredQuote
    exchange_type = modules["exchange"].Exchange
    scheduler_type = modules["scheduler"].Scheduler
    original_run, original_decide, original_rounded = (
        scheduler_type.run,
        base.decide,
        base.rounded_quotes,
    )
    original_post, original_report = (
        desired_type.__post_init__,
        base.on_execution_report,
    )
    original_market, original_command, original_expire = (
        exchange_type.handle_market_event,
        exchange_type.handle_command,
        exchange_type.expire_order,
    )

    def observed_run(scheduler: Any, handler: Any, **kwargs: Any) -> Any:
        def observed(item: Any, instance: Any) -> Any:
            record = {
                "key": list(item.ordering_key),
                "parent_schedule_id": item.parent_schedule_id,
                "payload": _copy(item.payload),
            }
            trace["scheduler"].append(record)
            previous = active["key"]
            active["key"] = record["key"]
            try:
                result = handler(item, instance)
                record["completed"] = True
                return result
            except (Exception, KeyboardInterrupt) as exc:
                record["error"] = _error(exc)
                raise
            finally:
                active["key"] = previous

        return original_run(scheduler, observed, **kwargs)

    def observed_decide(
        strategy: Any,
        *,
        timestamp_ns: int,
        max_abs_inventory: int,
        allow_new_quotes: bool = True,
    ) -> Any:
        record = {
            "id": len(trace["decisions"]),
            "scheduler_key": deepcopy(active["key"]),
            "timestamp_ns": timestamp_ns,
            "max_abs_inventory": max_abs_inventory,
            "allow_new_quotes": allow_new_quotes,
            "before": _strategy_state(strategy),
        }
        trace["decisions"].append(record)
        previous = active["decision"]
        active["decision"] = record
        offset = len(strategy.decisions)
        try:
            actions = original_decide(
                strategy,
                timestamp_ns=timestamp_ns,
                max_abs_inventory=max_abs_inventory,
                allow_new_quotes=allow_new_quotes,
            )
            record["actions"] = _copy(actions)
            return actions
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = _error(exc)
            raise
        finally:
            record["after"] = _strategy_state(strategy)
            record["new_decisions"] = _copy(strategy.decisions[offset:])
            active["decision"] = previous

    def observed_rounded(reservation_ticks: float, half_spread_ticks: float) -> Any:
        decision = active["decision"] or {}
        record = {
            "decision_id": decision.get("id"),
            "timestamp_ns": decision.get("timestamp_ns"),
            "reservation_ticks": _copy(reservation_ticks),
            "half_spread_ticks": _copy(half_spread_ticks),
        }
        trace["rounded_calls"].append(record)
        try:
            rounded = original_rounded(reservation_ticks, half_spread_ticks)
            record["result"] = list(rounded)
            return rounded
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = _error(exc)
            raise

    def observed_post(quote: Any) -> None:
        decision = active["decision"] or {}
        record = {
            "decision_id": decision.get("id"),
            "timestamp_ns": decision.get("timestamp_ns"),
            "side": int(quote.side),
            "price_ticks": quote.price_ticks,
            "quantity": quote.quantity,
        }
        trace["desired_attempts"].append(record)
        try:
            original_post(quote)
            record["validated"] = True
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = _error(exc)
            raise

    def observed_report(
        strategy: Any, report: Any, *, notification_timestamp_ns: int
    ) -> Any:
        record = {
            "scheduler_key": deepcopy(active["key"]),
            "notification_timestamp_ns": notification_timestamp_ns,
            "report": _copy(report),
            "before": _strategy_state(strategy),
        }
        trace["reports"].append(record)
        try:
            result = original_report(
                strategy, report, notification_timestamp_ns=notification_timestamp_ns
            )
            return result
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = _error(exc)
            raise
        finally:
            record["after"] = _strategy_state(strategy)

    def exchange_call(
        exchange: Any, kind: str, operation: Any, *args: Any, **kwargs: Any
    ) -> Any:
        record = {
            "kind": kind,
            "scheduler_key": deepcopy(active["key"]),
            "pre": _exchange_state(exchange),
        }
        if kind == "market":
            record["event"] = args[0].as_dict()
        elif kind == "command":
            record.update(
                command=_copy(args[0]),
                arrival_timestamp_ns=kwargs["exchange_arrival_timestamp_ns"],
            )
        else:
            record.update(order_id=args[0], timestamp_ns=kwargs["timestamp_ns"])
        trace["exchange"].append(record)
        offset = len(exchange.registry.transitions)
        try:
            result = operation(exchange, *args, **kwargs)
            record.update(
                fills=_copy(result.fills),
                reports=_copy(result.reports),
                queue_update=_copy(result.queue_update),
            )
            return result
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = _error(exc)
            raise
        finally:
            record["post"] = _exchange_state(exchange)
            record["new_transitions"] = _copy(exchange.registry.transitions[offset:])

    def observed_market(exchange: Any, event: Any) -> Any:
        return exchange_call(exchange, "market", original_market, event)

    def observed_command(
        exchange: Any, command: Any, *, exchange_arrival_timestamp_ns: int
    ) -> Any:
        return exchange_call(
            exchange,
            "command",
            original_command,
            command,
            exchange_arrival_timestamp_ns=exchange_arrival_timestamp_ns,
        )

    def observed_expire(exchange: Any, order_id: str, *, timestamp_ns: int) -> Any:
        return exchange_call(
            exchange, "expire", original_expire, order_id, timestamp_ns=timestamp_ns
        )

    run_name = f"positive-price-low-{policy}"
    artifacts = destination / "artifacts" / run_name
    tables: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    parquet: dict[str, str] = {}
    error = None
    status = "complete"
    try:
        with ExitStack() as stack:
            for owner, name, replacement in (
                (scheduler_type, "run", observed_run),
                (base, "decide", observed_decide),
                (base, "rounded_quotes", staticmethod(observed_rounded)),
                (desired_type, "__post_init__", observed_post),
                (base, "on_execution_report", observed_report),
                (exchange_type, "handle_market_event", observed_market),
                (exchange_type, "handle_command", observed_command),
                (exchange_type, "expire_order", observed_expire),
            ):
                stack.enter_context(patch.object(owner, name, replacement))
            result = modules["backtest"].run_backtest(
                config,
                events,
                run_name=run_name,
                output_root=destination / "artifacts",
                generate_plots=False,
                input_validation=certificate,
                persist_artifacts=True,
            )
            artifacts = result.run_directory
            import polars as pl

            parquet = {path.name: _digest(path) for path in artifacts.glob("*.parquet")}
            tables = {
                name: pl.read_parquet(artifacts / (name + ".parquet")).to_dicts()
                for name in TABLE_NAMES
            }
            metrics = json.loads((artifacts / "metrics.json").read_text())
            diagnostics = json.loads((artifacts / "diagnostics.json").read_text())
            summary = json.loads((artifacts / "summary.json").read_text())
    except (Exception, KeyboardInterrupt) as exc:
        error = _error(exc)
        status = (
            "construction_error"
            if error == {"type": "ValueError", "message": EXPECTED_ERROR}
            else "unexpected_error"
        )
    finally:
        _write(destination / "trace.json", trace)
    evidence = _checks(
        trace,
        oracle,
        cert,
        error,
        tables,
        diagnostics,
        parquet,
        config.strategy.minimum_quote_lifetime_ns,
    )
    changed_sources = any(
        _digest(Path(item["path"])) != item["sha256"]
        for item in (*identities.values(), *auxiliaries.values())
    )
    if changed_sources:
        raise RuntimeError("imported source changed while executing the frozen witness")
    result = {
        "policy": policy,
        "status": status,
        "error": error,
        **evidence,
        "input": inputs,
        "imports": identities,
        "auxiliary_imports": auxiliaries,
        "artifact_directory": str(artifacts) if artifacts.exists() else None,
        "parquet_sha256": parquet,
        "tables": tables,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "summary": summary,
        "trace_path": str(destination / "trace.json"),
        "trace_sha256": _digest(destination / "trace.json"),
        "input_sha256": _digest(destination / "inputs.json"),
        "oracle_sha256": _digest(destination / "oracle.json"),
        "interpretation": "One frozen fictional low-price attempt. Observers copy values and delegate originals once; later policy-specific actions are retained. This is not cancellation-liveness, economic advantage or calibrated fill evidence.",
    }
    _write(destination / "result.json", result)
    return result
