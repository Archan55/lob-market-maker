"""Readable reference implementation of an aggregated Level 2 order book."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import BookDelta, BookView, MarketEvent


class BookError(ValueError):
    """Base class for invalid book operations."""


class InsufficientDepthError(BookError):
    """Raised when a reduction exceeds displayed depth in strict mode."""


class CrossedBookError(BookError):
    """Raised when an event would leave best bid at or above best ask."""


class L2Book:
    """Single-instrument aggregated price-level book.

    The book contains external historical depth only. Simulated own resting
    orders belong to the queue overlay, not these dictionaries.
    """

    def __init__(self, validation_mode: ValidationMode = ValidationMode.STRICT) -> None:
        self.validation_mode = validation_mode
        self._bids: dict[int, int] = {}
        self._asks: dict[int, int] = {}
        self._best_bid: int | None = None
        self._best_ask: int | None = None
        self.diagnostics: Counter[str] = Counter()
        self.events_applied = 0

    @property
    def bids(self) -> Mapping[int, int]:
        return MappingProxyType(self._bids)

    @property
    def asks(self) -> Mapping[int, int]:
        return MappingProxyType(self._asks)

    @property
    def best_bid(self) -> int | None:
        return self._best_bid

    @property
    def best_ask(self) -> int | None:
        return self._best_ask

    @property
    def spread(self) -> int | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def midpoint(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def microprice(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        bid_quantity = self._bids[bid]
        ask_quantity = self._asks[ask]
        denominator = bid_quantity + ask_quantity
        if denominator == 0:
            return None
        return (ask * bid_quantity + bid * ask_quantity) / denominator

    @property
    def top_level_imbalance(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        bid_quantity = self._bids[bid]
        ask_quantity = self._asks[ask]
        total = bid_quantity + ask_quantity
        if total == 0:
            return None
        return (bid_quantity - ask_quantity) / total

    @property
    def is_crossed(self) -> bool:
        bid = self.best_bid
        ask = self.best_ask
        return bid is not None and ask is not None and bid >= ask

    def __len__(self) -> int:
        return len(self._bids) + len(self._asks)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, L2Book):
            return NotImplemented
        return self._bids == other._bids and self._asks == other._asks

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._best_bid = None
        self._best_ask = None

    def quantity_at(self, side: Side, price_ticks: int) -> int:
        return self._levels(side).get(price_ticks, 0)

    def top_n(self, side: Side, depth: int) -> tuple[tuple[int, int], ...]:
        if depth < 0:
            raise ValueError("depth must be nonnegative")
        levels = self._levels(side).items()
        ordered = sorted(levels, reverse=side is Side.BID)
        return tuple(ordered[:depth])

    def weighted_imbalance(self, depth: int = 5) -> float | None:
        """Inverse-rank-weighted multi-level imbalance."""

        if depth <= 0:
            raise ValueError("depth must be positive")
        bid_depth = self.top_n(Side.BID, depth)
        ask_depth = self.top_n(Side.ASK, depth)
        if not bid_depth or not ask_depth:
            return None
        weighted_bid = sum(qty / rank for rank, (_, qty) in enumerate(bid_depth, 1))
        weighted_ask = sum(qty / rank for rank, (_, qty) in enumerate(ask_depth, 1))
        total = weighted_bid + weighted_ask
        return (weighted_bid - weighted_ask) / total if total else None

    def apply_snapshot(
        self,
        bids: Iterable[tuple[int, int]],
        asks: Iterable[tuple[int, int]],
    ) -> None:
        new_bids = self._validated_snapshot_side(Side.BID, bids)
        new_asks = self._validated_snapshot_side(Side.ASK, asks)
        prior_bids, prior_asks = self._bids, self._asks
        prior_best_bid, prior_best_ask = self._best_bid, self._best_ask
        self._bids, self._asks = new_bids, new_asks
        self._best_bid = max(new_bids, default=None)
        self._best_ask = min(new_asks, default=None)
        if self.is_crossed:
            self._bids, self._asks = prior_bids, prior_asks
            self._best_bid, self._best_ask = prior_best_bid, prior_best_ask
            raise CrossedBookError("snapshot would create a crossed book")

    def apply(self, event: MarketEvent) -> BookDelta:
        """Apply one canonical event and return its external-depth delta."""

        if event.event_type is EventType.RESET:
            before = sum(self._bids.values()) + sum(self._asks.values())
            self.clear()
            self.events_applied += 1
            return BookDelta(event, before, before, 0)

        assert event.side is not None
        if event.event_type in {EventType.ADD, EventType.SNAPSHOT}:
            delta = self._add(event)
        elif event.event_type in {EventType.CANCEL, EventType.TRADE}:
            delta = self._reduce(event)
        else:  # pragma: no cover - exhaustive enum guard
            raise BookError(f"unsupported event type: {event.event_type}")
        self.events_applied += 1
        return delta

    def consume(self, side: Side, price_ticks: int, quantity: int) -> int:
        """Remove up to ``quantity`` external units for simulated taker flow."""

        if quantity <= 0:
            raise ValueError("quantity must be positive")
        levels = self._levels(side)
        available = levels.get(price_ticks, 0)
        applied = min(quantity, available)
        remaining = available - applied
        if remaining:
            levels[price_ticks] = remaining
        else:
            levels.pop(price_ticks, None)
            self._refresh_best_after_removal(side, price_ticks)
        return applied

    def executable_levels(
        self, incoming_side: Side, limit_price_ticks: int
    ) -> tuple[tuple[int, int], ...]:
        """Return opposite external levels eligible for a marketable limit."""

        if incoming_side is Side.BID:
            return tuple(
                (price, quantity)
                for price, quantity in sorted(self._asks.items())
                if price <= limit_price_ticks
            )
        return tuple(
            (price, quantity)
            for price, quantity in sorted(self._bids.items(), reverse=True)
            if price >= limit_price_ticks
        )

    def view(self, timestamp_ns: int, sequence_number: int, depth: int = 5) -> BookView:
        bid = self.best_bid
        ask = self.best_ask
        return BookView(
            timestamp_ns=timestamp_ns,
            sequence_number=sequence_number,
            best_bid_ticks=bid,
            best_ask_ticks=ask,
            best_bid_quantity=self._bids.get(bid, 0) if bid is not None else 0,
            best_ask_quantity=self._asks.get(ask, 0) if ask is not None else 0,
            midpoint_ticks=self.midpoint,
            microprice_ticks=self.microprice,
            imbalance=self.top_level_imbalance,
            bids=self.top_n(Side.BID, depth),
            asks=self.top_n(Side.ASK, depth),
        )

    def assert_valid(self) -> None:
        if any(quantity <= 0 for quantity in self._bids.values()):
            raise BookError("bid quantities must be positive")
        if any(quantity <= 0 for quantity in self._asks.values()):
            raise BookError("ask quantities must be positive")
        actual_best_bid = max(self._bids, default=None)
        actual_best_ask = min(self._asks, default=None)
        if (
            actual_best_bid is not None
            and actual_best_ask is not None
            and actual_best_bid >= actual_best_ask
        ):
            raise CrossedBookError(
                f"crossed book: best_bid={actual_best_bid}, best_ask={actual_best_ask}"
            )
        if self._best_bid != actual_best_bid:
            raise BookError("cached best bid does not match bid levels")
        if self._best_ask != actual_best_ask:
            raise BookError("cached best ask does not match ask levels")

    def _levels(self, side: Side) -> dict[int, int]:
        if side is Side.BID:
            return self._bids
        if side is Side.ASK:
            return self._asks
        raise BookError(f"invalid side: {side!r}")

    def _add(self, event: MarketEvent) -> BookDelta:
        assert event.side is not None
        levels = self._levels(event.side)
        before = levels.get(event.price_ticks, 0)
        prior_best = self.best_bid if event.side is Side.BID else self.best_ask
        levels[event.price_ticks] = before + event.quantity
        self._update_best_after_add(event.side, event.price_ticks)
        if self.is_crossed:
            if before:
                levels[event.price_ticks] = before
            else:
                del levels[event.price_ticks]
            if event.side is Side.BID:
                self._best_bid = prior_best
            else:
                self._best_ask = prior_best
            message = (
                f"{event.event_type} would cross market at "
                f"{event.side.name} {event.price_ticks}"
            )
            if self.validation_mode is ValidationMode.STRICT:
                raise CrossedBookError(message)
            self.diagnostics["crossed_add_ignored"] += 1
            return BookDelta(event, before, 0, before, clamped=True)
        return BookDelta(event, before, event.quantity, before + event.quantity)

    def _reduce(self, event: MarketEvent) -> BookDelta:
        assert event.side is not None
        levels = self._levels(event.side)
        before = levels.get(event.price_ticks, 0)
        if event.quantity > before:
            if self.validation_mode is ValidationMode.STRICT:
                raise InsufficientDepthError(
                    f"{event.event_type} requests {event.quantity} at "
                    f"{event.side.name} {event.price_ticks}, displayed={before}"
                )
            applied = before
            clamped = True
            self.diagnostics[f"clamped_{event.event_type.value.lower()}"] += 1
        else:
            applied = event.quantity
            clamped = False
        after = before - applied
        if after:
            levels[event.price_ticks] = after
        else:
            levels.pop(event.price_ticks, None)
            self._refresh_best_after_removal(event.side, event.price_ticks)
        return BookDelta(event, before, applied, after, clamped)

    def _update_best_after_add(self, side: Side, price_ticks: int) -> None:
        if side is Side.BID:
            if self._best_bid is None or price_ticks > self._best_bid:
                self._best_bid = price_ticks
            return
        if self._best_ask is None or price_ticks < self._best_ask:
            self._best_ask = price_ticks

    def _refresh_best_after_removal(self, side: Side, price_ticks: int) -> None:
        if side is Side.BID and price_ticks == self._best_bid:
            self._best_bid = max(self._bids, default=None)
        elif side is Side.ASK and price_ticks == self._best_ask:
            self._best_ask = min(self._asks, default=None)

    @staticmethod
    def _validated_snapshot_side(
        side: Side, levels: Iterable[tuple[int, int]]
    ) -> dict[int, int]:
        result: dict[int, int] = {}
        for price, quantity in levels:
            if price <= 0 or quantity <= 0:
                raise BookError(
                    f"snapshot {side.name} levels require positive price and quantity"
                )
            if price in result:
                raise BookError(f"duplicate snapshot price: {side.name} {price}")
            result[price] = quantity
        return result
