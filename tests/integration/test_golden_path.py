from __future__ import annotations

from decimal import Decimal

from lobmm.config import (
    ExchangeConfig,
    FeeConfig,
    InstrumentConfig,
    QueueModelConfig,
)
from lobmm.enums import EventType, OrderStatus, QueueAllocation, Side
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import NewOrderRequest
from lobmm.portfolio import Portfolio


def event(
    timestamp: int,
    sequence: int,
    kind: EventType,
    side: Side,
    price: int,
    quantity: int,
) -> MarketEvent:
    return MarketEvent(timestamp, sequence, kind, side, price, quantity)


def test_manually_auditable_queue_fill_accounting_golden_path() -> None:
    instrument = InstrumentConfig(tick_size=Decimal("0.01"))
    fees = FeeConfig(maker_rebate_per_unit=Decimal("0.001"))
    exchange = Exchange(
        instrument_config=instrument,
        fee_config=fees,
        exchange_config=ExchangeConfig(),
        queue_config=QueueModelConfig(
            cancellation_allocation=QueueAllocation.BACK_OF_QUEUE
        ),
    )
    exchange.handle_market_event(event(1, 1, EventType.ADD, Side.BID, 100, 10))
    exchange.handle_market_event(event(1, 2, EventType.ADD, Side.ASK, 102, 10))
    accepted = exchange.handle_command(
        NewOrderRequest(
            client_order_id="golden",
            side=Side.BID,
            price_ticks=100,
            quantity=5,
            strategy_id="golden",
            decision_timestamp_ns=2,
            send_timestamp_ns=2,
            post_only=True,
        ),
        exchange_arrival_timestamp_ns=2,
    )
    order = exchange.registry.orders[0]
    assert accepted.fills == ()
    assert order.queue_position.total_ahead == 10

    exchange.handle_market_event(event(3, 3, EventType.CANCEL, Side.BID, 100, 5))
    assert order.queue_position.total_ahead == 5
    exchange.handle_market_event(event(4, 4, EventType.ADD, Side.BID, 100, 10))
    assert order.queue_position.external_behind == 10

    partial = exchange.handle_market_event(
        event(5, 5, EventType.TRADE, Side.BID, 100, 8)
    )
    assert partial.fills[0].quantity == 3
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.remaining_quantity == 2

    complete = exchange.handle_market_event(
        event(6, 6, EventType.TRADE, Side.BID, 100, 2)
    )
    assert complete.fills[0].quantity == 2
    assert order.status is OrderStatus.FILLED
    assert order.remaining_quantity == 0
    assert order.cumulative_filled_quantity == 5

    portfolio = Portfolio(instrument, fees)
    for fill in (*partial.fills, *complete.fills):
        portfolio.apply_fill(fill, mark_ticks=101)
    final = portfolio.snapshot(101, timestamp_ns=6)
    assert final.inventory == 5
    assert final.trade_cash_ticks == -500
    assert final.gross_pnl_ticks == Decimal("5")
    assert final.gross_pnl == Decimal("0.05")
    assert final.rebates == Decimal("0.005")
    assert final.net_pnl == Decimal("0.055")
    portfolio.assert_invariants(101)
