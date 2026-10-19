"""Evidence delay bounds, composition, nominal ordering and checkpoint replay."""

import copy
from dataclasses import replace
from typing import Any

import pytest

from lobmm.channels import FixedLatency, OrderedChannel
from lobmm.config import AppConfig
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, SchedulerPhase, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.scheduler import Scheduler
from lobmm.strategies import make_strategy
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import (
    ClientKnowledge,
    ResearchTransport,
    TransportDelay,
    TransportFault,
    TransportFaultPlan,
)


@pytest.mark.parametrize(
    "channel,sequence,delay",
    [
        ("new_orders", 0, 1),
        ("market_data", 0, 1),
        ("execution_reports", -1, 1),
        ("execution_reports", True, 1),
        ("execution_reports", 0, 0),
        ("execution_reports", 0, -1),
        ("execution_reports", 0, True),
        ("execution_reports", 0, 1.5),
        ("execution_reports", 0, 10**12 + 1),
    ],
)
def test_invalid_delay(channel: Any, sequence: Any, delay: Any) -> None:
    with pytest.raises(ValueError):
        TransportDelay(channel, sequence, delay)


@pytest.mark.parametrize("invalid", ("list", "duplicate", "too-many"))
def test_bounded_unique_rules(invalid: str) -> None:
    rule = TransportDelay("execution_reports", 0, 1)
    value: Any = (
        [rule]
        if invalid == "list"
        else (
            (rule, rule)
            if invalid == "duplicate"
            else tuple(replace(rule, sequence=i) for i in range(17))
        )
    )
    with pytest.raises(ValueError):
        TransportFaultPlan(delays=value)


@pytest.mark.parametrize("action,copies", (("drop", 0), ("duplicate", 2)))
def test_delay_composes_with_existing_identity_faults(action: Any, copies: int) -> None:
    scheduler = Scheduler()
    transport = ResearchTransport(
        TransportFaultPlan(
            (TransportFault("execution_reports", 0, action),),
            (
                TransportDelay("execution_reports", 0, 10**12),
                TransportDelay("execution_reports", 9, 1),
            ),
        )
    )
    channel: OrderedChannel[str] = OrderedChannel(
        name="execution_reports",
        scheduler=scheduler,
        phase=SchedulerPhase.EXECUTION_REPORT_DELIVERY,
        latency=FixedLatency(10),
        transport=transport,
    )
    first = channel.send("first", send_timestamp_ns=0)
    channel.send("second", send_timestamp_ns=1)
    assert channel.last_delivery_ns == 11
    pending = list(scheduler.pending())
    assert [p.payload.payload for p in pending] == ["second"] + ["first"] * copies
    assert (first is None) == (copies == 0)
    assert [p.timestamp_ns for p in pending] == [11] + [10**12 + 10] * copies
    assert transport.artifact()["unused_delays"] == [
        dict(channel="execution_reports", sequence=9, delay_ns=1)
    ]
    seen = []
    scheduler.run(
        lambda item, _: seen.append(
            transport.receive(
                item.payload.channel_name,
                item.payload.channel_sequence,
                item.timestamp_ns,
            )
        )
    )
    assert seen == [True] + ([True, False] if copies else [])


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
@pytest.mark.parametrize("checkpoint_after_query", (False, True))
def test_memory_checkpoint_suffix_replay(
    side: Side, checkpoint_after_query: bool
) -> None:
    # A deepcopy of live Python client state is a volatile checkpoint. No file,
    # process restart, serialization, durable inbox or crash consistency claim.
    knowledge = ClientKnowledge()
    request = NewOrderRequest("C", side, 99, 5, "s", 0, 0)
    knowledge.sent(request)
    runtime = StrategyRuntime(
        strategy=make_strategy(AppConfig().strategy), max_abs_inventory=5
    )
    fills = [
        Fill(f"F{i}", "O", "C", "s", side, q, 99, LiquidityRole.MAKER, t)
        for i, q, t in ((1, 2, 10), (2, 1, 20))
    ]
    reports = [
        ExecutionReport(
            f"R{i}",
            ReportType.PARTIAL_FILL,
            t,
            "O",
            "C",
            OrderStatus.PARTIALLY_FILLED,
            rem,
            fill,
        )
        for i, t, rem, fill in ((1, 10, 3, fills[0]), (2, 20, 2, fills[1]))
    ]

    def query(k: ClientKnowledge, r: StrategyRuntime) -> None:
        for fill in fills:
            if k.recovered_fill(fill):
                r.deliver_recovered_fill(fill, notification_timestamp_ns=30)
        k.reconciled_state("C", 2, terminal=True)

    if checkpoint_after_query:
        query(knowledge, runtime)
    else:
        knowledge.delivered(reports[1])
        runtime.deliver_report(reports[1], notification_timestamp_ns=25)
    cloned_knowledge, cloned_runtime = copy.deepcopy((knowledge, runtime))
    for k, r in ((knowledge, runtime), (cloned_knowledge, cloned_runtime)):
        query(k, r)
        for report in (
            reports[0],
            reports[1],
            replace(reports[0], report_id="different-report"),
        ):
            k.delivered(report)
            r.deliver_report(report, notification_timestamp_ns=40)
        # The older live snapshot does not reopen the terminal reservation.
        k.reconciled_state("C", 3, terminal=False)
    assert cloned_knowledge.artifact() == knowledge.artifact()
    assert cloned_knowledge.fill_details == knowledge.fill_details
    assert cloned_runtime.known_inventory == runtime.known_inventory == int(side) * 3
    assert knowledge.artifact()["complete"]
    assert knowledge.artifact()["orders"][0]["reservation_quantity"] == 0
