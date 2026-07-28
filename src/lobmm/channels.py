"""Deterministic latency models and sequence-preserving message channels."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from lobmm.scheduler import EventPhase, ScheduledEvent, Scheduler


class LatencyModel(Protocol):
    """A stateful deterministic source of nonnegative latency samples."""

    def sample_ns(self) -> int:
        """Return one nonnegative delay in nanoseconds."""


@dataclass(frozen=True, slots=True)
class FixedLatency:
    """Constant one-way latency."""

    delay_ns: int

    def __post_init__(self) -> None:
        if self.delay_ns < 0:
            raise ValueError("delay_ns must be nonnegative")

    def sample_ns(self) -> int:
        return self.delay_ns


class UniformJitterLatency:
    """Fixed base latency plus seeded inclusive uniform integer jitter."""

    def __init__(
        self,
        *,
        base_delay_ns: int,
        jitter_ns: int,
        seed: int,
    ) -> None:
        if base_delay_ns < 0:
            raise ValueError("base_delay_ns must be nonnegative")
        if jitter_ns < 0:
            raise ValueError("jitter_ns must be nonnegative")
        self._base_delay_ns = base_delay_ns
        self._jitter_ns = jitter_ns
        self._rng = random.Random(seed)

    def sample_ns(self) -> int:
        if self._jitter_ns == 0:
            return self._base_delay_ns
        jitter = self._rng.randint(-self._jitter_ns, self._jitter_ns)
        return max(0, self._base_delay_ns + jitter)


@dataclass(frozen=True, slots=True)
class ChannelDelivery[MessageT]:
    """Immutable delivery envelope suitable for scheduler payloads."""

    channel_name: str
    channel_sequence: int
    send_timestamp_ns: int
    delivery_timestamp_ns: int
    sampled_latency_ns: int
    payload: MessageT


class OrderedChannel[MessageT]:
    """Schedule latency-delayed messages without source-order inversion.

    A sampled delivery time is clamped to the previous delivery timestamp.
    Equal-time messages retain channel order through ``source_sequence``.
    """

    def __init__(
        self,
        *,
        name: str,
        scheduler: Scheduler,
        phase: EventPhase,
        latency: LatencyModel,
    ) -> None:
        if not name:
            raise ValueError("channel name must not be empty")
        self._name = name
        self._scheduler = scheduler
        self._phase = phase
        self._latency = latency
        self._next_sequence = 0
        self._last_send_ns: int | None = None
        self._last_delivery_ns: int | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def last_delivery_ns(self) -> int | None:
        return self._last_delivery_ns

    def send(
        self,
        payload: MessageT,
        *,
        send_timestamp_ns: int,
    ) -> ScheduledEvent[ChannelDelivery[MessageT]]:
        if send_timestamp_ns < 0:
            raise ValueError("send_timestamp_ns must be nonnegative")
        if self._last_send_ns is not None and send_timestamp_ns < self._last_send_ns:
            raise ValueError(
                "ordered channel messages must be sent in nondecreasing time"
            )

        sampled_latency_ns = self._latency.sample_ns()
        if sampled_latency_ns < 0:
            raise ValueError("latency model returned a negative delay")
        delivery_timestamp_ns = send_timestamp_ns + sampled_latency_ns
        if self._last_delivery_ns is not None:
            delivery_timestamp_ns = max(
                delivery_timestamp_ns,
                self._last_delivery_ns,
            )

        channel_sequence = self._next_sequence
        self._next_sequence += 1
        envelope = ChannelDelivery(
            channel_name=self._name,
            channel_sequence=channel_sequence,
            send_timestamp_ns=send_timestamp_ns,
            delivery_timestamp_ns=delivery_timestamp_ns,
            sampled_latency_ns=sampled_latency_ns,
            payload=payload,
        )
        scheduled = self._scheduler.schedule(
            timestamp_ns=delivery_timestamp_ns,
            phase=self._phase,
            source_sequence=channel_sequence,
            payload=envelope,
        )
        self._last_send_ns = send_timestamp_ns
        self._last_delivery_ns = delivery_timestamp_ns
        return scheduled


def market_data_channel(
    *,
    scheduler: Scheduler,
    latency: LatencyModel,
    name: str = "market_data",
) -> OrderedChannel[object]:
    return OrderedChannel(
        name=name,
        scheduler=scheduler,
        phase=EventPhase.MARKET_DATA_DELIVERY,
        latency=latency,
    )


def command_channel(
    *,
    scheduler: Scheduler,
    latency: LatencyModel,
    name: str = "commands",
) -> OrderedChannel[object]:
    return OrderedChannel(
        name=name,
        scheduler=scheduler,
        phase=EventPhase.EXCHANGE_COMMAND,
        latency=latency,
    )


def execution_report_channel(
    *,
    scheduler: Scheduler,
    latency: LatencyModel,
    name: str = "execution_reports",
) -> OrderedChannel[object]:
    return OrderedChannel(
        name=name,
        scheduler=scheduler,
        phase=EventPhase.EXECUTION_REPORT_DELIVERY,
        latency=latency,
    )
