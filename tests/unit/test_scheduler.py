from __future__ import annotations

from dataclasses import dataclass

import pytest

from lobmm.channels import FixedLatency, OrderedChannel
from lobmm.enums import SchedulerPhase
from lobmm.scheduler import CausalityError, Scheduler


def test_total_order_uses_phase_source_sequence_and_schedule_id() -> None:
    scheduler = Scheduler()
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.EXCHANGE_COMMAND,
        source_sequence=0,
        payload="command",
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.MARKET,
        source_sequence=2,
        payload="market-2",
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.MARKET,
        source_sequence=1,
        payload="market-1",
    )
    scheduler.schedule(
        timestamp_ns=10,
        phase=SchedulerPhase.MARKET,
        source_sequence=1,
        payload="market-1-later-enqueue",
    )

    observed: list[str] = []
    scheduler.run(lambda event, _: observed.append(event.payload))

    assert observed == [
        "market-1",
        "market-1-later-enqueue",
        "market-2",
        "command",
    ]


def test_zero_latency_reaction_moves_backward_phase_to_next_wave() -> None:
    scheduler = Scheduler()
    observed: list[tuple[str, int, SchedulerPhase]] = []
    scheduler.schedule(
        timestamp_ns=100,
        phase=SchedulerPhase.MARKET,
        payload="market",
    )

    def handle(event: object, active: Scheduler) -> None:
        scheduled = event
        observed.append(
            (scheduled.payload, scheduled.causal_wave, scheduled.phase)  # type: ignore[attr-defined]
        )
        if scheduled.payload == "market":  # type: ignore[attr-defined]
            active.schedule(
                timestamp_ns=100,
                phase=SchedulerPhase.MARKET_DATA_DELIVERY,
                payload="md",
            )
        elif scheduled.payload == "md":  # type: ignore[attr-defined]
            active.schedule(
                timestamp_ns=100,
                phase=SchedulerPhase.STRATEGY_DECISION,
                payload="decision",
            )
        elif scheduled.payload == "decision":  # type: ignore[attr-defined]
            active.schedule(
                timestamp_ns=100,
                phase=SchedulerPhase.EXCHANGE_COMMAND,
                payload="reactive-order",
            )

    scheduler.run(handle)

    assert [(payload, wave) for payload, wave, _ in observed] == [
        ("market", 0),
        ("md", 0),
        ("decision", 0),
        ("reactive-order", 1),
    ]


def test_scheduler_rejects_work_in_processed_past() -> None:
    scheduler = Scheduler()
    scheduler.schedule(
        timestamp_ns=5,
        phase=SchedulerPhase.MARKET,
        payload=None,
    )

    def handle(_: object, active: Scheduler) -> None:
        with pytest.raises(CausalityError):
            active.schedule(
                timestamp_ns=4,
                phase=SchedulerPhase.AUDIT,
                payload=None,
            )

    scheduler.run(handle)


@dataclass
class SequenceLatency:
    samples: list[int]

    def sample_ns(self) -> int:
        return self.samples.pop(0)


def test_ordered_channel_clamps_delivery_without_reordering() -> None:
    scheduler = Scheduler()
    channel: OrderedChannel[str] = OrderedChannel(
        name="test",
        scheduler=scheduler,
        phase=SchedulerPhase.MARKET_DATA_DELIVERY,
        latency=SequenceLatency([10, 0]),
    )
    first = channel.send("first", send_timestamp_ns=0)
    second = channel.send("second", send_timestamp_ns=1)

    assert first.timestamp_ns == 10
    assert second.timestamp_ns == 10
    assert first.source_sequence < second.source_sequence
    assert [event.payload.payload for event in scheduler.pending()] == [
        "first",
        "second",
    ]


def test_fixed_latency_validates_and_samples_exactly() -> None:
    assert FixedLatency(7).sample_ns() == 7
    with pytest.raises(ValueError):
        FixedLatency(-1)
