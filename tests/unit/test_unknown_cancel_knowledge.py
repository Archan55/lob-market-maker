"""An unknown cancel response cannot terminate an in-flight entry."""

import pytest

from lobmm.config import StrategyConfig
from lobmm.enums import EventType, OrderStatus, ReportType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.orders import ExecutionReport
from lobmm.strategies import make_strategy
from lobmm.strategy_runtime import StrategyRuntime


@pytest.mark.parametrize("name", tuple(StrategyName))
def test_unknown_cancel_preserves_entry_until_actual_terminal_report(
    name: StrategyName,
) -> None:
    runtime = StrategyRuntime(
        strategy=make_strategy(StrategyConfig(name=name)), max_abs_inventory=10
    )
    for i, (side, price) in enumerate(((Side.BID, 99), (Side.ASK, 101)), 1):
        runtime.deliver_market(
            MarketEvent(0, i, EventType.ADD, side, price, 10),
            notification_timestamp_ns=0,
        )
    runtime.decide(timestamp_ns=0)
    quote = runtime.strategy.active_quotes[0]
    cid = quote.client_order_id
    runtime.deliver_report(
        ExecutionReport(
            "unknown",
            ReportType.CANCEL_REJECTED,
            1,
            "",
            cid,
            OrderStatus.REJECTED,
            0,
            reason="unknown_order",
        ),
        notification_timestamp_ns=2,
    )
    assert cid in {q.client_order_id for q in runtime.strategy.active_quotes}
    assert quote.remaining_quantity == quote.original_quantity
    runtime.deliver_report(
        ExecutionReport(
            "accepted",
            ReportType.ACCEPTED,
            3,
            "O1",
            cid,
            OrderStatus.LIVE,
            quote.original_quantity,
        ),
        notification_timestamp_ns=4,
    )
    assert quote.exchange_order_id == "O1"
    runtime.deliver_report(
        ExecutionReport(
            "cancelled",
            ReportType.CANCELLED,
            5,
            "O1",
            cid,
            OrderStatus.CANCELLED,
            quote.original_quantity,
        ),
        notification_timestamp_ns=6,
    )
    assert cid not in {q.client_order_id for q in runtime.strategy.active_quotes}
