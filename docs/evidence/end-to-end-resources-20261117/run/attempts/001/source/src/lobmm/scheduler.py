"""Deterministic event scheduler with explicit same-timestamp causality.

The scheduler deliberately separates *exchange time* from causal ordering.
Events at one nanosecond are ordered by a causal wave and a documented phase.
When a handler schedules work at the current timestamp into a phase that has
already started, that work moves to the next wave rather than travelling
backward through the timestamp.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from lobmm.enums import SchedulerPhase

# Backward-compatible local spelling while keeping one canonical phase enum.
EventPhase = SchedulerPhase


@dataclass(order=True, frozen=True, slots=True)
class ScheduledEvent[PayloadT]:
    """One heap item with a deterministic total-order key."""

    timestamp_ns: int
    causal_wave: int
    phase: EventPhase
    source_sequence: int
    schedule_id: int
    payload: PayloadT = field(compare=False)
    parent_schedule_id: int | None = field(default=None, compare=False)

    @property
    def ordering_key(self) -> tuple[int, int, int, int, int]:
        """Return the exact key used by the scheduler heap."""

        return (
            self.timestamp_ns,
            self.causal_wave,
            int(self.phase),
            self.source_sequence,
            self.schedule_id,
        )


EventHandler = Callable[[ScheduledEvent[Any], "DeterministicScheduler"], None]


class SchedulerError(RuntimeError):
    """Base class for deterministic scheduling failures."""


class CausalityError(SchedulerError):
    """Raised when work is scheduled into the already processed past."""


class CausalWaveLimitError(SchedulerError):
    """Raised when immediate reactions create too many waves at one time."""


class DeterministicScheduler:
    """Single-threaded deterministic priority-queue scheduler.

    Parameters
    ----------
    max_causal_waves:
        Maximum allowed wave index at one timestamp. This catches strategies
        that create an infinite zero-latency acknowledge/action loop.
    """

    def __init__(self, *, max_causal_waves: int = 1_000) -> None:
        if max_causal_waves < 0:
            raise ValueError("max_causal_waves must be nonnegative")
        self._heap: list[ScheduledEvent[Any]] = []
        self._next_schedule_id = 0
        self._current: ScheduledEvent[Any] | None = None
        self._last_processed: ScheduledEvent[Any] | None = None
        self._max_causal_waves = max_causal_waves
        self._processed_count = 0

    @property
    def current(self) -> ScheduledEvent[Any] | None:
        """Currently executing event, if called from a handler."""

        return self._current

    @property
    def now_ns(self) -> int | None:
        """Current simulation time, or the last processed time when idle."""

        if self._current is not None:
            return self._current.timestamp_ns
        if self._last_processed is not None:
            return self._last_processed.timestamp_ns
        return None

    @property
    def processed_count(self) -> int:
        return self._processed_count

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def schedule[PayloadT](
        self,
        *,
        timestamp_ns: int,
        phase: EventPhase,
        payload: PayloadT,
        source_sequence: int = 0,
    ) -> ScheduledEvent[PayloadT]:
        """Schedule an event and assign its deterministic causal wave.

        A handler may schedule later-phase work into its current wave. Equal-
        or earlier-phase work at the same timestamp is moved to the next wave.
        Scheduling before the current simulation clock is always rejected.
        """

        if timestamp_ns < 0:
            raise ValueError("timestamp_ns must be nonnegative")
        if source_sequence < 0:
            raise ValueError("source_sequence must be nonnegative")

        parent_id: int | None = None
        causal_wave = 0
        if self._current is not None:
            current = self._current
            parent_id = current.schedule_id
            if timestamp_ns < current.timestamp_ns:
                raise CausalityError(
                    f"cannot schedule at {timestamp_ns}; current time is "
                    f"{current.timestamp_ns}"
                )
            if timestamp_ns == current.timestamp_ns:
                causal_wave = current.causal_wave
                if int(phase) <= int(current.phase):
                    causal_wave += 1
        elif self._last_processed is not None:
            if timestamp_ns < self._last_processed.timestamp_ns:
                raise CausalityError(
                    f"cannot schedule at {timestamp_ns}; last processed time is "
                    f"{self._last_processed.timestamp_ns}"
                )
            if timestamp_ns == self._last_processed.timestamp_ns:
                # Once a run step has returned to the caller, all newly
                # injected same-time work is causally later.
                causal_wave = self._last_processed.causal_wave + 1

        if causal_wave > self._max_causal_waves:
            raise CausalWaveLimitError(
                f"causal wave {causal_wave} exceeds configured maximum "
                f"{self._max_causal_waves} at timestamp {timestamp_ns}"
            )

        schedule_id = self._next_schedule_id
        self._next_schedule_id += 1
        item = ScheduledEvent(
            timestamp_ns=timestamp_ns,
            causal_wave=causal_wave,
            phase=phase,
            source_sequence=source_sequence,
            schedule_id=schedule_id,
            payload=payload,
            parent_schedule_id=parent_id,
        )
        heapq.heappush(self._heap, item)
        return item

    def schedule_after[PayloadT](
        self,
        *,
        delay_ns: int,
        phase: EventPhase,
        payload: PayloadT,
        source_sequence: int = 0,
    ) -> ScheduledEvent[PayloadT]:
        """Schedule relative to the current simulation time."""

        if delay_ns < 0:
            raise ValueError("delay_ns must be nonnegative")
        if self.now_ns is None:
            raise SchedulerError("schedule_after requires an initialized clock")
        return self.schedule(
            timestamp_ns=self.now_ns + delay_ns,
            phase=phase,
            payload=payload,
            source_sequence=source_sequence,
        )

    def peek(self) -> ScheduledEvent[Any] | None:
        return self._heap[0] if self._heap else None

    def pop(self) -> ScheduledEvent[Any]:
        """Pop the next event without invoking a handler."""

        if not self._heap:
            raise IndexError("scheduler is empty")
        return heapq.heappop(self._heap)

    def run_one(self, handler: EventHandler) -> ScheduledEvent[Any] | None:
        """Process one event, returning ``None`` when the queue is empty."""

        if not self._heap:
            return None
        item = heapq.heappop(self._heap)
        if item.causal_wave > self._max_causal_waves:
            raise CausalWaveLimitError(
                f"causal wave {item.causal_wave} exceeds configured maximum"
            )
        self._current = item
        try:
            handler(item, self)
        finally:
            self._current = None
            self._last_processed = item
            self._processed_count += 1
        return item

    def run(
        self,
        handler: EventHandler,
        *,
        until_ns: int | None = None,
        max_events: int | None = None,
    ) -> int:
        """Process queued events and return the number handled in this call."""

        if until_ns is not None and until_ns < 0:
            raise ValueError("until_ns must be nonnegative")
        if max_events is not None and max_events < 0:
            raise ValueError("max_events must be nonnegative")

        handled = 0
        while self._heap:
            if max_events is not None and handled >= max_events:
                break
            if until_ns is not None and self._heap[0].timestamp_ns > until_ns:
                break
            self.run_one(handler)
            handled += 1
        return handled

    def pending(self) -> Iterator[ScheduledEvent[Any]]:
        """Iterate over a sorted snapshot of pending work."""

        return iter(sorted(self._heap))


# Short public alias used throughout the package.
Scheduler = DeterministicScheduler
