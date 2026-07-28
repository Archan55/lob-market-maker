"""Shared FIFO Level 2 queue approximation for simulated resting orders.

The historical :class:`~lobmm.book.L2Book` remains an exogenous external book.
This module owns a counterfactual queue overlay only at prices where we have
resting orders. An exact-price trade walks a price queue once with one volume
budget, so multiple own orders cannot each reuse the same historical trade.

Once an own order fills, overlay external depth can differ from the historical
book: our inserted order displaced external volume that the original trade
would otherwise have consumed. ``reconciliation()`` reports this displacement
but intentionally does not erase it. Reset/snapshot policy is the explicit
re-anchoring boundary.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from lobmm.enums import EventType, QueueAllocation, Side
from lobmm.events import BookDelta, MarketEvent
from lobmm.orders import ExchangeOrder, QueuePosition


class QueueModelError(ValueError):
    """Raised for impossible shared-queue operations."""


@dataclass(slots=True)
class ExternalSegment:
    segment_id: int
    quantity: int


@dataclass(slots=True)
class OwnSegment:
    segment_id: int
    order_id: str
    quantity: int


type QueueSegment = ExternalSegment | OwnSegment
type QueueKey = tuple[Side, int]


@dataclass(slots=True)
class PriceQueue:
    side: Side
    price_ticks: int
    segments: list[QueueSegment] = field(default_factory=list)

    @property
    def external_quantity(self) -> int:
        return sum(
            segment.quantity
            for segment in self.segments
            if isinstance(segment, ExternalSegment)
        )

    @property
    def own_quantity(self) -> int:
        return sum(
            segment.quantity
            for segment in self.segments
            if isinstance(segment, OwnSegment)
        )


@dataclass(frozen=True, slots=True)
class QueueFillIntent:
    order_id: str
    side: Side
    quantity: int
    price_ticks: int
    trigger: str


@dataclass(frozen=True, slots=True)
class QueueUpdate:
    fills: tuple[QueueFillIntent, ...] = ()
    orders_lost_on_reset: tuple[str, ...] = ()
    external_quantity_removed: int = 0
    unmatched_trade_quantity: int = 0


@dataclass(frozen=True, slots=True)
class QueueReconciliation:
    """Comparison without mutation between overlay and historical depth."""

    side: Side
    price_ticks: int
    historical_external_quantity: int
    overlay_external_quantity: int
    own_quantity: int

    @property
    def external_displacement(self) -> int:
        return self.overlay_external_quantity - self.historical_external_quantity


class SharedQueueLedger:
    """Counterfactual external/own FIFO segments at active own-order prices."""

    def __init__(
        self,
        *,
        cancellation_allocation: QueueAllocation = QueueAllocation.BACK_OF_QUEUE,
        price_through_fills: bool = True,
    ) -> None:
        self.cancellation_allocation = cancellation_allocation
        self.price_through_fills = price_through_fills
        self._levels: dict[QueueKey, PriceQueue] = {}
        self._order_locations: dict[str, QueueKey] = {}
        self._next_segment_id = 1
        self.diagnostics: Counter[str] = Counter()

    @property
    def active_levels(self) -> tuple[QueueKey, ...]:
        return tuple(sorted(self._levels, key=lambda key: (int(key[0]), key[1])))

    def contains(self, order_id: str) -> bool:
        return order_id in self._order_locations

    def join(
        self,
        order: ExchangeOrder,
        *,
        displayed_external_quantity: int,
    ) -> QueuePosition:
        """Append an accepted resting order exactly once.

        The first own order initializes the level from current historical
        displayed depth. Later own orders join the existing counterfactual
        overlay; the displayed quantity is deliberately not inserted again.
        """

        if not order.is_fillable:
            raise QueueModelError("only live or partially filled orders may rest")
        if order.remaining_quantity <= 0:
            raise QueueModelError("resting order must have positive remaining quantity")
        if displayed_external_quantity < 0:
            raise QueueModelError("displayed external quantity cannot be negative")
        if order.order_id in self._order_locations:
            raise QueueModelError(f"order already present in queue: {order.order_id}")

        key = (order.side, order.price_ticks)
        level = self._levels.get(key)
        if level is None:
            level = PriceQueue(side=order.side, price_ticks=order.price_ticks)
            self._levels[key] = level
            if displayed_external_quantity:
                level.segments.append(
                    ExternalSegment(
                        segment_id=self._take_segment_id(),
                        quantity=displayed_external_quantity,
                    )
                )
        elif displayed_external_quantity != level.external_quantity:
            # Expected after an own fill displaces historical external volume.
            # Record but do not mutate: adding the displayed amount again would
            # double count the level for later own orders.
            self.diagnostics["join_depth_reconciliation_difference"] += 1

        level.segments.append(
            OwnSegment(
                segment_id=self._take_segment_id(),
                order_id=order.order_id,
                quantity=order.remaining_quantity,
            )
        )
        self._order_locations[order.order_id] = key
        self._assert_level(level)
        return self.position(order.order_id)

    def position(self, order_id: str) -> QueuePosition:
        key = self._order_locations.get(order_id)
        if key is None:
            raise QueueModelError(f"order is not resting: {order_id}")
        level = self._levels[key]
        external_ahead = 0
        own_ahead = 0
        external_behind = 0
        found = False
        for segment in level.segments:
            if isinstance(segment, OwnSegment) and segment.order_id == order_id:
                found = True
                continue
            if isinstance(segment, ExternalSegment):
                if found:
                    external_behind += segment.quantity
                else:
                    external_ahead += segment.quantity
            elif not found:
                own_ahead += segment.quantity
        if not found:  # pragma: no cover - internal consistency guard
            raise QueueModelError(f"queue location is stale: {order_id}")
        return QueuePosition(
            external_ahead=external_ahead,
            own_ahead=own_ahead,
            external_behind=external_behind,
        )

    def positions_at(
        self,
        side: Side,
        price_ticks: int,
    ) -> dict[str, QueuePosition]:
        level = self._levels.get((side, price_ticks))
        if level is None:
            return {}
        return {
            segment.order_id: self.position(segment.order_id)
            for segment in level.segments
            if isinstance(segment, OwnSegment)
        }

    def remove_order(self, order_id: str) -> int:
        """Remove an own node when its cancel/expiry reaches the exchange."""

        key = self._order_locations.pop(order_id, None)
        if key is None:
            return 0
        level = self._levels[key]
        removed = 0
        retained: list[QueueSegment] = []
        for segment in level.segments:
            if isinstance(segment, OwnSegment) and segment.order_id == order_id:
                removed += segment.quantity
            else:
                retained.append(segment)
        level.segments = retained
        self._discard_if_no_own(key)
        return removed

    def add_external(self, side: Side, price_ticks: int, quantity: int) -> None:
        """Append a later displayed add behind all existing queue nodes."""

        if quantity <= 0:
            raise QueueModelError("external add quantity must be positive")
        level = self._levels.get((side, price_ticks))
        if level is None:
            return
        level.segments.append(ExternalSegment(self._take_segment_id(), quantity))
        self._assert_level(level)

    def cancel_external(
        self,
        side: Side,
        price_ticks: int,
        quantity: int,
    ) -> int:
        """Remove only external segments under the configured assumption."""

        if quantity <= 0:
            raise QueueModelError("external cancel quantity must be positive")
        key = (side, price_ticks)
        level = self._levels.get(key)
        if level is None:
            return 0
        target = min(quantity, level.external_quantity)
        if target < quantity:
            self.diagnostics["clamped_external_cancel"] += 1
        if target == 0:
            return 0

        if self.cancellation_allocation is QueueAllocation.BACK_OF_QUEUE:
            self._remove_external_directionally(level, target, reverse=True)
        elif self.cancellation_allocation is QueueAllocation.FRONT_OF_QUEUE:
            self._remove_external_directionally(level, target, reverse=False)
        elif self.cancellation_allocation is QueueAllocation.PRO_RATA:
            self._remove_external_pro_rata(level, target)
        else:  # pragma: no cover - exhaustive enum guard
            raise QueueModelError(
                f"unknown cancellation allocation: {self.cancellation_allocation}"
            )
        self._drop_empty_segments(level)
        self._assert_level(level)
        return target

    def consume_external(
        self,
        side: Side,
        price_ticks: int,
        quantity: int,
    ) -> int:
        """Remove external liquidity for our own taker execution.

        Own nodes are skipped, preventing accidental self-trading. The caller
        should pass exactly the quantity actually removed from ``L2Book``.
        """

        if quantity <= 0:
            raise QueueModelError("external consumption quantity must be positive")
        level = self._levels.get((side, price_ticks))
        if level is None:
            return 0
        target = min(quantity, level.external_quantity)
        if target < quantity:
            self.diagnostics["taker_overlay_shortfall"] += 1
        self._remove_external_directionally(level, target, reverse=False)
        self._drop_empty_segments(level)
        self._assert_level(level)
        return target

    def trade(
        self,
        side: Side,
        price_ticks: int,
        quantity: int,
    ) -> QueueUpdate:
        """Apply one exact-price historical trade with one shared budget."""

        if quantity <= 0:
            raise QueueModelError("trade quantity must be positive")
        level = self._levels.get((side, price_ticks))
        if level is None:
            return QueueUpdate()

        budget = quantity
        removed_external = 0
        fills: list[QueueFillIntent] = []
        retained: list[QueueSegment] = []
        for segment in level.segments:
            if budget == 0:
                retained.append(segment)
                continue
            consumed = min(segment.quantity, budget)
            segment.quantity -= consumed
            budget -= consumed
            if isinstance(segment, ExternalSegment):
                removed_external += consumed
            elif consumed:
                fills.append(
                    QueueFillIntent(
                        order_id=segment.order_id,
                        side=side,
                        quantity=consumed,
                        price_ticks=price_ticks,
                        trigger="exact_price_trade",
                    )
                )
            if segment.quantity:
                retained.append(segment)

        level.segments = retained
        for fill in fills:
            if not any(
                isinstance(segment, OwnSegment) and segment.order_id == fill.order_id
                for segment in retained
            ):
                self._order_locations.pop(fill.order_id, None)
        if budget:
            self.diagnostics["unmatched_trade_quantity"] += budget
        key = (side, price_ticks)
        if not level.segments:
            self._levels.pop(key, None)
        if key in self._levels:
            self._assert_level(level)
        return QueueUpdate(
            fills=tuple(fills),
            external_quantity_removed=removed_external,
            unmatched_trade_quantity=budget,
        )

    def apply_market_event(
        self,
        event: MarketEvent,
        *,
        book_delta: BookDelta | None = None,
    ) -> QueueUpdate:
        """Replay the externally applied event through active queue overlays."""

        if event.event_type is EventType.RESET:
            lost = self.reset()
            return QueueUpdate(orders_lost_on_reset=lost)
        assert event.side is not None
        applied = (
            book_delta.quantity_applied if book_delta is not None else event.quantity
        )
        if applied <= 0:
            return QueueUpdate()
        if event.event_type in {EventType.ADD, EventType.SNAPSHOT}:
            self.add_external(event.side, event.price_ticks, applied)
            return QueueUpdate()
        if event.event_type is EventType.CANCEL:
            removed = self.cancel_external(
                event.side,
                event.price_ticks,
                applied,
            )
            return QueueUpdate(external_quantity_removed=removed)
        if event.event_type is EventType.TRADE:
            through_fills: list[QueueFillIntent] = []
            removed_through_external = 0
            if self.price_through_fills:
                through_fills, removed_through_external = self._fill_through_levels(
                    event.side,
                    event.price_ticks,
                )
            exact = self.trade(event.side, event.price_ticks, applied)
            return QueueUpdate(
                fills=tuple(through_fills) + exact.fills,
                external_quantity_removed=(
                    removed_through_external + exact.external_quantity_removed
                ),
                unmatched_trade_quantity=exact.unmatched_trade_quantity,
            )
        raise QueueModelError(f"unsupported event type: {event.event_type}")

    def reset(self) -> tuple[str, ...]:
        """Drop all queue estimates and return own orders requiring expiry."""

        lost = tuple(
            segment.order_id
            for level in self._levels.values()
            for segment in level.segments
            if isinstance(segment, OwnSegment)
        )
        self._levels.clear()
        self._order_locations.clear()
        return lost

    def reconciliation(
        self,
        side: Side,
        price_ticks: int,
        *,
        historical_external_quantity: int,
    ) -> QueueReconciliation:
        """Report, but do not mutate, historical/overlay depth divergence."""

        if historical_external_quantity < 0:
            raise QueueModelError("historical depth cannot be negative")
        level = self._levels.get((side, price_ticks))
        return QueueReconciliation(
            side=side,
            price_ticks=price_ticks,
            historical_external_quantity=historical_external_quantity,
            overlay_external_quantity=(
                level.external_quantity if level is not None else 0
            ),
            own_quantity=level.own_quantity if level is not None else 0,
        )

    def snapshot_segments(
        self,
        side: Side,
        price_ticks: int,
    ) -> tuple[tuple[str, str | None, int], ...]:
        """Stable, test/audit-friendly segment representation."""

        level = self._levels.get((side, price_ticks))
        if level is None:
            return ()
        return tuple(
            (
                "external" if isinstance(segment, ExternalSegment) else "own",
                segment.order_id if isinstance(segment, OwnSegment) else None,
                segment.quantity,
            )
            for segment in level.segments
        )

    def _fill_through_levels(
        self,
        side: Side,
        trade_price_ticks: int,
    ) -> tuple[list[QueueFillIntent], int]:
        if side is Side.BID:
            keys = sorted(
                (
                    key
                    for key in self._levels
                    if key[0] is side and key[1] > trade_price_ticks
                ),
                key=lambda key: key[1],
                reverse=True,
            )
        else:
            keys = sorted(
                (
                    key
                    for key in self._levels
                    if key[0] is side and key[1] < trade_price_ticks
                ),
                key=lambda key: key[1],
            )
        fills: list[QueueFillIntent] = []
        removed_external = 0
        for key in keys:
            level = self._levels.pop(key)
            for segment in level.segments:
                if isinstance(segment, ExternalSegment):
                    removed_external += segment.quantity
                else:
                    fills.append(
                        QueueFillIntent(
                            order_id=segment.order_id,
                            side=side,
                            quantity=segment.quantity,
                            price_ticks=level.price_ticks,
                            trigger="price_through",
                        )
                    )
                    self._order_locations.pop(segment.order_id, None)
        return fills, removed_external

    def _remove_external_directionally(
        self,
        level: PriceQueue,
        quantity: int,
        *,
        reverse: bool,
    ) -> None:
        remaining = quantity
        indices = (
            range(len(level.segments) - 1, -1, -1)
            if reverse
            else range(len(level.segments))
        )
        for index in indices:
            if remaining == 0:
                break
            segment = level.segments[index]
            if not isinstance(segment, ExternalSegment):
                continue
            removed = min(segment.quantity, remaining)
            segment.quantity -= removed
            remaining -= removed

    def _remove_external_pro_rata(
        self,
        level: PriceQueue,
        quantity: int,
    ) -> None:
        external = [
            segment
            for segment in level.segments
            if isinstance(segment, ExternalSegment)
        ]
        total = sum(segment.quantity for segment in external)
        if total == 0:
            return
        allocations: dict[int, int] = {}
        remainders: list[tuple[int, int]] = []
        allocated = 0
        for segment in external:
            numerator = quantity * segment.quantity
            base, remainder = divmod(numerator, total)
            allocations[segment.segment_id] = base
            allocated += base
            remainders.append((remainder, segment.segment_id))
        remainder_units = quantity - allocated
        for _, segment_id in sorted(
            remainders,
            key=lambda item: (-item[0], item[1]),
        )[:remainder_units]:
            allocations[segment_id] += 1
        for segment in external:
            segment.quantity -= allocations[segment.segment_id]

    def _drop_empty_segments(self, level: PriceQueue) -> None:
        level.segments = [segment for segment in level.segments if segment.quantity > 0]

    def _discard_if_no_own(self, key: QueueKey) -> None:
        level = self._levels.get(key)
        if level is None:
            return
        if not any(isinstance(segment, OwnSegment) for segment in level.segments):
            self._levels.pop(key, None)

    def _take_segment_id(self) -> int:
        segment_id = self._next_segment_id
        self._next_segment_id += 1
        return segment_id

    @staticmethod
    def _assert_level(level: PriceQueue) -> None:
        if any(segment.quantity <= 0 for segment in level.segments):
            raise QueueModelError("queue segments must have positive quantity")


# Public short name used by exchange/backtest composition.
QueueModel = SharedQueueLedger
