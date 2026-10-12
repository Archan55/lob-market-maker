"""A client-only cross-channel identity boundary, with no venue dependency."""

from dataclasses import replace

import pytest

from lobmm.config import AppConfig
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.strategies import make_strategy
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import ClientKnowledge


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
@pytest.mark.parametrize("original_first", (False, True))
def test_fill_identity_shared_even_across_different_report_ids(
    side: Side, original_first: bool
) -> None:
    runtime = StrategyRuntime(
        strategy=make_strategy(AppConfig().strategy), max_abs_inventory=4
    )
    knowledge = ClientKnowledge()
    knowledge.sent(NewOrderRequest("C", side, 99, 4, "s", 0, 0))
    fill = Fill("F", "O", "C", "s", side, 2, 99, LiquidityRole.MAKER, 10)
    report = ExecutionReport(
        "R",
        ReportType.PARTIAL_FILL,
        10,
        "O",
        "C",
        OrderStatus.PARTIALLY_FILLED,
        2,
        fill,
    )
    if original_first:
        runtime.deliver_report(report, notification_timestamp_ns=20)
        knowledge.delivered(report)
    runtime.deliver_recovered_fill(fill, notification_timestamp_ns=30)
    knowledge.recovered_fill(fill)
    runtime.deliver_report(report, notification_timestamp_ns=40)
    knowledge.delivered(report)
    duplicate = replace(report, report_id="R2")
    runtime.deliver_report(duplicate, notification_timestamp_ns=50)
    knowledge.delivered(duplicate)
    assert (
        runtime.known_inventory
        == knowledge.artifact()["known_inventory"]
        == int(side) * 2
    )
    assert list(knowledge.fill_details) == ["F"]


def test_recovered_fill_cannot_arrive_before_execution() -> None:
    runtime = StrategyRuntime(
        strategy=make_strategy(AppConfig().strategy), max_abs_inventory=4
    )
    fill = Fill("F", "O", "C", "s", Side.BID, 1, 99, LiquidityRole.MAKER, 10)
    with pytest.raises(ValueError, match="cannot precede execution"):
        runtime.deliver_recovered_fill(fill, notification_timestamp_ns=9)
    assert runtime.known_inventory == 0
