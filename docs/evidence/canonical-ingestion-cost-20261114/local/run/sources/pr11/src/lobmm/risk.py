"""Deterministic pre-trade and continuous risk controls."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from lobmm.config import RiskConfig
from lobmm.enums import RiskReason, SessionEndPolicy, Side

DecimalLike = Decimal | int | float | str


class RiskError(ValueError):
    """Raised when risk inputs are internally invalid."""


def _decimal(value: DecimalLike) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _is_side(value: object) -> bool:
    """Keep the runtime boundary defensive without confusing static narrowing."""

    return isinstance(value, Side)


@dataclass(frozen=True, slots=True)
class OpenOrderExposure:
    """Minimal live-order view needed by risk."""

    order_id: str
    side: Side
    remaining_quantity: int
    accepted_timestamp_ns: int
    is_quote: bool = True

    def __post_init__(self) -> None:
        if not self.order_id:
            raise RiskError("order_id cannot be empty")
        if not isinstance(self.side, Side):
            raise RiskError("side must be a Side")
        if self.remaining_quantity <= 0:
            raise RiskError("remaining_quantity must be positive")
        if self.accepted_timestamp_ns < 0:
            raise RiskError("accepted_timestamp_ns must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProjectedExposure:
    """Worst-case fills without netting live bids against live asks."""

    worst_long_inventory: int
    worst_short_inventory: int
    total_open_quantity: int
    open_order_count: int


@dataclass(frozen=True, slots=True)
class OrderRiskRequest:
    """One proposed client action at its decision/send timestamp."""

    side: Side
    quantity: int
    timestamp_ns: int
    price_ticks: int | None = None
    order_id: str | None = None
    reduce_only: bool = False
    is_quote: bool = True
    may_rest: bool = True


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Authoritative inputs for a pre-trade decision."""

    inventory: int
    open_orders: tuple[OpenOrderExposure, ...] = ()
    spread_ticks: int | None = None
    volatility_ticks: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: RiskReason | None = None
    detail: str = ""
    cancel_open_orders: bool = False
    risk_reducing: bool = False


@dataclass(frozen=True, slots=True)
class RiskEvent:
    timestamp_ns: int
    reason: RiskReason
    detail: str
    order_id: str | None = None
    observed_value: Decimal | int | None = None
    limit_value: Decimal | int | None = None


@dataclass(frozen=True, slots=True)
class SessionDirective:
    """Actions requested by the configured end-of-session policy."""

    active: bool
    block_new_quotes: bool = False
    cancel_order_ids: tuple[str, ...] = ()
    liquidation_side: Side | None = None
    liquidation_quantity: int = 0
    mark_remaining_inventory: bool = False


@dataclass(frozen=True, slots=True)
class ContinuousRiskResult:
    kill_switch_active: bool
    cancel_order_ids: tuple[str, ...]
    session: SessionDirective


def project_worst_case_exposure(
    inventory: int,
    open_orders: Iterable[OpenOrderExposure],
    proposed: OrderRiskRequest | None = None,
) -> ProjectedExposure:
    """Project each directional extreme independently.

    A live ask never offsets a live bid for this calculation because execution
    of one side is not conditional on execution of the other.
    """

    bid_quantity = 0
    ask_quantity = 0
    total_quantity = 0
    count = 0
    for order in open_orders:
        total_quantity += order.remaining_quantity
        count += 1
        if order.side is Side.BID:
            bid_quantity += order.remaining_quantity
        else:
            ask_quantity += order.remaining_quantity

    if proposed is not None:
        if proposed.quantity <= 0:
            raise RiskError("proposed quantity must be positive")
        if proposed.side is Side.BID:
            bid_quantity += proposed.quantity
        else:
            ask_quantity += proposed.quantity
        if proposed.may_rest:
            total_quantity += proposed.quantity
            count += 1

    return ProjectedExposure(
        worst_long_inventory=inventory + bid_quantity,
        worst_short_inventory=inventory - ask_quantity,
        total_open_quantity=total_quantity,
        open_order_count=count,
    )


class RiskManager:
    """Stateful, auditable risk gate for a single-instrument backtest."""

    def __init__(
        self,
        config: RiskConfig,
        *,
        session_start_ns: int | None = None,
        session_end_ns: int | None = None,
        session_end_policy: SessionEndPolicy = SessionEndPolicy.MARK,
    ) -> None:
        if session_start_ns is not None and session_start_ns < 0:
            raise RiskError("session_start_ns must be nonnegative")
        if session_end_ns is not None and session_end_ns < 0:
            raise RiskError("session_end_ns must be nonnegative")
        if (
            session_start_ns is not None
            and session_end_ns is not None
            and session_start_ns > session_end_ns
        ):
            raise RiskError("session start cannot follow session end")
        if not isinstance(session_end_policy, SessionEndPolicy):
            raise RiskError("session_end_policy must be a SessionEndPolicy")

        self.config = config
        self.session_start_ns = session_start_ns
        self.session_end_ns = session_end_ns
        self.session_end_policy = session_end_policy
        self.kill_switch_active = False
        self.kill_reason: RiskReason | None = None
        self.high_watermark = Decimal(0)
        self.events: list[RiskEvent] = []
        self._message_timestamps: deque[int] = deque()
        self._reported_stale_orders: set[str] = set()

    def evaluate(self, request: OrderRiskRequest, context: RiskContext) -> RiskDecision:
        """Apply all applicable pre-trade checks in stable priority order."""

        invalid = self._validate_request(request, context)
        if invalid is not None:
            return invalid
        risk_reducing = self._is_valid_reduce_only(request, context.inventory)
        if request.reduce_only and not risk_reducing:
            return self._reject(
                request,
                RiskReason.INVALID_ORDER,
                "reduce-only order must oppose and not exceed current inventory",
            )

        if self.kill_switch_active and not risk_reducing:
            return self._reject(
                request,
                RiskReason.KILL_SWITCH,
                "kill switch is active",
                cancel_open_orders=True,
            )

        session_rejection = self._session_rejection(request, risk_reducing)
        if session_rejection is not None:
            return session_rejection

        if request.quantity > self.config.max_order_size:
            return self._reject(
                request,
                RiskReason.MAX_ORDER_SIZE,
                "order quantity exceeds maximum",
                request.quantity,
                self.config.max_order_size,
            )

        exposure = project_worst_case_exposure(
            context.inventory, context.open_orders, request
        )
        if request.may_rest:
            if exposure.total_open_quantity > self.config.max_total_open_quantity:
                return self._reject(
                    request,
                    RiskReason.MAX_OPEN_QUANTITY,
                    "projected open quantity exceeds maximum",
                    exposure.total_open_quantity,
                    self.config.max_total_open_quantity,
                )
            if exposure.open_order_count > self.config.max_open_orders:
                return self._reject(
                    request,
                    RiskReason.MAX_OPEN_ORDERS,
                    "projected open order count exceeds maximum",
                    exposure.open_order_count,
                    self.config.max_open_orders,
                )

        if not risk_reducing:
            if (
                request.side is Side.BID
                and exposure.worst_long_inventory > self.config.max_abs_inventory
            ):
                return self._reject(
                    request,
                    RiskReason.POSITION_LIMIT,
                    "worst-case long inventory exceeds maximum",
                    exposure.worst_long_inventory,
                    self.config.max_abs_inventory,
                )
            if (
                request.side is Side.ASK
                and exposure.worst_short_inventory < -self.config.max_abs_inventory
            ):
                return self._reject(
                    request,
                    RiskReason.POSITION_LIMIT,
                    "worst-case short inventory exceeds maximum",
                    exposure.worst_short_inventory,
                    -self.config.max_abs_inventory,
                )

        if request.is_quote and not risk_reducing:
            if (
                context.spread_ticks is not None
                and context.spread_ticks < self.config.minimum_spread_ticks
            ):
                return self._reject(
                    request,
                    RiskReason.SPREAD_TOO_NARROW,
                    "observed spread is below the quoting minimum",
                    context.spread_ticks,
                    self.config.minimum_spread_ticks,
                )
            maximum_volatility = self.config.max_volatility_ticks
            if (
                maximum_volatility is not None
                and context.volatility_ticks is not None
                and context.volatility_ticks > _decimal(maximum_volatility)
            ):
                return self._reject(
                    request,
                    RiskReason.VOLATILITY_TOO_HIGH,
                    "observed volatility exceeds the quoting maximum",
                    context.volatility_ticks,
                    _decimal(maximum_volatility),
                )

        if not self._message_rate_available(request.timestamp_ns):
            return self._reject(
                request,
                RiskReason.MESSAGE_RATE,
                "outbound message rate limit reached",
                self._current_message_count(request.timestamp_ns),
                self.config.max_messages_per_second,
            )

        self._record_message(request.timestamp_ns)
        return RiskDecision(
            approved=True,
            detail="approved",
            risk_reducing=risk_reducing,
        )

    pre_trade_check = evaluate

    def check_order(
        self,
        *,
        side: Side,
        quantity: int,
        timestamp_ns: int,
        inventory: int,
        open_orders: Iterable[OpenOrderExposure] = (),
        price_ticks: int | None = None,
        order_id: str | None = None,
        reduce_only: bool = False,
        is_quote: bool = True,
        may_rest: bool = True,
        spread_ticks: int | None = None,
        volatility_ticks: DecimalLike | None = None,
    ) -> RiskDecision:
        """Convenience wrapper for callers that do not build context objects."""

        request = OrderRiskRequest(
            side=side,
            quantity=quantity,
            timestamp_ns=timestamp_ns,
            price_ticks=price_ticks,
            order_id=order_id,
            reduce_only=reduce_only,
            is_quote=is_quote,
            may_rest=may_rest,
        )
        context = RiskContext(
            inventory=inventory,
            open_orders=tuple(open_orders),
            spread_ticks=spread_ticks,
            volatility_ticks=(
                None if volatility_ticks is None else _decimal(volatility_ticks)
            ),
        )
        return self.evaluate(request, context)

    def observe_pnl(self, net_pnl: DecimalLike, *, timestamp_ns: int) -> RiskDecision:
        """Update continuous loss/drawdown state and activate the kill switch."""

        if timestamp_ns < 0:
            raise RiskError("timestamp_ns must be nonnegative")
        pnl = _decimal(net_pnl)
        self.high_watermark = max(self.high_watermark, pnl)
        if pnl <= -self.config.max_loss:
            return self.activate_kill_switch(
                timestamp_ns=timestamp_ns,
                reason=RiskReason.MAX_LOSS,
                detail="net P&L breached maximum loss",
                observed_value=pnl,
                limit_value=-self.config.max_loss,
            )
        drawdown = self.high_watermark - pnl
        if drawdown >= self.config.max_drawdown:
            return self.activate_kill_switch(
                timestamp_ns=timestamp_ns,
                reason=RiskReason.MAX_DRAWDOWN,
                detail="net P&L breached maximum drawdown",
                observed_value=drawdown,
                limit_value=self.config.max_drawdown,
            )
        return RiskDecision(approved=True, detail="continuous P&L checks passed")

    def activate_kill_switch(
        self,
        *,
        timestamp_ns: int,
        reason: RiskReason = RiskReason.KILL_SWITCH,
        detail: str = "kill switch activated",
        observed_value: Decimal | int | None = None,
        limit_value: Decimal | int | None = None,
    ) -> RiskDecision:
        """Activate the sticky kill switch and request live-order cancellation."""

        if timestamp_ns < 0:
            raise RiskError("timestamp_ns must be nonnegative")
        if not isinstance(reason, RiskReason):
            raise RiskError("reason must be a RiskReason")
        if not self.kill_switch_active:
            self.kill_switch_active = True
            self.kill_reason = reason
            self.events.append(
                RiskEvent(
                    timestamp_ns=timestamp_ns,
                    reason=reason,
                    detail=detail,
                    observed_value=observed_value,
                    limit_value=limit_value,
                )
            )
        return RiskDecision(
            approved=False,
            reason=self.kill_reason,
            detail=detail,
            cancel_open_orders=True,
        )

    def stale_quote_ids(
        self, *, timestamp_ns: int, open_orders: Iterable[OpenOrderExposure]
    ) -> tuple[str, ...]:
        """Return quotes older than the configured maximum age."""

        if timestamp_ns < 0:
            raise RiskError("timestamp_ns must be nonnegative")
        orders = tuple(open_orders)
        live_ids = {order.order_id for order in orders}
        self._reported_stale_orders.intersection_update(live_ids)
        stale: list[str] = []
        for order in orders:
            if not order.is_quote:
                continue
            age = timestamp_ns - order.accepted_timestamp_ns
            if age > self.config.max_quote_age_ns:
                stale.append(order.order_id)
                if order.order_id not in self._reported_stale_orders:
                    self.events.append(
                        RiskEvent(
                            timestamp_ns=timestamp_ns,
                            reason=RiskReason.QUOTE_AGE,
                            detail="quote exceeded maximum age",
                            order_id=order.order_id,
                            observed_value=age,
                            limit_value=self.config.max_quote_age_ns,
                        )
                    )
                    self._reported_stale_orders.add(order.order_id)
        return tuple(stale)

    def session_directive(
        self,
        *,
        timestamp_ns: int,
        inventory: int,
        open_orders: Iterable[OpenOrderExposure],
    ) -> SessionDirective:
        """Describe quote suppression, cancellation, and closing inventory action."""

        if timestamp_ns < 0:
            raise RiskError("timestamp_ns must be nonnegative")
        if self.session_end_ns is None:
            return SessionDirective(active=False)

        cutoff = max(0, self.session_end_ns - self.config.prohibit_new_quotes_last_ns)
        if timestamp_ns < cutoff:
            return SessionDirective(active=False)

        orders = tuple(open_orders)
        if timestamp_ns < self.session_end_ns:
            quote_ids = tuple(order.order_id for order in orders if order.is_quote)
            return SessionDirective(
                active=True,
                block_new_quotes=True,
                cancel_order_ids=quote_ids,
            )

        all_ids = tuple(order.order_id for order in orders)
        if self.session_end_policy is SessionEndPolicy.LIQUIDATE and inventory != 0:
            return SessionDirective(
                active=True,
                block_new_quotes=True,
                cancel_order_ids=all_ids,
                liquidation_side=Side.ASK if inventory > 0 else Side.BID,
                liquidation_quantity=abs(inventory),
            )
        return SessionDirective(
            active=True,
            block_new_quotes=True,
            cancel_order_ids=all_ids,
            mark_remaining_inventory=True,
        )

    def continuous_check(
        self,
        *,
        timestamp_ns: int,
        net_pnl: DecimalLike,
        inventory: int,
        open_orders: Iterable[OpenOrderExposure],
    ) -> ContinuousRiskResult:
        """Run P&L, quote-age, kill, and session checks together."""

        orders = tuple(open_orders)
        self.observe_pnl(net_pnl, timestamp_ns=timestamp_ns)
        stale = set(self.stale_quote_ids(timestamp_ns=timestamp_ns, open_orders=orders))
        session = self.session_directive(
            timestamp_ns=timestamp_ns,
            inventory=inventory,
            open_orders=orders,
        )
        cancel_ids = stale | set(session.cancel_order_ids)
        if self.kill_switch_active:
            cancel_ids.update(order.order_id for order in orders)
        return ContinuousRiskResult(
            kill_switch_active=self.kill_switch_active,
            cancel_order_ids=tuple(sorted(cancel_ids)),
            session=session,
        )

    def register_message(
        self, *, timestamp_ns: int, bypass_limit: bool = False
    ) -> RiskDecision:
        """Register a non-order message, such as a cancellation."""

        if timestamp_ns < 0:
            raise RiskError("timestamp_ns must be nonnegative")
        if not bypass_limit and not self._message_rate_available(timestamp_ns):
            event = RiskEvent(
                timestamp_ns=timestamp_ns,
                reason=RiskReason.MESSAGE_RATE,
                detail="outbound message rate limit reached",
                observed_value=self._current_message_count(timestamp_ns),
                limit_value=self.config.max_messages_per_second,
            )
            self.events.append(event)
            return RiskDecision(
                approved=False,
                reason=RiskReason.MESSAGE_RATE,
                detail=event.detail,
            )
        self._record_message(timestamp_ns)
        return RiskDecision(approved=True, detail="message approved")

    def _validate_request(
        self, request: OrderRiskRequest, context: RiskContext
    ) -> RiskDecision | None:
        if not _is_side(request.side):
            return self._reject(
                request, RiskReason.INVALID_ORDER, "side must be a Side"
            )
        if request.quantity <= 0:
            return self._reject(
                request, RiskReason.INVALID_ORDER, "quantity must be positive"
            )
        if request.timestamp_ns < 0:
            return self._reject(
                request, RiskReason.INVALID_ORDER, "timestamp must be nonnegative"
            )
        if request.price_ticks is not None and request.price_ticks <= 0:
            return self._reject(
                request, RiskReason.INVALID_ORDER, "price must be positive"
            )
        if context.spread_ticks is not None and context.spread_ticks < 0:
            return self._reject(
                request,
                RiskReason.INVALID_ORDER,
                "spread cannot be negative",
            )
        if context.volatility_ticks is not None and context.volatility_ticks < 0:
            return self._reject(
                request,
                RiskReason.INVALID_ORDER,
                "volatility cannot be negative",
            )
        return None

    @staticmethod
    def _is_valid_reduce_only(request: OrderRiskRequest, inventory: int) -> bool:
        if not request.reduce_only or inventory == 0:
            return False
        opposes_inventory = (inventory > 0 and request.side is Side.ASK) or (
            inventory < 0 and request.side is Side.BID
        )
        return opposes_inventory and request.quantity <= abs(inventory)

    def _session_rejection(
        self, request: OrderRiskRequest, risk_reducing: bool
    ) -> RiskDecision | None:
        if (
            self.session_start_ns is not None
            and request.timestamp_ns < self.session_start_ns
            and not risk_reducing
        ):
            return self._reject(
                request,
                RiskReason.SESSION_END,
                "session has not started",
            )
        if self.session_end_ns is None:
            return None
        cutoff = max(0, self.session_end_ns - self.config.prohibit_new_quotes_last_ns)
        if request.timestamp_ns >= self.session_end_ns:
            liquidation_allowed = (
                self.session_end_policy is SessionEndPolicy.LIQUIDATE
                and risk_reducing
                and not request.is_quote
                and not request.may_rest
            )
            if not liquidation_allowed:
                return self._reject(
                    request,
                    RiskReason.SESSION_END,
                    "session has ended",
                )
        elif request.is_quote and request.timestamp_ns >= cutoff:
            return self._reject(
                request,
                RiskReason.SESSION_END,
                "new quotes are prohibited near session end",
            )
        return None

    def _reject(
        self,
        request: OrderRiskRequest,
        reason: RiskReason,
        detail: str,
        observed_value: Decimal | int | None = None,
        limit_value: Decimal | int | None = None,
        *,
        cancel_open_orders: bool = False,
    ) -> RiskDecision:
        self.events.append(
            RiskEvent(
                timestamp_ns=max(0, request.timestamp_ns),
                reason=reason,
                detail=detail,
                order_id=request.order_id,
                observed_value=observed_value,
                limit_value=limit_value,
            )
        )
        return RiskDecision(
            approved=False,
            reason=reason,
            detail=detail,
            cancel_open_orders=cancel_open_orders,
        )

    def _prune_messages(self, timestamp_ns: int) -> None:
        cutoff = timestamp_ns - 1_000_000_000
        while self._message_timestamps and self._message_timestamps[0] <= cutoff:
            self._message_timestamps.popleft()

    def _current_message_count(self, timestamp_ns: int) -> int:
        self._prune_messages(timestamp_ns)
        return len(self._message_timestamps)

    def _message_rate_available(self, timestamp_ns: int) -> bool:
        maximum = self.config.max_messages_per_second
        if maximum is None:
            return True
        return self._current_message_count(timestamp_ns) < maximum

    def _record_message(self, timestamp_ns: int) -> None:
        self._prune_messages(timestamp_ns)
        self._message_timestamps.append(timestamp_ns)
