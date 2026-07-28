from __future__ import annotations

from decimal import Decimal

import pytest

from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import (
    CancelRequest,
    InvalidOrderTransition,
    NewOrderRequest,
    OrderError,
    OrderRegistry,
    ReplaceRequest,
)


def request(client_order_id: str = "c-1") -> NewOrderRequest:
    return NewOrderRequest(
        client_order_id=client_order_id,
        side=Side.BID,
        price_ticks=100,
        quantity=10,
        strategy_id="s",
        decision_timestamp_ns=1,
        send_timestamp_ns=2,
    )


def test_partial_and_full_fill_preserve_quantity_and_notional() -> None:
    registry = OrderRegistry()
    order = registry.create(request(), exchange_arrival_timestamp_ns=3)
    registry.transition(order, OrderStatus.LIVE, timestamp_ns=3)
    assert registry.live_orders == (order,)

    first, first_transition = registry.apply_fill(
        order,
        quantity=4,
        price_ticks=99,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=4,
        rebate=Decimal("0.004"),
    )
    second, second_transition = registry.apply_fill(
        order,
        quantity=6,
        price_ticks=100,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=5,
    )

    assert first.fill_id == "F000000000001"
    assert second.fill_id == "F000000000002"
    assert first_transition.new_status is OrderStatus.PARTIALLY_FILLED
    assert first_transition.remaining_quantity == 6
    assert second_transition.new_status is OrderStatus.FILLED
    assert registry.live_orders == ()
    assert order.remaining_quantity == 0
    assert order.cumulative_filled_quantity == 10
    assert order.cumulative_fill_notional_ticks == 996
    assert order.average_fill_price_ticks == Decimal("99.6")
    order.assert_quantity_identity()


def test_terminal_order_cannot_transition_or_fill() -> None:
    registry = OrderRegistry()
    order = registry.create(request(), exchange_arrival_timestamp_ns=3)
    registry.transition(order, OrderStatus.REJECTED, timestamp_ns=3)
    assert registry.live_orders == ()

    with pytest.raises(InvalidOrderTransition):
        registry.transition(order, OrderStatus.LIVE, timestamp_ns=4)
    with pytest.raises(OrderError):
        registry.apply_fill(
            order,
            quantity=1,
            price_ticks=100,
            liquidity_role=LiquidityRole.MAKER,
            exchange_fill_timestamp_ns=4,
        )


def test_identifiers_and_reports_are_deterministic() -> None:
    first = OrderRegistry()
    second = OrderRegistry()
    first_order = first.create(request(), exchange_arrival_timestamp_ns=3)
    second_order = second.create(request(), exchange_arrival_timestamp_ns=3)
    first.transition(first_order, OrderStatus.LIVE, timestamp_ns=3)
    second.transition(second_order, OrderStatus.LIVE, timestamp_ns=3)

    first_report = first.make_report(
        first_order,
        report_type=ReportType.ACCEPTED,
        exchange_timestamp_ns=3,
    )
    second_report = second.make_report(
        second_order,
        report_type=ReportType.ACCEPTED,
        exchange_timestamp_ns=3,
    )

    assert first_order.order_id == second_order.order_id == "O000000000001"
    assert first_report.report_id == second_report.report_id == "R000000000001"


def test_cancel_identifier_contract_and_replace_normalization() -> None:
    cancel = CancelRequest(
        order_id="O1",
        decision_timestamp_ns=2,
        send_timestamp_ns=3,
    )
    replacement = request("replacement")
    assert ReplaceRequest(cancel, replacement).normalize() == (
        cancel,
        replacement,
    )

    with pytest.raises(OrderError):
        CancelRequest(
            order_id="O1",
            client_order_id="c1",
            decision_timestamp_ns=2,
            send_timestamp_ns=3,
        )
