"""Immutable canonical market events and book deltas."""

from __future__ import annotations

from dataclasses import dataclass

from lobmm.enums import EventType, Side


class EventValidationError(ValueError):
    """Raised when a canonical event violates its field contract."""


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One normalized Level 2 event.

    ``side`` always identifies resting liquidity. A ``TRADE`` with ``BID`` side
    is therefore an aggressive sell consuming the bid.
    """

    timestamp_ns: int
    sequence_number: int
    event_type: EventType
    side: Side | None = None
    price_ticks: int = 0
    quantity: int = 0

    def __post_init__(self) -> None:
        if self.timestamp_ns < 0:
            raise EventValidationError("timestamp_ns must be nonnegative")
        if self.sequence_number < 0:
            raise EventValidationError("sequence_number must be nonnegative")
        if not isinstance(self.event_type, EventType):
            raise EventValidationError("event_type must be an EventType")
        if self.event_type is EventType.RESET:
            if self.quantity != 0:
                raise EventValidationError("RESET quantity must be zero")
            if self.price_ticks != 0:
                raise EventValidationError("RESET price_ticks must be zero")
            return
        if not isinstance(self.side, Side):
            raise EventValidationError(f"{self.event_type} requires a valid side")
        if self.price_ticks <= 0:
            raise EventValidationError(
                f"{self.event_type} requires positive price_ticks"
            )
        if self.quantity <= 0:
            raise EventValidationError(f"{self.event_type} requires positive quantity")

    @classmethod
    def reset(cls, timestamp_ns: int, sequence_number: int) -> MarketEvent:
        return cls(timestamp_ns, sequence_number, EventType.RESET)

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "timestamp_ns": self.timestamp_ns,
            "sequence_number": self.sequence_number,
            "event_type": self.event_type.value,
            "side": int(self.side) if self.side is not None else None,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True)
class BookDelta:
    """Observable effect of applying one market event to external depth."""

    event: MarketEvent
    quantity_before: int
    quantity_applied: int
    quantity_after: int
    clamped: bool = False


@dataclass(frozen=True, slots=True)
class BookView:
    """Frozen strategy-safe top-of-book and depth snapshot."""

    timestamp_ns: int
    sequence_number: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    best_bid_quantity: int
    best_ask_quantity: int
    midpoint_ticks: float | None
    microprice_ticks: float | None
    imbalance: float | None
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
