"""Deterministic single-instrument event-driven backtest orchestration."""

from __future__ import annotations

import json
import time
import tracemalloc
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from lobmm.channels import (
    ChannelDelivery,
    FixedLatency,
    OrderedChannel,
    UniformJitterLatency,
)
from lobmm.client_stop import ClientStopController
from lobmm.config import AppConfig, dump_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.enums import (
    MarkPrice,
    OrderStatus,
    ReportType,
    SchedulerPhase,
    SessionEndPolicy,
    ShutdownPolicy,
    Side,
)
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange, ExchangeResult
from lobmm.metrics import build_metrics, classify_regimes, compute_markouts
from lobmm.orders import (
    CancelRequest,
    ExchangeOrder,
    ExecutionReport,
    NewOrderRequest,
)
from lobmm.portfolio import Portfolio, PortfolioSnapshot, select_mark_price
from lobmm.risk import OpenOrderExposure, RiskManager
from lobmm.scheduler import ScheduledEvent, Scheduler
from lobmm.strategies import make_strategy
from lobmm.strategies.base import StrategyAction
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.validation import ValidationResult, validate_event_stream


@dataclass(frozen=True, slots=True)
class TimerMessage:
    timer_sequence: int


@dataclass(frozen=True, slots=True)
class QuoteAgeCheck:
    order_id: str


@dataclass(frozen=True, slots=True)
class StrategyWake:
    reason: str


@dataclass(frozen=True, slots=True)
class SessionEnd:
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class ExpireOrder:
    order_id: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_name: str
    run_directory: Path
    metrics: dict[str, Any]
    diagnostics: dict[str, Any]


def run_backtest(
    config: AppConfig,
    events: list[MarketEvent],
    *,
    run_name: str,
    output_root: str | Path | None = None,
    generate_plots: bool | None = None,
    input_validation: ValidationResult | None = None,
    persist_artifacts: bool = True,
) -> BacktestResult:
    """Run one fully deterministic historical/synthetic replay."""

    if not run_name or run_name in {".", ".."}:
        raise ValueError("run_name must be a nonempty directory name")
    if "/" in run_name or "\\" in run_name:
        raise ValueError("run_name must not contain a directory separator")
    validation = input_validation or validate_event_stream(
        events,
        mode=config.data.validation_mode,
    )
    if input_validation is not None and (
        validation.event_count != len(events)
        or validation.event_stream_sha256 != event_stream_sha256(events)
        or validation.mode is not config.data.validation_mode
        or not validation.reconstructed_book
        or not validation.required_nonempty
    ):
        raise ValueError(
            "input validation certificate does not match the supplied event "
            "stream or required validation settings"
        )
    selected = _select_events(config, events)
    if not selected:
        raise ValueError("no market events remain after backtest filters")
    start_ns = selected[0].timestamp_ns
    end_ns = selected[-1].timestamp_ns
    client_stop_enabled = config.backtest.shutdown_policy is ShutdownPolicy.CLIENT_STOP
    stop_ns = (
        config.backtest.client_stop_timestamp_ns if client_stop_enabled else end_ns
    )
    horizon_ns = (
        config.backtest.observation_end_timestamp_ns if client_stop_enabled else None
    )
    assert stop_ns is not None
    if not start_ns <= stop_ns <= end_ns:
        raise ValueError("client stop must lie within the selected tape")
    destination_root = (
        Path(output_root) if output_root is not None else config.output.runs_directory
    )
    run_directory = destination_root / run_name
    should_plot = (
        config.output.write_plots if generate_plots is None else generate_plots
    )
    if should_plot and not persist_artifacts:
        raise ValueError("plots require persisted run artifacts")
    if persist_artifacts:
        run_directory.mkdir(parents=True, exist_ok=True)

    scheduler = Scheduler()
    portfolio = Portfolio(
        config.instrument,
        config.fees,
        identity_tolerance=Decimal("1e-12"),
    )
    risk = RiskManager(
        config.risk,
        session_start_ns=start_ns,
        session_end_ns=stop_ns,
        session_end_policy=config.backtest.session_end_policy,
    )
    strategy = make_strategy(config.strategy)
    runtime = StrategyRuntime(
        strategy=strategy,
        max_abs_inventory=config.risk.max_abs_inventory,
        depth=config.data.book_depth,
    )
    session_state = {"ended": False}

    def exchange_session_rejector(
        request: NewOrderRequest, _: tuple[ExchangeOrder, ...]
    ) -> str | None:
        if (
            not client_stop_enabled
            and session_state["ended"]
            and request.strategy_id != "session-policy"
        ):
            return "session_ended"
        return None

    exchange = Exchange(
        exchange_config=config.exchange,
        queue_config=config.queue_model,
        fee_config=config.fees,
        instrument_config=config.instrument,
        order_rejector=exchange_session_rejector,
    )

    market_channel: OrderedChannel[MarketEvent] = OrderedChannel(
        name="market_data",
        scheduler=scheduler,
        phase=SchedulerPhase.MARKET_DATA_DELIVERY,
        latency=_latency(
            config.latency.market_data_ns,
            config.latency.market_data_jitter_ns,
            config.random_seed + 11,
        ),
    )
    new_order_channel: OrderedChannel[NewOrderRequest] = OrderedChannel(
        name="new_orders",
        scheduler=scheduler,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        latency=_latency(
            config.latency.order_entry_ns,
            config.latency.order_entry_jitter_ns,
            config.random_seed + 12,
        ),
    )
    cancel_channel: OrderedChannel[CancelRequest] = OrderedChannel(
        name="cancellations",
        scheduler=scheduler,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        latency=_latency(
            config.latency.cancellation_ns,
            config.latency.cancellation_jitter_ns,
            config.random_seed + 13,
        ),
    )
    report_channel: OrderedChannel[ExecutionReport] = OrderedChannel(
        name="execution_reports",
        scheduler=scheduler,
        phase=SchedulerPhase.EXECUTION_REPORT_DELIVERY,
        latency=_latency(
            config.latency.fill_report_ns,
            config.latency.fill_report_jitter_ns,
            config.random_seed + 14,
        ),
    )

    def send_stop_cancel(request: CancelRequest) -> None:
        cancel_channel.send(request, send_timestamp_ns=request.send_timestamp_ns)

    client_stop = ClientStopController(send_stop_cancel)

    for event in selected:
        scheduler.schedule(
            timestamp_ns=event.timestamp_ns,
            phase=SchedulerPhase.MARKET,
            source_sequence=event.sequence_number,
            payload=event,
        )
    timer_sequence = 0
    timer_ns = start_ns
    while timer_ns <= stop_ns:
        scheduler.schedule(
            timestamp_ns=timer_ns,
            phase=SchedulerPhase.TIMER,
            source_sequence=timer_sequence,
            payload=TimerMessage(timer_sequence),
        )
        timer_sequence += 1
        timer_ns += config.backtest.timer_interval_ns
    scheduler.schedule(
        timestamp_ns=stop_ns,
        phase=SchedulerPhase.SESSION,
        payload=SessionEnd(stop_ns),
    )

    market_rows: list[dict[str, Any]] = []
    pnl_rows: list[dict[str, Any]] = []
    pending_wake_times: set[int] = set()
    pending_new: dict[str, NewOrderRequest] = {}
    fill_notification_ns: dict[str, int] = {}
    risk_cancel_sent: set[str] = set()
    risk_cancel_request_count = 0
    local_report_counter = 0
    session_ended = False
    entry_arrivals: dict[str, int] = {}
    unobserved_cancel_arrivals: list[dict[str, Any]] = []
    unobserved_expiries: list[dict[str, Any]] = []
    mark_source_ns: int | None = None
    last_market_ns: int | None = None

    def append_snapshot(snapshot: PortfolioSnapshot) -> None:
        row = _dataclass_row(snapshot)
        pnl_rows.append(row)

    def current_mark(
        fallback: int | None = None, *, inventory: int | None = None
    ) -> Decimal:
        nonlocal mark_source_ns
        best_bid = exchange.book.best_bid
        best_ask = exchange.book.best_ask
        marked_inventory = portfolio.inventory if inventory is None else inventory
        # A conservative position mark needs only its liquidation-side quote.
        # The opposite side may disappear after a depth-consuming fill.
        if config.backtest.mark_price is MarkPrice.CONSERVATIVE:
            if marked_inventory > 0 and best_bid is not None:
                mark_source_ns = last_market_ns
                return Decimal(best_bid)
            if marked_inventory < 0 and best_ask is not None:
                mark_source_ns = last_market_ns
                return Decimal(best_ask)
        if best_bid is not None and best_ask is not None:
            mark_source_ns = last_market_ns
            return select_mark_price(
                config.backtest.mark_price,
                inventory=marked_inventory,
                best_bid_ticks=best_bid,
                best_ask_ticks=best_ask,
                microprice_ticks=exchange.book.microprice,
            )
        if portfolio.last_mark_ticks is not None:
            return portfolio.last_mark_ticks
        return Decimal(fallback or 0)

    def schedule_reports(result: ExchangeResult) -> None:
        for report in result.reports:
            report_channel.send(
                report,
                send_timestamp_ns=report.exchange_timestamp_ns,
            )

    def apply_exchange_result(result: ExchangeResult) -> None:
        for fill in result.fills:
            snapshot = portfolio.record_fill(
                side=fill.side,
                quantity=fill.quantity,
                price_ticks=fill.price_ticks,
                timestamp_ns=fill.exchange_fill_timestamp_ns,
                fee=fill.fee,
                rebate=fill.rebate,
                liquidity_role=fill.liquidity_role,
                mark_ticks=current_mark(
                    fill.price_ticks,
                    inventory=portfolio.inventory + int(fill.side) * fill.quantity,
                ),
            )
            append_snapshot(snapshot)
            risk.observe_pnl(
                snapshot.net_pnl,
                timestamp_ns=fill.exchange_fill_timestamp_ns,
            )
        schedule_reports(result)
        if (
            risk.kill_switch_active
            and result.fills
            and not (client_stop_enabled and session_ended)
        ):
            request_risk_cancels(
                {order.order_id for order in exchange.live_orders},
                timestamp_ns=max(
                    fill.exchange_fill_timestamp_ns for fill in result.fills
                ),
            )

    def schedule_wake(
        scheduler_instance: Scheduler, *, timestamp_ns: int, reason: str
    ) -> None:
        if timestamp_ns in pending_wake_times:
            return
        scheduler_instance.schedule(
            timestamp_ns=timestamp_ns,
            phase=SchedulerPhase.STRATEGY_DECISION,
            payload=StrategyWake(reason),
        )
        pending_wake_times.add(timestamp_ns)

    def accepted_exposures() -> tuple[OpenOrderExposure, ...]:
        return tuple(
            OpenOrderExposure(
                order_id=order.order_id,
                side=order.side,
                remaining_quantity=order.remaining_quantity,
                accepted_timestamp_ns=order.exchange_arrival_timestamp_ns,
                is_quote=order.strategy_id != "session-policy",
            )
            for order in exchange.live_orders
        )

    def live_exposures() -> tuple[OpenOrderExposure, ...]:
        live = list(accepted_exposures())
        live.extend(
            OpenOrderExposure(
                order_id=f"pending:{request.client_order_id}",
                side=request.side,
                remaining_quantity=request.quantity,
                accepted_timestamp_ns=request.send_timestamp_ns,
            )
            for request in pending_new.values()
        )
        return tuple(live)

    def local_risk_rejection(
        request: NewOrderRequest,
        *,
        timestamp_ns: int,
        reason: str,
    ) -> None:
        nonlocal local_report_counter
        local_report_counter += 1
        report = ExecutionReport(
            report_id=f"LR{local_report_counter:012d}",
            report_type=ReportType.REJECTED,
            exchange_timestamp_ns=timestamp_ns,
            order_id="",
            client_order_id=request.client_order_id,
            order_status=OrderStatus.REJECTED,
            remaining_quantity=request.quantity,
            reason=reason,
        )
        report_channel.send(report, send_timestamp_ns=timestamp_ns)

    def send_strategy_actions(
        actions: tuple[StrategyAction, ...], *, timestamp_ns: int
    ) -> None:
        for action in actions:
            if isinstance(action, CancelRequest):
                message_decision = risk.register_message(timestamp_ns=timestamp_ns)
                if not message_decision.approved and not risk.kill_switch_active:
                    runtime.record_action_blocked(
                        action,
                        timestamp_ns=timestamp_ns,
                        reason=(
                            message_decision.reason.value
                            if message_decision.reason
                            else message_decision.detail
                        ),
                    )
                    continue
                cancel_channel.send(action, send_timestamp_ns=timestamp_ns)
                runtime.record_action_sent(action, timestamp_ns=timestamp_ns)
                continue
            decision = risk.check_order(
                side=action.side,
                quantity=action.quantity,
                timestamp_ns=timestamp_ns,
                inventory=portfolio.inventory,
                open_orders=live_exposures(),
                price_ticks=action.price_ticks,
                is_quote=True,
                may_rest=action.rest_unfilled,
                spread_ticks=exchange.book.spread,
                volatility_ticks=strategy.recent_volatility_ticks,
            )
            if not decision.approved:
                rejection_reason = (
                    decision.reason.value if decision.reason else decision.detail
                )
                runtime.record_action_blocked(
                    action,
                    timestamp_ns=timestamp_ns,
                    reason=rejection_reason,
                )
                local_risk_rejection(
                    action,
                    timestamp_ns=timestamp_ns,
                    reason=rejection_reason,
                )
                continue
            pending_new[action.client_order_id] = action
            arrival = new_order_channel.send(action, send_timestamp_ns=timestamp_ns)
            if client_stop_enabled:
                client_stop.sent_entry(action)
                entry_arrivals[action.client_order_id] = arrival.timestamp_ns
            runtime.record_action_sent(action, timestamp_ns=timestamp_ns)

    def request_risk_cancels(
        order_ids: set[str] | tuple[str, ...],
        *,
        timestamp_ns: int,
    ) -> None:
        nonlocal risk_cancel_request_count
        requested = set(order_ids)
        pending_strategy_cancels = {
            quote.exchange_order_id
            for quote in strategy.active_quotes
            if quote.pending_cancel and quote.exchange_order_id is not None
        }
        for order in exchange.live_orders:
            if (
                order.order_id not in requested
                or order.order_id in risk_cancel_sent
                or order.order_id in pending_strategy_cancels
            ):
                continue
            cancel = CancelRequest(
                decision_timestamp_ns=timestamp_ns,
                send_timestamp_ns=timestamp_ns,
                order_id=order.order_id,
            )
            cancel_channel.send(cancel, send_timestamp_ns=timestamp_ns)
            risk_cancel_sent.add(order.order_id)
            risk_cancel_request_count += 1

    def enforce_continuous_risk(timestamp_ns: int) -> None:
        snapshot = portfolio.snapshot(timestamp_ns=timestamp_ns)
        outcome = risk.continuous_check(
            timestamp_ns=timestamp_ns,
            net_pnl=snapshot.net_pnl,
            inventory=portfolio.inventory,
            open_orders=accepted_exposures(),
        )
        request_risk_cancels(
            outcome.cancel_order_ids,
            timestamp_ns=timestamp_ns,
        )

    def handler(scheduled: ScheduledEvent[Any], scheduler_instance: Scheduler) -> None:
        nonlocal session_ended, last_market_ns
        timestamp_ns = scheduled.timestamp_ns
        payload = scheduled.payload
        if scheduled.phase is SchedulerPhase.MARKET:
            assert isinstance(payload, MarketEvent)
            last_market_ns = timestamp_ns
            result = exchange.handle_market_event(payload)
            apply_exchange_result(result)
            market_channel.send(payload, send_timestamp_ns=payload.timestamp_ns)
            view = exchange.book.view(
                payload.timestamp_ns,
                payload.sequence_number,
                config.data.book_depth,
            )
            market_rows.append(
                {
                    "timestamp_ns": payload.timestamp_ns,
                    "sequence_number": payload.sequence_number,
                    "event_type": payload.event_type.value,
                    "side": int(payload.side) if payload.side is not None else None,
                    "price_ticks": payload.price_ticks,
                    "quantity": payload.quantity,
                    "best_bid_ticks": view.best_bid_ticks,
                    "best_ask_ticks": view.best_ask_ticks,
                    "best_bid_quantity": view.best_bid_quantity,
                    "best_ask_quantity": view.best_ask_quantity,
                    "midpoint_ticks": view.midpoint_ticks,
                    "microprice_ticks": view.microprice_ticks,
                    "imbalance": view.imbalance,
                    "spread_ticks": exchange.book.spread,
                }
            )
            can_mark = (
                view.best_bid_ticks is not None and view.best_ask_ticks is not None
            ) or (
                config.backtest.mark_price is MarkPrice.CONSERVATIVE
                and (
                    (portfolio.inventory > 0 and view.best_bid_ticks is not None)
                    or (portfolio.inventory < 0 and view.best_ask_ticks is not None)
                )
            )
            if can_mark:
                snapshot = portfolio.mark_to_market(
                    current_mark(),
                    timestamp_ns=payload.timestamp_ns,
                )
                append_snapshot(snapshot)
                risk.observe_pnl(snapshot.net_pnl, timestamp_ns=payload.timestamp_ns)
            if risk.kill_switch_active and not (client_stop_enabled and session_ended):
                request_risk_cancels(
                    {order.order_id for order in exchange.live_orders},
                    timestamp_ns=timestamp_ns,
                )
            return

        if scheduled.phase is SchedulerPhase.MARKET_DATA_DELIVERY:
            assert isinstance(payload, ChannelDelivery)
            assert isinstance(payload.payload, MarketEvent)
            runtime.deliver_market(
                payload.payload, notification_timestamp_ns=timestamp_ns
            )
            schedule_wake(
                scheduler_instance,
                timestamp_ns=timestamp_ns,
                reason="market_data",
            )
            return

        if scheduled.phase is SchedulerPhase.EXECUTION_REPORT_DELIVERY:
            assert isinstance(payload, ChannelDelivery)
            assert isinstance(payload.payload, ExecutionReport)
            report = payload.payload
            runtime.deliver_report(report, notification_timestamp_ns=timestamp_ns)
            if client_stop_enabled:
                client_stop.delivered(report, timestamp_ns)
            if report.fill is not None:
                fill_notification_ns[report.fill.fill_id] = timestamp_ns
            schedule_wake(
                scheduler_instance,
                timestamp_ns=timestamp_ns,
                reason="execution_report",
            )
            return

        if scheduled.phase is SchedulerPhase.EXCHANGE_COMMAND:
            assert isinstance(payload, ChannelDelivery)
            command = payload.payload
            if client_stop_enabled and timestamp_ns > end_ns:
                # Without later tape, intervening fills and hence the remaining
                # quantity at cancellation cannot be established. Preserve the
                # last covered reservation rather than inventing a terminal ACK.
                if isinstance(command, CancelRequest):
                    unobserved_cancel_arrivals.append(
                        {
                            "client_order_id": command.client_order_id,
                            "order_id": command.order_id,
                            "timestamp_ns": timestamp_ns,
                        }
                    )
                return
            if isinstance(command, NewOrderRequest):
                pending_new.pop(command.client_order_id, None)
                if (
                    not client_stop_enabled
                    and session_ended
                    and command.strategy_id != "session-policy"
                ):
                    local_risk_rejection(
                        command,
                        timestamp_ns=timestamp_ns,
                        reason="session_end",
                    )
                    return
            if not isinstance(command, (NewOrderRequest, CancelRequest)):
                raise TypeError(f"unsupported channel command: {command!r}")
            result = exchange.handle_command(
                command, exchange_arrival_timestamp_ns=timestamp_ns
            )
            apply_exchange_result(result)
            if isinstance(command, NewOrderRequest):
                try:
                    order = exchange.registry.get_by_client_id(command.client_order_id)
                except Exception:
                    order = None
                if (
                    order is not None
                    and order.is_fillable
                    and order.expire_timestamp_ns is not None
                    and order.expire_timestamp_ns >= timestamp_ns
                ):
                    scheduler_instance.schedule(
                        timestamp_ns=order.expire_timestamp_ns,
                        phase=SchedulerPhase.SESSION,
                        payload=ExpireOrder(order.order_id),
                    )
                if (
                    order is not None
                    and order.is_fillable
                    and order.strategy_id != "session-policy"
                ):
                    age_check_ns = (
                        order.exchange_arrival_timestamp_ns
                        + config.risk.max_quote_age_ns
                        + 1
                    )
                    if age_check_ns <= stop_ns:
                        scheduler_instance.schedule(
                            timestamp_ns=age_check_ns,
                            phase=SchedulerPhase.TIMER,
                            payload=QuoteAgeCheck(order.order_id),
                        )
            return

        if scheduled.phase is SchedulerPhase.TIMER:
            assert isinstance(payload, (TimerMessage, QuoteAgeCheck))
            if client_stop_enabled and session_ended:
                return
            enforce_continuous_risk(timestamp_ns)
            if isinstance(payload, TimerMessage):
                schedule_wake(
                    scheduler_instance,
                    timestamp_ns=timestamp_ns,
                    reason="timer",
                )
            return

        if scheduled.phase is SchedulerPhase.STRATEGY_DECISION:
            pending_wake_times.discard(timestamp_ns)
            if client_stop_enabled and session_ended:
                return
            warm = runtime.market_messages_delivered >= config.backtest.warmup_events
            cutoff = max(
                start_ns,
                stop_ns - config.risk.prohibit_new_quotes_last_ns,
            )
            allow_new = (
                warm
                and not session_ended
                and not risk.kill_switch_active
                and timestamp_ns < cutoff
            )
            actions = runtime.decide(
                timestamp_ns=timestamp_ns, allow_new_quotes=allow_new
            )
            send_strategy_actions(actions, timestamp_ns=timestamp_ns)
            return

        if scheduled.phase is SchedulerPhase.SESSION:
            if isinstance(payload, ExpireOrder):
                if client_stop_enabled and timestamp_ns > end_ns:
                    unobserved_expiries.append(
                        {"order_id": payload.order_id, "timestamp_ns": timestamp_ns}
                    )
                    return
                result = exchange.expire_order(
                    payload.order_id, timestamp_ns=timestamp_ns
                )
                apply_exchange_result(result)
                return
            assert isinstance(payload, SessionEnd)
            session_ended = True
            session_state["ended"] = True
            if client_stop_enabled:
                client_stop.stop(timestamp_ns)
                return
            for order in tuple(exchange.live_orders):
                apply_exchange_result(
                    exchange.expire_order(order.order_id, timestamp_ns=timestamp_ns)
                )
            if (
                config.backtest.session_end_policy is SessionEndPolicy.LIQUIDATE
                and portfolio.inventory != 0
            ):
                side = Side.ASK if portfolio.inventory > 0 else Side.BID
                price = (
                    exchange.book.best_bid
                    if side is Side.ASK
                    else exchange.book.best_ask
                )
                if price is not None:
                    liquidation = NewOrderRequest(
                        client_order_id="session-liquidation",
                        side=side,
                        price_ticks=price,
                        quantity=abs(portfolio.inventory),
                        strategy_id="session-policy",
                        decision_timestamp_ns=timestamp_ns,
                        send_timestamp_ns=timestamp_ns,
                        post_only=False,
                        rest_unfilled=False,
                    )
                    new_order_channel.send(liquidation, send_timestamp_ns=timestamp_ns)
            return

        raise TypeError(
            f"no handler for scheduled phase {scheduled.phase}: {payload!r}"
        )

    peak_memory: int | None = None
    if config.backtest.track_memory:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        scheduler.run(handler, until_ns=horizon_ns)
    finally:
        wall_seconds = time.perf_counter() - started
        if config.backtest.track_memory:
            _, peak_memory = tracemalloc.get_traced_memory()
            tracemalloc.stop()

    last_economic_snapshot_ns = pnl_rows[-1]["timestamp_ns"] if pnl_rows else None
    if client_stop_enabled:
        assert horizon_ns is not None
        append_snapshot(portfolio.snapshot(timestamp_ns=horizon_ns))
    elif not pnl_rows:
        append_snapshot(portfolio.snapshot(timestamp_ns=end_ns))
    classify_regimes(market_rows)
    order_rows = _order_rows(exchange)
    fill_rows = _fill_rows(exchange, fill_notification_ns)
    inventory_rows = [
        {
            "timestamp_ns": row.get("timestamp_ns"),
            "inventory": row["inventory"],
            "current_gross_exposure": row["current_gross_exposure"],
            "peak_gross_exposure": row["peak_gross_exposure"],
        }
        for row in pnl_rows
    ]
    quote_rows = [_dataclass_row(row) for row in strategy.decisions]
    risk_rows = [_dataclass_row(row) for row in risk.events]
    markout_rows = compute_markouts(
        fill_rows,
        market_rows,
        config.metrics.markout_horizons_ns,
        tick_size=config.instrument.tick_size,
    )
    dataset_provenance = config.data.provenance.artifact()
    processed_market = [
        event
        for event in selected
        if horizon_ns is None or event.timestamp_ns <= horizon_ns
    ]
    stream_hash = event_stream_sha256(processed_market)
    diagnostics: dict[str, Any] = {
        "events_processed": len(processed_market),
        "scheduler_events_processed": scheduler.processed_count,
        "wall_clock_seconds": wall_seconds,
        "events_per_second": (
            len(processed_market) / wall_seconds if wall_seconds > 0 else None
        ),
        "peak_memory_bytes": peak_memory,
        "random_seed": config.random_seed,
        "market_data_messages_delivered": runtime.market_messages_delivered,
        "execution_reports_delivered": runtime.reports_delivered,
        "desired_quote_attempts": strategy.desired_quote_attempts,
        "retry_suppressed_quote_attempts": (strategy.retry_suppressed_quote_attempts),
        "risk_blocked_quote_attempts": strategy.risk_blocked_quote_attempts,
        "exchange_rejected_quote_attempts": (strategy.exchange_rejected_quote_attempts),
        "rejected_quote_attempts": strategy.rejected_quote_attempts,
        "submitted_order_messages": strategy.submitted_orders,
        "cancel_attempts": strategy.cancel_attempts,
        "risk_blocked_cancel_attempts": strategy.risk_blocked_cancel_attempts,
        "exchange_rejected_cancel_attempts": strategy.rejected_cancel_attempts,
        "cancel_request_messages": strategy.cancel_requests,
        "open_orders_end": len(exchange.live_orders),
        "end_inventory": portfolio.inventory,
        "max_abs_inventory": config.risk.max_abs_inventory,
        "kill_switch_active": risk.kill_switch_active,
        "true_book_diagnostics": dict(exchange.book.diagnostics),
        "observed_book_diagnostics": dict(runtime.observed_book.diagnostics),
        "queue_levels_remaining": len(exchange.queue_model.active_levels),
        "input_validation_issue_count": validation.issue_count,
        "input_validation_diagnostics": dict(validation.diagnostics),
        "input_validation_certificate": {
            "event_count": validation.event_count,
            "event_stream_sha256": validation.event_stream_sha256,
            "mode": validation.mode.value,
            "reconstructed_book": validation.reconstructed_book,
            "required_nonempty": validation.required_nonempty,
        },
        "risk_cancel_request_messages": risk_cancel_request_count,
        "dataset_provenance": dataset_provenance,
        "event_stream_sha256": stream_hash,
        "synthetic_demonstration": config.data.provenance.synthetic_demonstration,
        "performance_note": (
            "Measured unprofiled Python wall-clock throughput; timestamp precision "
            "is not a latency-performance claim."
            if not config.backtest.track_memory
            else "Measured Python wall-clock throughput with tracemalloc enabled; "
            "timestamp precision is not a latency-performance claim."
        ),
    }
    if client_stop_enabled:
        assert horizon_ns is not None
        outstanding: list[dict[str, Any]] = [
            {
                "client_order_id": order.client_order_id,
                "side": int(order.side),
                "remaining_quantity": order.remaining_quantity,
            }
            for order in exchange.live_orders
        ]
        in_flight: list[dict[str, Any]] = [
            {
                "client_order_id": cid,
                "side": int(request.side),
                "quantity": request.quantity,
                "scheduled_arrival_timestamp_ns": entry_arrivals[cid],
                "outcome_unobserved_after_tape": entry_arrivals[cid] > end_ns,
            }
            for cid, request in pending_new.items()
        ]
        min_inv = (
            portfolio.inventory
            - sum(
                order["remaining_quantity"]
                for order in outstanding
                if order["side"] == -1
            )
            - sum(order["quantity"] for order in in_flight if order["side"] == -1)
        )
        max_inv = (
            portfolio.inventory
            + sum(
                order["remaining_quantity"]
                for order in outstanding
                if order["side"] == 1
            )
            + sum(order["quantity"] for order in in_flight if order["side"] == 1)
        )
        pending_channels: dict[str, int] = {}
        for pending_event in scheduler.pending():
            if isinstance(pending_event.payload, ChannelDelivery):
                name = pending_event.payload.channel_name
                pending_channels[name] = pending_channels.get(name, 0) + 1
        diagnostics["client_stop"] = {
            "stop_timestamp_ns": stop_ns,
            "observation_end_timestamp_ns": horizon_ns,
            "tape_end_timestamp_ns": end_ns,
            "venue_observation_end_timestamp_ns": min(end_ns, horizon_ns),
            "last_observed_market_timestamp_ns": market_rows[-1]["timestamp_ns"],
            "last_economic_snapshot_ns": last_economic_snapshot_ns,
            "mark_source_timestamp_ns": mark_source_ns,
            "mark_age_ns": horizon_ns - mark_source_ns
            if mark_source_ns is not None
            else None,
            "known_inventory": runtime.known_inventory,
            "outstanding_orders": outstanding,
            "in_flight_entries": in_flight,
            "unresolved_client_order_ids": list(client_stop.entries),
            "cancel_requests": [
                {
                    "client_order_id": request.client_order_id,
                    "send_timestamp_ns": request.send_timestamp_ns,
                }
                for request in client_stop.cancel_sends
            ],
            "execution_reports": [
                {
                    "client_order_id": report.client_order_id,
                    "type": report.report_type.value,
                    "status": report.order_status.value,
                    "reason": report.reason,
                    "exchange_timestamp_ns": report.exchange_timestamp_ns,
                    "notification_timestamp_ns": ns,
                    "fill_id": report.fill.fill_id if report.fill else None,
                }
                for ns, report in client_stop.reports
            ],
            "unobserved_cancel_arrivals": unobserved_cancel_arrivals,
            "unobserved_expiries": unobserved_expiries,
            "pending_channels": pending_channels,
            "reachable_inventory_min": min_inv,
            "reachable_inventory_max": max_inv,
            "accounting_complete": not outstanding and not in_flight,
            "client_knowledge_complete": not client_stop.entries,
            "execution_model_note": "Venue execution and termination are observed only through tape end. Later entries, cancels and expiries retain unresolved reservations because intervening fills lack tape coverage. In-tape-generated reports continue to the inclusive horizon; marks are historical observations, not liquidation proceeds.",
        }
    metrics = build_metrics(
        orders=order_rows,
        fills=fill_rows,
        inventory=inventory_rows,
        pnl=pnl_rows,
        market_states=market_rows,
        markouts=markout_rows,
        risk_events=risk_rows,
        quote_messages=quote_rows,
        diagnostics=diagnostics,
        tick_size=config.instrument.tick_size,
        near_limit_fraction=config.metrics.near_limit_fraction,
    )
    if client_stop_enabled:
        metrics["client_stop"] = diagnostics["client_stop"]
    summary = {
        "run_name": run_name,
        "strategy": config.strategy.name.value,
        "symbol": config.instrument.symbol,
        "event_count": len(processed_market),
        "fill_count": len(fill_rows),
        "end_inventory": portfolio.inventory,
        "net_pnl": metrics["pnl"]["net_pnl"],
        "dataset_provenance": dataset_provenance,
        "event_stream_sha256": stream_hash,
        "synthetic_demonstration": diagnostics["synthetic_demonstration"],
        "disclaimer": metrics["research_disclaimer"],
    }
    if client_stop_enabled:
        summary["client_stop"] = diagnostics["client_stop"]

    if persist_artifacts:
        _write_outputs(
            run_directory,
            config=config,
            summary=summary,
            metrics=metrics,
            diagnostics=diagnostics,
            orders=order_rows,
            fills=fill_rows,
            inventory=inventory_rows,
            pnl=pnl_rows,
            quotes=quote_rows,
            risk_events=risk_rows,
            market=market_rows,
            markouts=markout_rows,
        )
        if should_plot:
            from lobmm.report import generate_report

            generate_report(run_directory)
    return BacktestResult(run_name, run_directory, metrics, diagnostics)


def _latency(
    base_ns: int, jitter_ns: int, seed: int
) -> FixedLatency | UniformJitterLatency:
    if jitter_ns:
        return UniformJitterLatency(
            base_delay_ns=base_ns, jitter_ns=jitter_ns, seed=seed
        )
    return FixedLatency(base_ns)


def _select_events(config: AppConfig, events: list[MarketEvent]) -> list[MarketEvent]:
    selected = [
        event
        for event in events
        if (
            config.backtest.start_timestamp_ns is None
            or event.timestamp_ns >= config.backtest.start_timestamp_ns
        )
        and (
            config.backtest.end_timestamp_ns is None
            or event.timestamp_ns <= config.backtest.end_timestamp_ns
        )
    ]
    if config.backtest.event_limit is not None:
        return selected[: config.backtest.event_limit]
    return selected


def _order_rows(exchange: Exchange) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for transition in exchange.registry.transitions:
        order = exchange.registry.get(transition.order_id)
        output.append(
            {
                **_dataclass_row(transition),
                "client_order_id": order.client_order_id,
                "strategy_id": order.strategy_id,
                "side": int(order.side),
                "price_ticks": order.price_ticks,
                "original_quantity": order.original_quantity,
                "cumulative_filled_quantity": order.cumulative_filled_quantity,
                "creation_timestamp_ns": order.creation_timestamp_ns,
                "send_timestamp_ns": order.send_timestamp_ns,
                "exchange_arrival_timestamp_ns": (order.exchange_arrival_timestamp_ns),
                "average_fill_price_ticks": (
                    float(order.average_fill_price_ticks)
                    if order.average_fill_price_ticks is not None
                    else None
                ),
                "queue_ahead_estimate": order.queue_position.total_ahead,
                "external_behind_estimate": (order.queue_position.external_behind),
            }
        )
    return output


def _fill_rows(
    exchange: Exchange, notification_times: dict[str, int]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for fill in exchange.registry.fills:
        order = exchange.registry.get(fill.order_id)
        output.append(
            {
                **_dataclass_row(fill),
                "decision_timestamp_ns": order.creation_timestamp_ns,
                "send_timestamp_ns": order.send_timestamp_ns,
                "exchange_arrival_timestamp_ns": (order.exchange_arrival_timestamp_ns),
                "strategy_notification_timestamp_ns": notification_times.get(
                    fill.fill_id
                ),
                "queue_ahead_estimate": order.queue_position.total_ahead,
            }
        )
    return output


def _dataclass_row(value: object) -> dict[str, Any]:
    if not is_dataclass(value):
        raise TypeError(f"expected dataclass instance, got {type(value)!r}")
    return {
        field.name: _serializable(getattr(value, field.name)) for field in fields(value)
    }


def _serializable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _dataclass_row(value)
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _write_outputs(
    run_directory: Path,
    *,
    config: AppConfig,
    summary: dict[str, Any],
    metrics: dict[str, Any],
    diagnostics: dict[str, Any],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    pnl: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    risk_events: list[dict[str, Any]],
    market: list[dict[str, Any]],
    markouts: list[dict[str, Any]],
) -> None:
    (run_directory / "run_config.yaml").write_text(
        yaml.safe_dump(dump_config(config), sort_keys=False),
        encoding="utf-8",
    )
    for name, document in (
        ("summary.json", summary),
        ("metrics.json", metrics),
        ("diagnostics.json", diagnostics),
    ):
        (run_directory / name).write_text(
            json.dumps(_serializable(document), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    schemas: dict[str, dict[str, Any]] = {
        "orders": {
            "transition_id": pl.String,
            "order_id": pl.String,
            "new_status": pl.String,
            "timestamp_ns": pl.Int64,
        },
        "fills": {
            "fill_id": pl.String,
            "order_id": pl.String,
            "side": pl.Int64,
            "quantity": pl.Int64,
            "price_ticks": pl.Int64,
            "exchange_fill_timestamp_ns": pl.Int64,
        },
        "inventory": {
            "timestamp_ns": pl.Int64,
            "inventory": pl.Int64,
        },
        "pnl": {
            "timestamp_ns": pl.Int64,
            "gross_pnl": pl.Float64,
            "net_pnl": pl.Float64,
        },
        "quotes": {
            "timestamp_ns": pl.Int64,
            "side": pl.Int64,
            "action": pl.String,
        },
        "risk_events": {
            "timestamp_ns": pl.Int64,
            "reason": pl.String,
            "detail": pl.String,
        },
        "market": {
            "timestamp_ns": pl.Int64,
            "sequence_number": pl.Int64,
            "midpoint_ticks": pl.Float64,
        },
        "markouts": {
            "fill_id": pl.String,
            "horizon_ns": pl.Int64,
            "markout_ticks": pl.Float64,
        },
    }
    tables = {
        "orders": orders,
        "fills": fills,
        "inventory": inventory,
        "pnl": pnl,
        "quotes": quotes,
        "risk_events": risk_events,
        "market": market,
        "markouts": markouts,
    }
    for name, rows in tables.items():
        if rows:
            frame = pl.DataFrame(rows, infer_schema_length=None)
        else:
            frame = pl.DataFrame(schema=schemas[name])
        frame.write_parquet(run_directory / f"{name}.parquet")
