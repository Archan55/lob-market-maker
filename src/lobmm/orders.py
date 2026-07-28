"""Typed order commands, exchange state, fills, and lifecycle validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side

ZERO: Final = Decimal("0")


class OrderError(ValueError):
    """Base class for invalid order operations."""


class InvalidOrderTransition(OrderError):
    """Raised when an order lifecycle transition is not permitted."""


class DuplicateClientOrderId(OrderError):
    """Raised when a client order identifier is reused."""


class UnknownOrder(OrderError):
    """Raised when an order identifier is not present in the registry."""


@dataclass(frozen=True, slots=True)
class NewOrderRequest:
    """Immutable client instruction sent toward the simulated exchange."""

    client_order_id: str
    side: Side
    price_ticks: int
    quantity: int
    strategy_id: str
    decision_timestamp_ns: int
    send_timestamp_ns: int
    post_only: bool = False
    rest_unfilled: bool = True
    expire_timestamp_ns: int | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id:
            raise OrderError("client_order_id must not be empty")
        if not self.strategy_id:
            raise OrderError("strategy_id must not be empty")
        if not isinstance(self.side, Side):
            raise OrderError("side must be Side.BID or Side.ASK")
        if self.price_ticks <= 0:
            raise OrderError("price_ticks must be positive")
        if self.quantity <= 0:
            raise OrderError("quantity must be positive")
        if self.decision_timestamp_ns < 0:
            raise OrderError("decision_timestamp_ns must be nonnegative")
        if self.send_timestamp_ns < self.decision_timestamp_ns:
            raise OrderError("send timestamp cannot precede decision timestamp")
        if (
            self.expire_timestamp_ns is not None
            and self.expire_timestamp_ns < self.send_timestamp_ns
        ):
            raise OrderError("expiry cannot precede send timestamp")


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """Cancel an exchange order by exchange or client identifier."""

    decision_timestamp_ns: int
    send_timestamp_ns: int
    order_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.order_id) == bool(self.client_order_id):
            raise OrderError(
                "cancel request requires exactly one of order_id/client_order_id"
            )
        if self.decision_timestamp_ns < 0:
            raise OrderError("decision_timestamp_ns must be nonnegative")
        if self.send_timestamp_ns < self.decision_timestamp_ns:
            raise OrderError("send timestamp cannot precede decision timestamp")


@dataclass(frozen=True, slots=True)
class ReplaceRequest:
    """Non-atomic replace normalized into an ordered cancel and new request."""

    cancel: CancelRequest
    new_order: NewOrderRequest

    def normalize(self) -> tuple[CancelRequest, NewOrderRequest]:
        return self.cancel, self.new_order


ExchangeCommand = NewOrderRequest | CancelRequest


@dataclass(frozen=True, slots=True)
class QueuePosition:
    """Derived Level 2 queue estimate for one own order."""

    external_ahead: int
    own_ahead: int
    external_behind: int

    def __post_init__(self) -> None:
        if min(self.external_ahead, self.own_ahead, self.external_behind) < 0:
            raise OrderError("queue position components cannot be negative")

    @property
    def total_ahead(self) -> int:
        return self.external_ahead + self.own_ahead


@dataclass(slots=True)
class ExchangeOrder:
    """Authoritative mutable order record owned only by the exchange."""

    order_id: str
    client_order_id: str
    strategy_id: str
    side: Side
    price_ticks: int
    original_quantity: int
    remaining_quantity: int
    creation_timestamp_ns: int
    send_timestamp_ns: int
    exchange_arrival_timestamp_ns: int
    expire_timestamp_ns: int | None = None
    status: OrderStatus = OrderStatus.PENDING_ARRIVAL
    cumulative_filled_quantity: int = 0
    cumulative_fill_notional_ticks: int = 0
    last_update_timestamp_ns: int | None = None
    queue_position: QueuePosition = field(
        default_factory=lambda: QueuePosition(0, 0, 0)
    )
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.client_order_id or not self.strategy_id:
            raise OrderError("order, client, and strategy IDs must not be empty")
        if self.price_ticks <= 0 or self.original_quantity <= 0:
            raise OrderError("order price and original quantity must be positive")
        if self.remaining_quantity != self.original_quantity:
            raise OrderError(
                "new exchange order must start with full remaining quantity"
            )
        if self.creation_timestamp_ns < 0:
            raise OrderError("creation_timestamp_ns must be nonnegative")
        if self.send_timestamp_ns < self.creation_timestamp_ns:
            raise OrderError("send timestamp cannot precede creation timestamp")
        if self.exchange_arrival_timestamp_ns < self.send_timestamp_ns:
            raise OrderError("exchange arrival cannot precede send timestamp")
        if self.last_update_timestamp_ns is None:
            self.last_update_timestamp_ns = self.exchange_arrival_timestamp_ns
        self.assert_quantity_identity()

    @property
    def average_fill_price_ticks(self) -> Decimal | None:
        if self.cumulative_filled_quantity == 0:
            return None
        return Decimal(self.cumulative_fill_notional_ticks) / Decimal(
            self.cumulative_filled_quantity
        )

    @property
    def is_fillable(self) -> bool:
        return self.status in {OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED}

    def assert_quantity_identity(self) -> None:
        if self.remaining_quantity < 0 or self.cumulative_filled_quantity < 0:
            raise OrderError("order quantities cannot be negative")
        if (
            self.remaining_quantity + self.cumulative_filled_quantity
            != self.original_quantity
        ):
            raise OrderError(
                "original quantity must equal filled plus remaining quantity"
            )


@dataclass(frozen=True, slots=True)
class Fill:
    """One immutable maker or taker execution."""

    fill_id: str
    order_id: str
    client_order_id: str
    strategy_id: str
    side: Side
    quantity: int
    price_ticks: int
    liquidity_role: LiquidityRole
    exchange_fill_timestamp_ns: int
    fee: Decimal = ZERO
    rebate: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.fill_id or not self.order_id or not self.client_order_id:
            raise OrderError("fill and order identifiers must not be empty")
        if not self.strategy_id:
            raise OrderError("strategy_id must not be empty")
        if self.quantity <= 0 or self.price_ticks <= 0:
            raise OrderError("fill quantity and price must be positive")
        if self.exchange_fill_timestamp_ns < 0:
            raise OrderError("fill timestamp must be nonnegative")
        if self.fee < ZERO or self.rebate < ZERO:
            raise OrderError("fee and rebate must be nonnegative")

    @property
    def signed_quantity(self) -> int:
        return int(self.side) * self.quantity


@dataclass(frozen=True, slots=True)
class OrderTransition:
    """Auditable, deterministic lifecycle transition."""

    transition_id: str
    order_id: str
    previous_status: OrderStatus | None
    new_status: OrderStatus
    timestamp_ns: int
    remaining_quantity: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Immutable exchange report before transport latency is applied."""

    report_id: str
    report_type: ReportType
    exchange_timestamp_ns: int
    order_id: str
    client_order_id: str
    order_status: OrderStatus
    remaining_quantity: int
    fill: Fill | None = None
    reason: str | None = None


_ALLOWED_TRANSITIONS: Final[dict[OrderStatus, frozenset[OrderStatus]]] = {
    OrderStatus.PENDING_ARRIVAL: frozenset({OrderStatus.LIVE, OrderStatus.REJECTED}),
    OrderStatus.LIVE: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class OrderRegistry:
    """Order ownership, state validation, and deterministic identifier source."""

    def __init__(self) -> None:
        self._orders: dict[str, ExchangeOrder] = {}
        self._by_client_id: dict[str, str] = {}
        self._live_order_ids: dict[str, None] = {}
        self._next_order_id = 1
        self._next_fill_id = 1
        self._next_transition_id = 1
        self._next_report_id = 1
        self.transitions: list[OrderTransition] = []
        self.fills: list[Fill] = []
        self.reports: list[ExecutionReport] = []

    @property
    def orders(self) -> tuple[ExchangeOrder, ...]:
        return tuple(self._orders.values())

    @property
    def live_orders(self) -> tuple[ExchangeOrder, ...]:
        """Return fillable orders without scanning terminal history."""

        return tuple(self._orders[order_id] for order_id in self._live_order_ids)

    def create(
        self,
        request: NewOrderRequest,
        *,
        exchange_arrival_timestamp_ns: int,
    ) -> ExchangeOrder:
        if request.client_order_id in self._by_client_id:
            raise DuplicateClientOrderId(request.client_order_id)
        if exchange_arrival_timestamp_ns < request.send_timestamp_ns:
            raise OrderError("exchange arrival cannot precede send timestamp")

        order_id = f"O{self._next_order_id:012d}"
        self._next_order_id += 1
        order = ExchangeOrder(
            order_id=order_id,
            client_order_id=request.client_order_id,
            strategy_id=request.strategy_id,
            side=request.side,
            price_ticks=request.price_ticks,
            original_quantity=request.quantity,
            remaining_quantity=request.quantity,
            creation_timestamp_ns=request.decision_timestamp_ns,
            send_timestamp_ns=request.send_timestamp_ns,
            exchange_arrival_timestamp_ns=exchange_arrival_timestamp_ns,
            expire_timestamp_ns=request.expire_timestamp_ns,
        )
        self._orders[order_id] = order
        self._by_client_id[request.client_order_id] = order_id
        self._record_transition(
            order=order,
            previous_status=None,
            timestamp_ns=exchange_arrival_timestamp_ns,
            reason="created",
        )
        return order

    def get(self, order_id: str) -> ExchangeOrder:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise UnknownOrder(order_id) from exc

    def get_by_client_id(self, client_order_id: str) -> ExchangeOrder:
        try:
            order_id = self._by_client_id[client_order_id]
        except KeyError as exc:
            raise UnknownOrder(client_order_id) from exc
        return self._orders[order_id]

    def resolve_cancel(self, request: CancelRequest) -> ExchangeOrder:
        if request.order_id is not None:
            return self.get(request.order_id)
        assert request.client_order_id is not None
        return self.get_by_client_id(request.client_order_id)

    def transition(
        self,
        order: ExchangeOrder,
        new_status: OrderStatus,
        *,
        timestamp_ns: int,
        reason: str | None = None,
    ) -> OrderTransition:
        previous = order.status
        if new_status not in _ALLOWED_TRANSITIONS[previous]:
            raise InvalidOrderTransition(
                f"{order.order_id}: {previous.value} -> {new_status.value}"
            )
        if timestamp_ns < (order.last_update_timestamp_ns or 0):
            raise OrderError("order transition timestamp moved backward")
        order.status = new_status
        order.last_update_timestamp_ns = timestamp_ns
        if order.is_fillable:
            self._live_order_ids[order.order_id] = None
        else:
            self._live_order_ids.pop(order.order_id, None)
        if new_status is OrderStatus.REJECTED:
            order.rejection_reason = reason
        order.assert_quantity_identity()
        return self._record_transition(
            order=order,
            previous_status=previous,
            timestamp_ns=timestamp_ns,
            reason=reason,
        )

    def apply_fill(
        self,
        order: ExchangeOrder,
        *,
        quantity: int,
        price_ticks: int,
        liquidity_role: LiquidityRole,
        exchange_fill_timestamp_ns: int,
        fee: Decimal = ZERO,
        rebate: Decimal = ZERO,
    ) -> tuple[Fill, OrderTransition]:
        if not order.is_fillable:
            raise OrderError(f"order {order.order_id} is not fillable")
        if quantity <= 0:
            raise OrderError("fill quantity must be positive")
        if quantity > order.remaining_quantity:
            raise OrderError(
                f"fill {quantity} exceeds remaining {order.remaining_quantity}"
            )
        if exchange_fill_timestamp_ns < (order.last_update_timestamp_ns or 0):
            raise OrderError("fill timestamp moved backward")

        order.remaining_quantity -= quantity
        order.cumulative_filled_quantity += quantity
        order.cumulative_fill_notional_ticks += quantity * price_ticks
        new_status = (
            OrderStatus.FILLED
            if order.remaining_quantity == 0
            else OrderStatus.PARTIALLY_FILLED
        )
        transition = self.transition(
            order,
            new_status,
            timestamp_ns=exchange_fill_timestamp_ns,
            reason="fill",
        )
        fill = Fill(
            fill_id=f"F{self._next_fill_id:012d}",
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            strategy_id=order.strategy_id,
            side=order.side,
            quantity=quantity,
            price_ticks=price_ticks,
            liquidity_role=liquidity_role,
            exchange_fill_timestamp_ns=exchange_fill_timestamp_ns,
            fee=fee,
            rebate=rebate,
        )
        self._next_fill_id += 1
        self.fills.append(fill)
        return fill, transition

    def make_report(
        self,
        order: ExchangeOrder,
        *,
        report_type: ReportType,
        exchange_timestamp_ns: int,
        fill: Fill | None = None,
        reason: str | None = None,
    ) -> ExecutionReport:
        report = ExecutionReport(
            report_id=f"R{self._next_report_id:012d}",
            report_type=report_type,
            exchange_timestamp_ns=exchange_timestamp_ns,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            order_status=order.status,
            remaining_quantity=order.remaining_quantity,
            fill=fill,
            reason=reason,
        )
        self._next_report_id += 1
        self.reports.append(report)
        return report

    def make_unknown_cancel_report(
        self,
        request: CancelRequest,
        *,
        exchange_timestamp_ns: int,
        reason: str = "unknown_order",
    ) -> ExecutionReport:
        """Create an auditable rejection when no exchange order exists."""

        report = ExecutionReport(
            report_id=f"R{self._next_report_id:012d}",
            report_type=ReportType.CANCEL_REJECTED,
            exchange_timestamp_ns=exchange_timestamp_ns,
            order_id=request.order_id or "",
            client_order_id=request.client_order_id or "",
            order_status=OrderStatus.REJECTED,
            remaining_quantity=0,
            reason=reason,
        )
        self._next_report_id += 1
        self.reports.append(report)
        return report

    def _record_transition(
        self,
        *,
        order: ExchangeOrder,
        previous_status: OrderStatus | None,
        timestamp_ns: int,
        reason: str | None,
    ) -> OrderTransition:
        transition = OrderTransition(
            transition_id=f"T{self._next_transition_id:012d}",
            order_id=order.order_id,
            previous_status=previous_status,
            new_status=order.status,
            timestamp_ns=timestamp_ns,
            remaining_quantity=order.remaining_quantity,
            reason=reason,
        )
        self._next_transition_id += 1
        self.transitions.append(transition)
        return transition
