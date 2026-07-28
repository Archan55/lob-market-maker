from __future__ import annotations

import pytest

from lobmm.enums import EventType, OrderStatus, QueueAllocation, Side
from lobmm.events import MarketEvent
from lobmm.orders import ExchangeOrder, NewOrderRequest, OrderRegistry
from lobmm.queue_model import QueueModel


def live_order(
    registry: OrderRegistry,
    client_id: str,
    *,
    side: Side = Side.BID,
    price_ticks: int = 100,
    quantity: int = 10,
) -> ExchangeOrder:
    request = NewOrderRequest(
        client_order_id=client_id,
        side=side,
        price_ticks=price_ticks,
        quantity=quantity,
        strategy_id="s",
        decision_timestamp_ns=0,
        send_timestamp_ns=0,
    )
    order = registry.create(request, exchange_arrival_timestamp_ns=0)
    registry.transition(order, OrderStatus.LIVE, timestamp_ns=0)
    return order


def test_multiple_own_orders_share_one_trade_budget() -> None:
    registry = OrderRegistry()
    first = live_order(registry, "first")
    second = live_order(registry, "second")
    queue = QueueModel()
    queue.join(first, displayed_external_quantity=100)
    queue.add_external(Side.BID, 100, 5)
    queue.join(second, displayed_external_quantity=105)

    update = queue.trade(Side.BID, 100, 115)

    assert [(fill.order_id, fill.quantity) for fill in update.fills] == [
        (first.order_id, 10)
    ]
    assert queue.position(second.order_id).total_ahead == 0

    second_update = queue.trade(Side.BID, 100, 5)
    assert [(fill.order_id, fill.quantity) for fill in second_update.fills] == [
        (second.order_id, 5)
    ]


@pytest.mark.parametrize(
    ("allocation", "expected_ahead", "expected_behind"),
    [
        (QueueAllocation.BACK_OF_QUEUE, 90, 0),
        (QueueAllocation.FRONT_OF_QUEUE, 40, 50),
        (QueueAllocation.PRO_RATA, 60, 30),
    ],
)
def test_cancellation_allocation_is_deterministic(
    allocation: QueueAllocation,
    expected_ahead: int,
    expected_behind: int,
) -> None:
    registry = OrderRegistry()
    order = live_order(registry, "one")
    queue = QueueModel(cancellation_allocation=allocation)
    queue.join(order, displayed_external_quantity=100)
    queue.add_external(Side.BID, 100, 50)

    assert queue.cancel_external(Side.BID, 100, 60) == 60
    position = queue.position(order.order_id)
    assert position.external_ahead == expected_ahead
    assert position.external_behind == expected_behind
    assert (
        sum(
            quantity
            for kind, _, quantity in queue.snapshot_segments(Side.BID, 100)
            if kind == "external"
        )
        == 90
    )


def test_removing_earlier_own_order_reduces_later_queue_ahead() -> None:
    registry = OrderRegistry()
    first = live_order(registry, "first", quantity=7)
    second = live_order(registry, "second", quantity=5)
    queue = QueueModel()
    queue.join(first, displayed_external_quantity=10)
    queue.join(second, displayed_external_quantity=10)

    assert queue.position(second.order_id).own_ahead == 7
    assert queue.remove_order(first.order_id) == 7
    assert queue.position(second.order_id).own_ahead == 0


def test_reconciliation_reports_displaced_external_depth_without_mutation() -> None:
    registry = OrderRegistry()
    order = live_order(registry, "one")
    queue = QueueModel()
    queue.join(order, displayed_external_quantity=100)
    queue.add_external(Side.BID, 100, 20)

    update = queue.trade(Side.BID, 100, 110)
    reconciliation = queue.reconciliation(
        Side.BID,
        100,
        historical_external_quantity=10,
    )

    assert [(fill.order_id, fill.quantity) for fill in update.fills] == [
        (order.order_id, 10)
    ]
    assert reconciliation.overlay_external_quantity == 20
    assert reconciliation.external_displacement == 10


def test_price_through_fills_better_resting_levels() -> None:
    registry = OrderRegistry()
    order = live_order(registry, "one", price_ticks=100, quantity=4)
    queue = QueueModel(price_through_fills=True)
    queue.join(order, displayed_external_quantity=5)

    update = queue.apply_market_event(
        MarketEvent(
            timestamp_ns=10,
            sequence_number=1,
            event_type=EventType.TRADE,
            side=Side.BID,
            price_ticks=99,
            quantity=1,
        )
    )

    assert update.fills[0].order_id == order.order_id
    assert update.fills[0].quantity == 4
    assert update.fills[0].trigger == "price_through"
    assert not queue.contains(order.order_id)
