from __future__ import annotations

from typing import Any

import polars as pl

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig
from lobmm.enums import (
    EventType,
    OrderStatus,
    ReportType,
    SchedulerPhase,
    Side,
)
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.scheduler import ScheduledEvent, Scheduler


def event(
    timestamp_ns: int,
    sequence_number: int,
    event_type: EventType,
    side: Side,
    price_ticks: int,
    quantity: int,
) -> MarketEvent:
    return MarketEvent(
        timestamp_ns=timestamp_ns,
        sequence_number=sequence_number,
        event_type=event_type,
        side=side,
        price_ticks=price_ticks,
        quantity=quantity,
    )


def causal_config(*, order_latency_ns: int = 2) -> AppConfig:
    base = AppConfig()
    return base.model_copy(
        update={
            "latency": base.latency.model_copy(
                update={
                    "market_data_ns": 5,
                    "order_entry_ns": order_latency_ns,
                    "cancellation_ns": 2,
                    "fill_report_ns": 5,
                    "market_data_jitter_ns": 0,
                    "order_entry_jitter_ns": 0,
                    "cancellation_jitter_ns": 0,
                    "fill_report_jitter_ns": 0,
                }
            ),
            "strategy": base.strategy.model_copy(
                update={
                    "order_size": 2,
                    "minimum_quote_lifetime_ns": 0,
                    "refresh_interval_ns": 10_000,
                    "stale_after_ns": 20_000,
                }
            ),
            "backtest": base.backtest.model_copy(
                update={
                    "warmup_events": 2,
                    "timer_interval_ns": 1_000,
                }
            ),
            "risk": base.risk.model_copy(
                update={
                    "minimum_spread_ticks": 0,
                    "prohibit_new_quotes_last_ns": 0,
                }
            ),
            "output": base.output.model_copy(update={"write_plots": False}),
        }
    )


def test_future_divergent_streams_have_identical_action_prefix(
    tmp_path: Any,
) -> None:
    prefix = [
        event(0, 1, EventType.ADD, Side.BID, 100, 50),
        event(0, 2, EventType.ADD, Side.ASK, 102, 50),
        event(20, 3, EventType.ADD, Side.BID, 99, 1),
        event(40, 4, EventType.CANCEL, Side.BID, 99, 1),
    ]
    suffix_a = [
        event(100, 5, EventType.CANCEL, Side.ASK, 102, 50),
        event(100, 6, EventType.ADD, Side.ASK, 104, 50),
        event(200, 7, EventType.ADD, Side.BID, 97, 1),
    ]
    suffix_b = [
        event(100, 5, EventType.CANCEL, Side.BID, 100, 50),
        event(100, 6, EventType.ADD, Side.BID, 98, 50),
        event(200, 7, EventType.ADD, Side.BID, 97, 1),
    ]
    config = causal_config()
    first = run_backtest(
        config,
        prefix + suffix_a,
        run_name="future-a",
        output_root=tmp_path,
        generate_plots=False,
    )
    second = run_backtest(
        config,
        prefix + suffix_b,
        run_name="future-b",
        output_root=tmp_path,
        generate_plots=False,
    )
    first_actions = pl.read_parquet(first.run_directory / "quotes.parquet")
    second_actions = pl.read_parquet(second.run_directory / "quotes.parquet")
    prefix_a = first_actions.filter(pl.col("timestamp_ns") <= 80)
    prefix_b = second_actions.filter(pl.col("timestamp_ns") <= 80)

    assert prefix_a.height > 0
    assert prefix_a.equals(prefix_b)
    assert not first_actions.equals(second_actions)


def test_same_timestamp_trade_precedes_cancel_and_new_order() -> None:
    exchange = Exchange()
    exchange.handle_market_event(event(1, 1, EventType.ADD, Side.BID, 100, 10))
    exchange.handle_market_event(event(1, 2, EventType.ADD, Side.ASK, 102, 10))
    old_request = NewOrderRequest(
        client_order_id="old",
        side=Side.BID,
        price_ticks=100,
        quantity=3,
        strategy_id="s",
        decision_timestamp_ns=2,
        send_timestamp_ns=2,
    )
    exchange.handle_command(old_request, exchange_arrival_timestamp_ns=3)
    exchange.handle_market_event(event(4, 3, EventType.ADD, Side.BID, 100, 5))
    old_order = exchange.registry.get_by_client_id("old")
    cancel = CancelRequest(
        order_id=old_order.order_id,
        decision_timestamp_ns=8,
        send_timestamp_ns=9,
    )
    new_request = NewOrderRequest(
        client_order_id="new",
        side=Side.BID,
        price_ticks=100,
        quantity=2,
        strategy_id="s",
        decision_timestamp_ns=8,
        send_timestamp_ns=9,
    )
    trade = event(10, 4, EventType.TRADE, Side.BID, 100, 13)
    scheduler = Scheduler()
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        source_sequence=0,
        payload=cancel,
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        source_sequence=1,
        payload=new_request,
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.MARKET,
        source_sequence=trade.sequence_number,
        payload=trade,
    )
    report_types: list[ReportType] = []

    def handle(
        scheduled: ScheduledEvent[object],
        _: Scheduler,
    ) -> None:
        payload = scheduled.payload
        if isinstance(payload, MarketEvent):
            result = exchange.handle_market_event(payload)
        else:
            assert isinstance(payload, (CancelRequest, NewOrderRequest))
            result = exchange.handle_command(
                payload,
                exchange_arrival_timestamp_ns=scheduled.timestamp_ns,
            )
        report_types.extend(report.report_type for report in result.reports)

    scheduler.run(handle)

    new_order = exchange.registry.get_by_client_id("new")
    assert old_order.status is OrderStatus.FILLED
    assert new_order.status is OrderStatus.LIVE
    assert new_order.cumulative_filled_quantity == 0
    assert report_types == [
        ReportType.FILL,
        ReportType.CANCEL_REJECTED,
        ReportType.ACCEPTED,
    ]


def test_order_in_flight_is_rejected_after_session_end(tmp_path: Any) -> None:
    config = causal_config(order_latency_ns=50)
    config = config.model_copy(
        update={
            "latency": config.latency.model_copy(
                update={"market_data_ns": 0, "fill_report_ns": 0}
            )
        }
    )
    events = [
        event(0, 1, EventType.ADD, Side.BID, 100, 50),
        event(0, 2, EventType.ADD, Side.ASK, 102, 50),
        event(10, 3, EventType.ADD, Side.BID, 99, 1),
    ]

    result = run_backtest(
        config,
        events,
        run_name="session-boundary",
        output_root=tmp_path,
        generate_plots=False,
    )
    orders = pl.read_parquet(result.run_directory / "orders.parquet")
    quotes = pl.read_parquet(result.run_directory / "quotes.parquet")

    assert quotes.filter(pl.col("action") == "submit").height == 2
    assert orders.filter(pl.col("new_status") == OrderStatus.LIVE.value).is_empty()
