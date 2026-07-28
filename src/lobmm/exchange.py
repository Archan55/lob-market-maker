"""Simulated exchange composition for book, orders, queues, and fills."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from lobmm.book import L2Book
from lobmm.config import (
    ExchangeConfig,
    FeeConfig,
    InstrumentConfig,
    QueueModelConfig,
)
from lobmm.enums import (
    LiquidityRole,
    OrderStatus,
    ReportType,
    Side,
)
from lobmm.events import BookDelta, MarketEvent
from lobmm.orders import (
    CancelRequest,
    DuplicateClientOrderId,
    ExchangeCommand,
    ExchangeOrder,
    ExecutionReport,
    Fill,
    NewOrderRequest,
    OrderRegistry,
    UnknownOrder,
)
from lobmm.queue_model import QueueModel, QueueUpdate

type OrderRejector = Callable[[NewOrderRequest, tuple[ExchangeOrder, ...]], str | None]
type FillSink = Callable[[Fill], object]


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """All deterministic effects produced by one exchange input."""

    book_delta: BookDelta | None = None
    reports: tuple[ExecutionReport, ...] = ()
    fills: tuple[Fill, ...] = ()
    queue_update: QueueUpdate | None = None
    command_rejection_reason: str | None = None


class Exchange:
    """Authoritative true exchange state for one historical instrument."""

    def __init__(
        self,
        *,
        book: L2Book | None = None,
        registry: OrderRegistry | None = None,
        queue_model: QueueModel | None = None,
        exchange_config: ExchangeConfig | None = None,
        queue_config: QueueModelConfig | None = None,
        fee_config: FeeConfig | None = None,
        instrument_config: InstrumentConfig | None = None,
        order_rejector: OrderRejector | None = None,
        fill_sink: FillSink | None = None,
    ) -> None:
        self.exchange_config = exchange_config or ExchangeConfig()
        self.queue_config = queue_config or QueueModelConfig()
        self.fee_config = fee_config or FeeConfig()
        self.instrument_config = instrument_config or InstrumentConfig()
        self.book = (
            book if book is not None else L2Book(self.exchange_config.validation_mode)
        )
        self.registry = registry if registry is not None else OrderRegistry()
        self.queue_model = (
            queue_model
            if queue_model is not None
            else QueueModel(
                cancellation_allocation=self.queue_config.cancellation_allocation,
                price_through_fills=self.queue_config.price_through_fills,
            )
        )
        self._order_rejector = order_rejector
        self._fill_sink = fill_sink

    @property
    def live_orders(self) -> tuple[ExchangeOrder, ...]:
        return self.registry.live_orders

    def handle_market_event(self, event: MarketEvent) -> ExchangeResult:
        """Apply true external data, then queue/fill consequences."""

        delta = self.book.apply(event)
        queue_update = self.queue_model.apply_market_event(
            event,
            book_delta=delta,
        )
        reports: list[ExecutionReport] = []
        fills: list[Fill] = []

        for intent in queue_update.fills:
            order = self.registry.get(intent.order_id)
            if not order.is_fillable:
                # This indicates an internal queue/order ownership bug rather
                # than malformed historical input.
                raise RuntimeError(
                    f"queue attempted to fill terminal order {order.order_id}"
                )
            quantity = min(intent.quantity, order.remaining_quantity)
            if quantity != intent.quantity:
                raise RuntimeError(
                    f"queue fill {intent.quantity} exceeds order remaining "
                    f"{order.remaining_quantity}: {order.order_id}"
                )
            fill, report = self._book_fill(
                order,
                quantity=quantity,
                price_ticks=intent.price_ticks,
                liquidity_role=LiquidityRole.MAKER,
                timestamp_ns=event.timestamp_ns,
            )
            fills.append(fill)
            reports.append(report)

        for order_id in queue_update.orders_lost_on_reset:
            order = self.registry.get(order_id)
            if not order.is_fillable:
                continue
            if self.exchange_config.cancel_orders_on_reset:
                self.registry.transition(
                    order,
                    OrderStatus.EXPIRED,
                    timestamp_ns=event.timestamp_ns,
                    reason="market_reset",
                )
                reports.append(
                    self.registry.make_report(
                        order,
                        report_type=ReportType.EXPIRED,
                        exchange_timestamp_ns=event.timestamp_ns,
                        reason="market_reset",
                    )
                )
            else:
                # Priority survival through a reset is unobservable. The
                # configured optimistic sensitivity re-adds the order ahead of
                # subsequently replayed snapshot levels.
                self.queue_model.join(
                    order,
                    displayed_external_quantity=self.book.quantity_at(
                        order.side,
                        order.price_ticks,
                    ),
                )

        if event.side is not None:
            self._refresh_queue_positions(event.side, event.price_ticks)
        return ExchangeResult(
            book_delta=delta,
            reports=tuple(reports),
            fills=tuple(fills),
            queue_update=queue_update,
        )

    def handle_command(
        self,
        command: ExchangeCommand,
        *,
        exchange_arrival_timestamp_ns: int,
    ) -> ExchangeResult:
        """Apply one new/cancel command at its delayed venue-arrival time."""

        if exchange_arrival_timestamp_ns < 0:
            raise ValueError("exchange arrival timestamp must be nonnegative")
        if isinstance(command, NewOrderRequest):
            return self._handle_new(command, exchange_arrival_timestamp_ns)
        if isinstance(command, CancelRequest):
            return self._handle_cancel(command, exchange_arrival_timestamp_ns)
        raise TypeError(f"unsupported exchange command: {type(command)!r}")

    def expire_order(
        self,
        order_id: str,
        *,
        timestamp_ns: int,
    ) -> ExchangeResult:
        """Expire remaining quantity at a scheduled venue-control event."""

        order = self.registry.get(order_id)
        if not order.is_fillable:
            return ExchangeResult()
        self.queue_model.remove_order(order_id)
        self.registry.transition(
            order,
            OrderStatus.EXPIRED,
            timestamp_ns=timestamp_ns,
            reason="time_in_force",
        )
        report = self.registry.make_report(
            order,
            report_type=ReportType.EXPIRED,
            exchange_timestamp_ns=timestamp_ns,
            reason="time_in_force",
        )
        self._refresh_queue_positions(order.side, order.price_ticks)
        return ExchangeResult(reports=(report,))

    def _handle_new(
        self,
        request: NewOrderRequest,
        arrival_ns: int,
    ) -> ExchangeResult:
        try:
            order = self.registry.create(
                request,
                exchange_arrival_timestamp_ns=arrival_ns,
            )
        except DuplicateClientOrderId:
            return ExchangeResult(command_rejection_reason="duplicate_client_order_id")

        reject_reason = self._validate_new_order(request, order, arrival_ns)
        if reject_reason is not None:
            self.registry.transition(
                order,
                OrderStatus.REJECTED,
                timestamp_ns=arrival_ns,
                reason=reject_reason,
            )
            report = self.registry.make_report(
                order,
                report_type=ReportType.REJECTED,
                exchange_timestamp_ns=arrival_ns,
                reason=reject_reason,
            )
            return ExchangeResult(
                reports=(report,),
                command_rejection_reason=reject_reason,
            )

        self.registry.transition(
            order,
            OrderStatus.LIVE,
            timestamp_ns=arrival_ns,
            reason="accepted",
        )
        reports: list[ExecutionReport] = [
            self.registry.make_report(
                order,
                report_type=ReportType.ACCEPTED,
                exchange_timestamp_ns=arrival_ns,
            )
        ]
        fills: list[Fill] = []

        marketable_at_arrival = self._is_marketable(order)
        if marketable_at_arrival:
            opposite = order.side.opposite
            for price_ticks, displayed in self.book.executable_levels(
                order.side,
                order.price_ticks,
            ):
                if order.remaining_quantity == 0:
                    break
                requested = min(order.remaining_quantity, displayed)
                consumed = self.book.consume(opposite, price_ticks, requested)
                if consumed == 0:
                    continue
                self.queue_model.consume_external(
                    opposite,
                    price_ticks,
                    consumed,
                )
                fill, report = self._book_fill(
                    order,
                    quantity=consumed,
                    price_ticks=price_ticks,
                    liquidity_role=LiquidityRole.TAKER,
                    timestamp_ns=arrival_ns,
                )
                fills.append(fill)
                reports.append(report)
                self._refresh_queue_positions(opposite, price_ticks)

        if order.remaining_quantity:
            may_rest = not marketable_at_arrival or (
                request.rest_unfilled
                and self.exchange_config.rest_unfilled_marketable_quantity
            )
            if may_rest:
                order.queue_position = self.queue_model.join(
                    order,
                    displayed_external_quantity=self.book.quantity_at(
                        order.side,
                        order.price_ticks,
                    ),
                )
            else:
                self.registry.transition(
                    order,
                    OrderStatus.EXPIRED,
                    timestamp_ns=arrival_ns,
                    reason="unfilled_remainder_not_resting",
                )
                reports.append(
                    self.registry.make_report(
                        order,
                        report_type=ReportType.EXPIRED,
                        exchange_timestamp_ns=arrival_ns,
                        reason="unfilled_remainder_not_resting",
                    )
                )

        return ExchangeResult(reports=tuple(reports), fills=tuple(fills))

    def _handle_cancel(
        self,
        request: CancelRequest,
        arrival_ns: int,
    ) -> ExchangeResult:
        if arrival_ns < request.send_timestamp_ns:
            raise ValueError("cancel arrival cannot precede send timestamp")
        try:
            order = self.registry.resolve_cancel(request)
        except UnknownOrder:
            report = self.registry.make_unknown_cancel_report(
                request,
                exchange_timestamp_ns=arrival_ns,
            )
            return ExchangeResult(
                reports=(report,),
                command_rejection_reason="unknown_order",
            )

        if not order.is_fillable:
            report = self.registry.make_report(
                order,
                report_type=ReportType.CANCEL_REJECTED,
                exchange_timestamp_ns=arrival_ns,
                reason="order_not_live",
            )
            return ExchangeResult(
                reports=(report,),
                command_rejection_reason="order_not_live",
            )

        self.queue_model.remove_order(order.order_id)
        self.registry.transition(
            order,
            OrderStatus.CANCELLED,
            timestamp_ns=arrival_ns,
            reason="cancel_arrived",
        )
        report = self.registry.make_report(
            order,
            report_type=ReportType.CANCELLED,
            exchange_timestamp_ns=arrival_ns,
        )
        self._refresh_queue_positions(order.side, order.price_ticks)
        return ExchangeResult(reports=(report,))

    def _validate_new_order(
        self,
        request: NewOrderRequest,
        order: ExchangeOrder,
        arrival_ns: int,
    ) -> str | None:
        if arrival_ns < request.send_timestamp_ns:
            return "arrival_before_send"
        if request.quantity % self.instrument_config.lot_size:
            return "invalid_lot_size"
        if request.expire_timestamp_ns is not None and arrival_ns >= (
            request.expire_timestamp_ns
        ):
            return "expired_before_arrival"
        if self._would_self_trade(order):
            return "self_trade_prevention"
        if request.post_only and self._is_marketable(order):
            return "post_only_would_cross"
        if self._order_rejector is not None:
            return self._order_rejector(request, self.live_orders)
        return None

    def _is_marketable(self, order: ExchangeOrder) -> bool:
        if order.side is Side.BID:
            best_ask = self.book.best_ask
            return best_ask is not None and best_ask <= order.price_ticks
        best_bid = self.book.best_bid
        return best_bid is not None and best_bid >= order.price_ticks

    def _would_self_trade(self, incoming: ExchangeOrder) -> bool:
        for resting in self.live_orders:
            if resting.side is incoming.side:
                continue
            if incoming.side is Side.BID and incoming.price_ticks >= (
                resting.price_ticks
            ):
                return True
            if incoming.side is Side.ASK and incoming.price_ticks <= (
                resting.price_ticks
            ):
                return True
        return False

    def _book_fill(
        self,
        order: ExchangeOrder,
        *,
        quantity: int,
        price_ticks: int,
        liquidity_role: LiquidityRole,
        timestamp_ns: int,
    ) -> tuple[Fill, ExecutionReport]:
        fee, rebate = self._economics(
            quantity=quantity,
            price_ticks=price_ticks,
            liquidity_role=liquidity_role,
        )
        fill, _ = self.registry.apply_fill(
            order,
            quantity=quantity,
            price_ticks=price_ticks,
            liquidity_role=liquidity_role,
            exchange_fill_timestamp_ns=timestamp_ns,
            fee=fee,
            rebate=rebate,
        )
        if self._fill_sink is not None:
            self._fill_sink(fill)
        report_type = (
            ReportType.FILL
            if order.status is OrderStatus.FILLED
            else ReportType.PARTIAL_FILL
        )
        report = self.registry.make_report(
            order,
            report_type=report_type,
            exchange_timestamp_ns=timestamp_ns,
            fill=fill,
        )
        return fill, report

    def _economics(
        self,
        *,
        quantity: int,
        price_ticks: int,
        liquidity_role: LiquidityRole,
    ) -> tuple[Decimal, Decimal]:
        notional = (
            Decimal(price_ticks) * self.instrument_config.tick_size * Decimal(quantity)
        )
        proportional = notional * self.fee_config.proportional_fee_rate
        if liquidity_role is LiquidityRole.MAKER:
            fee = self.fee_config.maker_fee_per_unit * Decimal(quantity) + proportional
            rebate = self.fee_config.maker_rebate_per_unit * Decimal(quantity)
            return fee, rebate
        fee = self.fee_config.taker_fee_per_unit * Decimal(quantity) + proportional
        return fee, Decimal("0")

    def _refresh_queue_positions(
        self,
        side: Side,
        price_ticks: int,
    ) -> None:
        for order_id, position in self.queue_model.positions_at(
            side,
            price_ticks,
        ).items():
            self.registry.get(order_id).queue_position = position


# Explicit research-facing name.
SimulatedExchange = Exchange
