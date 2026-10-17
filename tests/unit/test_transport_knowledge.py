"""Report identity and missing-detail checks at the client-only boundary."""

from dataclasses import replace

import pytest

from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.transport import ClientKnowledge


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_client_knowledge_deduplicates_report_and_fill_identity(side: Side) -> None:
    client = ClientKnowledge()
    client.sent(NewOrderRequest("C", side, 99, 4, "s", 0, 0))
    fill = Fill("F", "O", "C", "s", side, 1, 99, LiquidityRole.MAKER, 20)
    partial = ExecutionReport(
        "R",
        ReportType.PARTIAL_FILL,
        20,
        "O",
        "C",
        OrderStatus.PARTIALLY_FILLED,
        3,
        fill,
    )
    client.delivered(partial)
    before = client.artifact()
    client.delivered(partial)
    client.delivered(replace(partial, report_id="R2"))
    client.delivered(replace(partial, report_id="R3", client_order_id="other"))
    client.delivered(
        ExecutionReport(
            "R4",
            ReportType.CANCEL_REJECTED,
            30,
            "",
            "C",
            OrderStatus.REJECTED,
            0,
            reason="unknown_order",
        )
    )
    assert client.artifact() == before
    client.delivered(
        ExecutionReport(
            "R5", ReportType.CANCELLED, 40, "O", "C", OrderStatus.CANCELLED, 2
        )
    )
    after = client.artifact()
    assert after["known_inventory"] == int(side)
    assert after["inventory_min"] == after["inventory_max"] == int(side) * 2
    assert after["orders"][0]["missing_fill_quantity_min"] == 1
    assert after["orders"][0]["reservation_quantity"] == 0
    assert not after["complete"]
