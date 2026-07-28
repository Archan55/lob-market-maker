from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from lobmm.book import L2Book
from lobmm.channels import (
    ChannelDelivery,
    FixedLatency,
    OrderedChannel,
    UniformJitterLatency,
)
from lobmm.config import StrategyConfig
from lobmm.enums import (
    EventType,
    LiquidityRole,
    OrderStatus,
    ReportType,
    SchedulerPhase,
    Side,
)
from lobmm.events import MarketEvent
from lobmm.orders import ExecutionReport, Fill
from lobmm.scheduler import ScheduledEvent, Scheduler
from lobmm.strategies import make_strategy
from lobmm.strategy_runtime import StrategyRuntime


def runtime() -> StrategyRuntime:
    return StrategyRuntime(
        strategy=make_strategy(StrategyConfig()),
        max_abs_inventory=10,
    )


def test_observed_book_changes_only_when_delayed_message_arrives() -> None:
    true_book = L2Book()
    strategy_runtime = runtime()
    scheduler = Scheduler()
    channel: OrderedChannel[MarketEvent] = OrderedChannel(
        name="market",
        scheduler=scheduler,
        phase=SchedulerPhase.MARKET_DATA_DELIVERY,
        latency=FixedLatency(5),
    )
    event = MarketEvent(
        timestamp_ns=10,
        sequence_number=1,
        event_type=EventType.ADD,
        side=Side.BID,
        price_ticks=100,
        quantity=10,
    )

    true_book.apply(event)
    channel.send(event, send_timestamp_ns=event.timestamp_ns)
    assert true_book.best_bid == 100
    assert strategy_runtime.observed_book.best_bid is None
    assert scheduler.run(lambda *_: None, until_ns=14) == 0
    assert strategy_runtime.observed_book.best_bid is None

    def deliver(
        scheduled: ScheduledEvent[object],
        _: Scheduler,
    ) -> None:
        envelope = scheduled.payload
        assert isinstance(envelope, ChannelDelivery)
        assert isinstance(envelope.payload, MarketEvent)
        strategy_runtime.deliver_market(
            envelope.payload,
            notification_timestamp_ns=scheduled.timestamp_ns,
        )

    scheduler.run(deliver, until_ns=15)
    assert strategy_runtime.observed_book.best_bid == 100
    assert strategy_runtime.latest_view is not None
    assert strategy_runtime.latest_view.timestamp_ns == 15


def test_known_inventory_changes_only_on_delayed_fill_delivery() -> None:
    strategy_runtime = runtime()
    scheduler = Scheduler()
    channel: OrderedChannel[ExecutionReport] = OrderedChannel(
        name="reports",
        scheduler=scheduler,
        phase=SchedulerPhase.EXECUTION_REPORT_DELIVERY,
        latency=FixedLatency(5),
    )
    fill = Fill(
        fill_id="F1",
        order_id="O1",
        client_order_id="C1",
        strategy_id="s",
        side=Side.BID,
        quantity=3,
        price_ticks=100,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=20,
        fee=Decimal("0"),
        rebate=Decimal("0"),
    )
    report = ExecutionReport(
        report_id="R1",
        report_type=ReportType.FILL,
        exchange_timestamp_ns=20,
        order_id="O1",
        client_order_id="C1",
        order_status=OrderStatus.FILLED,
        remaining_quantity=0,
        fill=fill,
    )
    channel.send(report, send_timestamp_ns=20)

    assert scheduler.run(lambda *_: None, until_ns=24) == 0
    assert strategy_runtime.known_inventory == 0

    def deliver(
        scheduled: ScheduledEvent[object],
        _: Scheduler,
    ) -> None:
        envelope = scheduled.payload
        assert isinstance(envelope, ChannelDelivery)
        assert isinstance(envelope.payload, ExecutionReport)
        strategy_runtime.deliver_report(
            envelope.payload,
            notification_timestamp_ns=scheduled.timestamp_ns,
        )

    scheduler.run(deliver, until_ns=25)
    assert strategy_runtime.known_inventory == 3


def test_seeded_random_latency_cannot_reorder_an_ordered_channel() -> None:
    scheduler = Scheduler()
    channel: OrderedChannel[int] = OrderedChannel(
        name="random",
        scheduler=scheduler,
        phase=SchedulerPhase.MARKET_DATA_DELIVERY,
        latency=UniformJitterLatency(
            base_delay_ns=50,
            jitter_ns=50,
            seed=7,
        ),
    )
    scheduled = [
        channel.send(sequence, send_timestamp_ns=sequence) for sequence in range(100)
    ]
    raw_candidates = [
        item.payload.send_timestamp_ns + item.payload.sampled_latency_ns
        for item in scheduled
    ]
    deliveries = [item.timestamp_ns for item in scheduled]

    assert any(current > following for current, following in pairwise(raw_candidates))
    assert deliveries == sorted(deliveries)

    observed: list[int] = []

    def receive(
        item: ScheduledEvent[object],
        _: Scheduler,
    ) -> None:
        envelope = item.payload
        assert isinstance(envelope, ChannelDelivery)
        assert isinstance(envelope.payload, int)
        observed.append(envelope.payload)

    scheduler.run(receive)
    assert observed == list(range(100))
