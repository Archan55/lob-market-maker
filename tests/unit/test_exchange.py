from __future__ import annotations

from decimal import Decimal

from lobmm.config import ExchangeConfig, FeeConfig
from lobmm.enums import (
    EventType,
    LiquidityRole,
    OrderStatus,
    ReportType,
    SchedulerPhase,
    Side,
)
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.scheduler import Scheduler


def market(
    sequence: int,
    event_type: EventType,
    side: Side,
    price: int,
    quantity: int,
    *,
    timestamp: int | None = None,
) -> MarketEvent:
    return MarketEvent(
        timestamp_ns=sequence if timestamp is None else timestamp,
        sequence_number=sequence,
        event_type=event_type,
        side=side,
        price_ticks=price,
        quantity=quantity,
    )


def new_order(
    client_id: str,
    side: Side,
    price: int,
    quantity: int,
    *,
    send: int = 5,
    post_only: bool = False,
    rest_unfilled: bool = True,
) -> NewOrderRequest:
    return NewOrderRequest(
        client_order_id=client_id,
        side=side,
        price_ticks=price,
        quantity=quantity,
        strategy_id="s",
        decision_timestamp_ns=send - 1,
        send_timestamp_ns=send,
        post_only=post_only,
        rest_unfilled=rest_unfilled,
    )


def seed_two_sided_book(exchange: Exchange) -> None:
    exchange.handle_market_event(market(1, EventType.ADD, Side.BID, 100, 10))
    exchange.handle_market_event(market(2, EventType.ADD, Side.ASK, 102, 10))


def test_passive_queue_fill_and_reconciliation() -> None:
    exchange = Exchange()
    seed_two_sided_book(exchange)
    accepted = exchange.handle_command(
        new_order("bid", Side.BID, 100, 3),
        exchange_arrival_timestamp_ns=6,
    )
    order = exchange.registry.orders[0]
    assert accepted.reports[0].report_type is ReportType.ACCEPTED
    assert order.queue_position.external_ahead == 10

    exchange.handle_market_event(market(7, EventType.ADD, Side.BID, 100, 5))
    filled = exchange.handle_market_event(market(8, EventType.TRADE, Side.BID, 100, 13))

    assert order.status is OrderStatus.FILLED
    assert len(filled.fills) == 1
    assert filled.fills[0].liquidity_role is LiquidityRole.MAKER
    assert filled.fills[0].quantity == 3
    assert filled.fills[0].rebate == Decimal("0.0006")
    reconciliation = exchange.queue_model.reconciliation(
        Side.BID,
        100,
        historical_external_quantity=exchange.book.quantity_at(Side.BID, 100),
    )
    assert reconciliation.external_displacement == 3


def test_market_event_beats_cancel_at_identical_timestamp() -> None:
    exchange = Exchange()
    seed_two_sided_book(exchange)
    exchange.handle_command(
        new_order("bid", Side.BID, 100, 3),
        exchange_arrival_timestamp_ns=6,
    )
    exchange.handle_market_event(
        market(3, EventType.ADD, Side.BID, 100, 5, timestamp=7)
    )
    order = exchange.registry.orders[0]
    cancel = CancelRequest(
        order_id=order.order_id,
        decision_timestamp_ns=10,
        send_timestamp_ns=11,
    )
    trade = market(
        4,
        EventType.TRADE,
        Side.BID,
        100,
        13,
        timestamp=12,
    )
    scheduler = Scheduler()
    scheduler.schedule(
        timestamp_ns=12,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        payload=cancel,
    )
    scheduler.schedule(
        timestamp_ns=12,
        phase=SchedulerPhase.MARKET,
        source_sequence=trade.sequence_number,
        payload=trade,
    )
    reports: list[ReportType] = []

    def handle(event: object, _: Scheduler) -> None:
        payload = event.payload  # type: ignore[attr-defined]
        if isinstance(payload, MarketEvent):
            result = exchange.handle_market_event(payload)
        else:
            result = exchange.handle_command(
                payload,
                exchange_arrival_timestamp_ns=12,
            )
        reports.extend(report.report_type for report in result.reports)

    scheduler.run(handle)

    assert order.status is OrderStatus.FILLED
    assert reports == [ReportType.FILL, ReportType.CANCEL_REJECTED]


def test_same_time_new_order_cannot_receive_prior_historical_trade() -> None:
    exchange = Exchange()
    seed_two_sided_book(exchange)
    scheduler = Scheduler()
    trade = market(
        3,
        EventType.TRADE,
        Side.BID,
        100,
        5,
        timestamp=10,
    )
    request = new_order("late", Side.BID, 100, 3, send=9)
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        payload=request,
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.MARKET,
        source_sequence=trade.sequence_number,
        payload=trade,
    )

    def handle(event: object, _: Scheduler) -> None:
        payload = event.payload  # type: ignore[attr-defined]
        if isinstance(payload, MarketEvent):
            exchange.handle_market_event(payload)
        else:
            exchange.handle_command(payload, exchange_arrival_timestamp_ns=10)

    scheduler.run(handle)

    order = exchange.registry.orders[0]
    assert order.status is OrderStatus.LIVE
    assert order.cumulative_filled_quantity == 0
    assert order.exchange_arrival_timestamp_ns == 10


def test_marketable_limit_sweeps_multiple_levels_without_inventing_depth() -> None:
    exchange = Exchange(
        fee_config=FeeConfig(
            maker_fee_per_unit=Decimal("0"),
            maker_rebate_per_unit=Decimal("0"),
            taker_fee_per_unit=Decimal("0.001"),
            proportional_fee_rate=Decimal("0"),
        )
    )
    exchange.handle_market_event(market(1, EventType.ADD, Side.BID, 99, 10))
    exchange.handle_market_event(market(2, EventType.ADD, Side.ASK, 101, 5))
    exchange.handle_market_event(market(3, EventType.ADD, Side.ASK, 102, 7))

    result = exchange.handle_command(
        new_order("take", Side.BID, 102, 10),
        exchange_arrival_timestamp_ns=6,
    )
    order = exchange.registry.orders[0]

    assert order.status is OrderStatus.FILLED
    assert [(fill.price_ticks, fill.quantity) for fill in result.fills] == [
        (101, 5),
        (102, 5),
    ]
    assert all(fill.liquidity_role is LiquidityRole.TAKER for fill in result.fills)
    assert sum(fill.fee for fill in result.fills) == Decimal("0.010")
    assert exchange.book.quantity_at(Side.ASK, 101) == 0
    assert exchange.book.quantity_at(Side.ASK, 102) == 2
    assert result.reports[0].report_type is ReportType.ACCEPTED
    assert result.reports[-1].report_type is ReportType.FILL


def test_post_only_cross_is_rejected_without_consuming_depth() -> None:
    exchange = Exchange()
    seed_two_sided_book(exchange)

    result = exchange.handle_command(
        new_order("post", Side.BID, 102, 2, post_only=True),
        exchange_arrival_timestamp_ns=6,
    )

    assert exchange.registry.orders[0].status is OrderStatus.REJECTED
    assert result.command_rejection_reason == "post_only_would_cross"
    assert exchange.book.quantity_at(Side.ASK, 102) == 10


def test_unfilled_marketable_remainder_can_expire_instead_of_resting() -> None:
    exchange = Exchange(
        exchange_config=ExchangeConfig(
            rest_unfilled_marketable_quantity=False,
        )
    )
    seed_two_sided_book(exchange)

    result = exchange.handle_command(
        new_order(
            "ioc-like",
            Side.BID,
            102,
            15,
            rest_unfilled=False,
        ),
        exchange_arrival_timestamp_ns=6,
    )

    order = exchange.registry.orders[0]
    assert order.cumulative_filled_quantity == 10
    assert order.remaining_quantity == 5
    assert order.status is OrderStatus.EXPIRED
    assert result.reports[-1].report_type is ReportType.EXPIRED
