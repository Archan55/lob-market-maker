"""Strategy interface and deterministic quote-management state machine."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass

from lobmm.config import StrategyConfig
from lobmm.enums import OrderStatus, ReportType, RiskReason, Side
from lobmm.events import BookView
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest

type StrategyAction = NewOrderRequest | CancelRequest


@dataclass(frozen=True, slots=True)
class DesiredQuote:
    side: Side
    price_ticks: int
    quantity: int

    def __post_init__(self) -> None:
        if self.price_ticks <= 0 or self.quantity <= 0:
            raise ValueError("desired quote price and quantity must be positive")


@dataclass(slots=True)
class ManagedQuote:
    side: Side
    client_order_id: str
    price_ticks: int
    original_quantity: int
    remaining_quantity: int
    sent_timestamp_ns: int
    exchange_order_id: str | None = None
    accepted_timestamp_ns: int | None = None
    pending_cancel: bool = False
    next_cancel_attempt_ns: int = 0
    cancel_rejection_count: int = 0
    dispatch_status: str = "pending"
    cancel_dispatch_status: str | None = None


@dataclass(frozen=True, slots=True)
class RejectionBackoff:
    price_ticks: int
    quantity: int
    reason: str
    consecutive_count: int
    rejection_timestamp_ns: int
    retry_after_ns: int


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    timestamp_ns: int
    observed_sequence: int
    side: Side
    action: str
    client_order_id: str
    order_id: str | None
    price_ticks: int
    quantity: int
    reason: str


_PRICE_INDEPENDENT_REJECTIONS = frozenset(reason.value for reason in RiskReason)


class MarketMakingStrategy(ABC):
    """Abstract strategy receiving only delayed, frozen observations."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.known_inventory = 0
        self.latest_view: BookView | None = None
        self.observed_sequence = -1
        self.report_sequence = -1
        self._next_client_id = 1
        self._managed: dict[Side, ManagedQuote] = {}
        self._rejection_backoff: dict[Side, RejectionBackoff] = {}
        self._global_retry_after_ns = 0
        self._seen_reports: set[str] = set()
        self._locally_blocked_client_ids: set[str] = set()
        self._midpoints: deque[float] = deque(
            maxlen=max(config.volatility_window + 1, 3)
        )
        self.decisions: list[QuoteDecision] = []
        self.desired_quote_attempts = 0
        self.retry_suppressed_quote_attempts = 0
        self.risk_blocked_quote_attempts = 0
        self.exchange_rejected_quote_attempts = 0
        self.rejected_quote_attempts = 0
        self.submitted_orders = 0
        self.cancel_attempts = 0
        self.risk_blocked_cancel_attempts = 0
        self.rejected_cancel_attempts = 0
        self.cancel_requests = 0

    @property
    def active_quotes(self) -> tuple[ManagedQuote, ...]:
        return tuple(self._managed.values())

    @property
    def recent_volatility_ticks(self) -> float:
        if len(self._midpoints) < 3:
            return 0.0
        changes = [
            current - previous
            for previous, current in zip(
                tuple(self._midpoints)[:-1],
                tuple(self._midpoints)[1:],
                strict=True,
            )
        ]
        mean = sum(changes) / len(changes)
        variance = sum((change - mean) ** 2 for change in changes) / len(changes)
        return math.sqrt(variance)

    def on_market_data(self, view: BookView) -> None:
        if view.sequence_number < self.observed_sequence:
            raise ValueError("observed market sequence moved backward")
        self.latest_view = view
        self.observed_sequence = view.sequence_number
        if view.midpoint_ticks is not None:
            self._midpoints.append(view.midpoint_ticks)

    def on_execution_report(
        self, report: ExecutionReport, *, notification_timestamp_ns: int
    ) -> None:
        """Update only strategy-known state when a delayed report arrives."""

        if report.report_id in self._seen_reports:
            return
        if notification_timestamp_ns < report.exchange_timestamp_ns:
            raise ValueError("report notification cannot precede exchange event")
        self._seen_reports.add(report.report_id)
        self.report_sequence += 1

        quote = next(
            (
                managed
                for managed in self._managed.values()
                if managed.client_order_id == report.client_order_id
            ),
            None,
        )
        if report.report_type is ReportType.REJECTED:
            if report.client_order_id in self._locally_blocked_client_ids:
                self._locally_blocked_client_ids.discard(report.client_order_id)
                return
            if quote is not None:
                self.exchange_rejected_quote_attempts += 1
                self._record_quote_rejection(
                    quote,
                    timestamp_ns=notification_timestamp_ns,
                    reason=report.reason or ReportType.REJECTED.value,
                )
        if report.fill is not None:
            self.known_inventory += report.fill.signed_quantity
            if quote is not None:
                quote.remaining_quantity = report.remaining_quantity

        if quote is None:
            return
        if report.report_type is ReportType.ACCEPTED:
            quote.exchange_order_id = report.order_id
            quote.accepted_timestamp_ns = notification_timestamp_ns
            self._rejection_backoff.pop(quote.side, None)
        if report.report_type is ReportType.CANCEL_REJECTED:
            quote.pending_cancel = False
            quote.cancel_dispatch_status = None
            self.rejected_cancel_attempts += 1
            if report.order_status.terminal:
                self._managed.pop(quote.side, None)
            else:
                self._defer_cancel_retry(
                    quote,
                    timestamp_ns=notification_timestamp_ns,
                    reason=report.reason or ReportType.CANCEL_REJECTED.value,
                )
            return
        if (
            report.order_status is not OrderStatus.PARTIALLY_FILLED
            and report.order_status.terminal
        ):
            self._managed.pop(quote.side, None)

    def record_action_sent(self, action: StrategyAction, *, timestamp_ns: int) -> None:
        """Record that a strategy action passed local risk and entered a channel."""

        if timestamp_ns < action.send_timestamp_ns:
            raise ValueError("action cannot be sent before its send timestamp")
        if isinstance(action, NewOrderRequest):
            if action.client_order_id in self._locally_blocked_client_ids:
                raise ValueError("new-order action was already recorded as blocked")
            quote = self._quote_for_client_id(action.client_order_id)
            if quote is None:
                return
            if quote.dispatch_status == "sent":
                return
            quote.dispatch_status = "sent"
            self.submitted_orders += 1
            return

        quote = self._quote_for_cancel(action)
        if quote is None:
            return
        if quote.cancel_dispatch_status == "blocked":
            raise ValueError("cancel action was already recorded as blocked")
        if quote.cancel_dispatch_status == "sent":
            return
        quote.cancel_dispatch_status = "sent"
        self.cancel_requests += 1

    def record_action_blocked(
        self,
        action: StrategyAction,
        *,
        timestamp_ns: int,
        reason: str,
    ) -> None:
        """Apply immediate local-risk feedback without exchange-state leakage."""

        if timestamp_ns < action.send_timestamp_ns:
            raise ValueError("action cannot be blocked before its send timestamp")
        if not reason:
            raise ValueError("blocked action reason must not be empty")

        if isinstance(action, NewOrderRequest):
            if action.client_order_id in self._locally_blocked_client_ids:
                return
            quote = self._quote_for_client_id(action.client_order_id)
            if quote is None:
                return
            if quote.dispatch_status == "sent":
                raise ValueError("new-order action was already recorded as sent")
            quote.dispatch_status = "blocked"
            self._locally_blocked_client_ids.add(action.client_order_id)
            self.risk_blocked_quote_attempts += 1
            self._record_quote_rejection(
                quote,
                timestamp_ns=timestamp_ns,
                reason=reason,
            )
            self._managed.pop(quote.side, None)
            return

        quote = self._quote_for_cancel(action)
        if quote is None or not quote.pending_cancel:
            return
        if quote.cancel_dispatch_status == "sent":
            raise ValueError("cancel action was already recorded as sent")
        if quote.cancel_dispatch_status == "blocked":
            return
        quote.cancel_dispatch_status = "blocked"
        self.risk_blocked_cancel_attempts += 1
        quote.pending_cancel = False
        self._defer_cancel_retry(
            quote,
            timestamp_ns=timestamp_ns,
            reason=reason,
        )

    def decide(
        self,
        *,
        timestamp_ns: int,
        max_abs_inventory: int,
        allow_new_quotes: bool = True,
    ) -> tuple[StrategyAction, ...]:
        """Return idempotent submit/cancel actions for the current observation."""

        if timestamp_ns < 0:
            raise ValueError("timestamp_ns must be nonnegative")
        view = self.latest_view
        if (
            not allow_new_quotes
            or view is None
            or view.best_bid_ticks is None
            or view.best_ask_ticks is None
        ):
            return self.cancel_all(
                timestamp_ns=timestamp_ns, reason="quotes_suppressed"
            )

        desired = {
            quote.side: quote
            for quote in self.desired_quotes(
                view=view,
                known_inventory=self.known_inventory,
                max_abs_inventory=max_abs_inventory,
            )
        }
        actions: list[StrategyAction] = []
        for side in (Side.BID, Side.ASK):
            managed = self._managed.get(side)
            target = desired.get(side)
            if target is None:
                if managed is not None:
                    cancel = self._cancel_if_possible(
                        managed,
                        timestamp_ns=timestamp_ns,
                        reason="risk_side_suppression",
                        ignore_minimum_lifetime=True,
                    )
                    if cancel is not None:
                        actions.append(cancel)
                continue
            if managed is None:
                if self._submission_is_suppressed(target, timestamp_ns):
                    self.retry_suppressed_quote_attempts += 1
                    continue
                actions.append(self._submit(target, timestamp_ns))
                continue
            if managed.pending_cancel:
                continue
            reason = self._replacement_reason(managed, target, timestamp_ns)
            if reason is not None:
                cancel = self._cancel_if_possible(
                    managed, timestamp_ns=timestamp_ns, reason=reason
                )
                if cancel is not None:
                    actions.append(cancel)
        return tuple(actions)

    def cancel_all(
        self, *, timestamp_ns: int, reason: str = "cancel_all"
    ) -> tuple[CancelRequest, ...]:
        actions: list[CancelRequest] = []
        for managed in tuple(self._managed.values()):
            cancel = self._cancel_if_possible(
                managed,
                timestamp_ns=timestamp_ns,
                reason=reason,
                ignore_minimum_lifetime=True,
            )
            if cancel is not None:
                actions.append(cancel)
        return tuple(actions)

    @abstractmethod
    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        """Compute desired passive quotes from permitted information."""

    @staticmethod
    def rounded_quotes(
        reservation_ticks: float, half_spread_ticks: float
    ) -> tuple[int, int]:
        bid = math.floor(reservation_ticks - half_spread_ticks)
        ask = math.ceil(reservation_ticks + half_spread_ticks)
        if bid >= ask:
            ask = bid + 1
        return bid, ask

    @staticmethod
    def inventory_scaled_quantity(
        base_quantity: int, inventory: int, maximum: int
    ) -> int:
        if maximum <= 0:
            return base_quantity
        fraction = min(1.0, abs(inventory) / maximum)
        return max(1, math.floor(base_quantity * (1.0 - 0.75 * fraction)))

    def _submit(self, quote: DesiredQuote, timestamp_ns: int) -> NewOrderRequest:
        client_order_id = f"{self.config.strategy_id}-{self._next_client_id:012d}"
        self._next_client_id += 1
        request = NewOrderRequest(
            client_order_id=client_order_id,
            side=quote.side,
            price_ticks=quote.price_ticks,
            quantity=quote.quantity,
            strategy_id=self.config.strategy_id,
            decision_timestamp_ns=timestamp_ns,
            send_timestamp_ns=timestamp_ns,
            post_only=self.config.post_only,
        )
        self._managed[quote.side] = ManagedQuote(
            side=quote.side,
            client_order_id=client_order_id,
            price_ticks=quote.price_ticks,
            original_quantity=quote.quantity,
            remaining_quantity=quote.quantity,
            sent_timestamp_ns=timestamp_ns,
        )
        self.desired_quote_attempts += 1
        self.decisions.append(
            QuoteDecision(
                timestamp_ns=timestamp_ns,
                observed_sequence=self.observed_sequence,
                side=quote.side,
                action="submit",
                client_order_id=client_order_id,
                order_id=None,
                price_ticks=quote.price_ticks,
                quantity=quote.quantity,
                reason="desired_quote",
            )
        )
        return request

    def _cancel_if_possible(
        self,
        managed: ManagedQuote,
        *,
        timestamp_ns: int,
        reason: str,
        ignore_minimum_lifetime: bool = False,
    ) -> CancelRequest | None:
        if managed.pending_cancel:
            return None
        if timestamp_ns < max(
            managed.next_cancel_attempt_ns, self._global_retry_after_ns
        ):
            return None
        age = timestamp_ns - managed.sent_timestamp_ns
        if not ignore_minimum_lifetime and age < self.config.minimum_quote_lifetime_ns:
            return None
        request = CancelRequest(
            decision_timestamp_ns=timestamp_ns,
            send_timestamp_ns=timestamp_ns,
            order_id=managed.exchange_order_id,
            client_order_id=(
                None if managed.exchange_order_id else managed.client_order_id
            ),
        )
        managed.pending_cancel = True
        managed.cancel_dispatch_status = "pending"
        self.cancel_attempts += 1
        self.decisions.append(
            QuoteDecision(
                timestamp_ns=timestamp_ns,
                observed_sequence=self.observed_sequence,
                side=managed.side,
                action="cancel",
                client_order_id=managed.client_order_id,
                order_id=managed.exchange_order_id,
                price_ticks=managed.price_ticks,
                quantity=managed.remaining_quantity,
                reason=reason,
            )
        )
        return request

    def _submission_is_suppressed(
        self, target: DesiredQuote, timestamp_ns: int
    ) -> bool:
        if timestamp_ns < self._global_retry_after_ns:
            return True
        backoff = self._rejection_backoff.get(target.side)
        if backoff is None:
            return False
        if not self._backoff_applies_to_target(backoff, target):
            base_retry = (
                backoff.rejection_timestamp_ns + self.config.rejection_cooldown_ns
            )
            if timestamp_ns >= base_retry:
                self._rejection_backoff.pop(target.side, None)
                return False
            return True
        return timestamp_ns < backoff.retry_after_ns

    @staticmethod
    def _backoff_applies_to_target(
        backoff: RejectionBackoff, target: DesiredQuote
    ) -> bool:
        if backoff.quantity != target.quantity:
            return False
        if backoff.reason in _PRICE_INDEPENDENT_REJECTIONS:
            return True
        return backoff.price_ticks == target.price_ticks

    def _record_quote_rejection(
        self,
        quote: ManagedQuote,
        *,
        timestamp_ns: int,
        reason: str,
    ) -> None:
        previous = self._rejection_backoff.get(quote.side)
        target = DesiredQuote(
            side=quote.side,
            price_ticks=quote.price_ticks,
            quantity=quote.original_quantity,
        )
        same_rejection = (
            previous is not None
            and previous.reason == reason
            and self._backoff_applies_to_target(previous, target)
        )
        consecutive_count = 1
        if same_rejection:
            assert previous is not None
            consecutive_count = previous.consecutive_count + 1
        retry_after_ns = timestamp_ns + self._rejection_delay_ns(
            consecutive_count=consecutive_count,
            reason=reason,
        )
        self._rejection_backoff[quote.side] = RejectionBackoff(
            price_ticks=quote.price_ticks,
            quantity=quote.original_quantity,
            reason=reason,
            consecutive_count=consecutive_count,
            rejection_timestamp_ns=timestamp_ns,
            retry_after_ns=retry_after_ns,
        )
        if reason == RiskReason.MESSAGE_RATE.value:
            self._global_retry_after_ns = max(
                self._global_retry_after_ns,
                timestamp_ns + self.config.message_rate_cooldown_ns,
            )
        self.rejected_quote_attempts += 1

    def _defer_cancel_retry(
        self,
        quote: ManagedQuote,
        *,
        timestamp_ns: int,
        reason: str,
    ) -> None:
        quote.cancel_rejection_count += 1
        quote.next_cancel_attempt_ns = timestamp_ns + self._rejection_delay_ns(
            consecutive_count=quote.cancel_rejection_count,
            reason=reason,
        )
        if reason == RiskReason.MESSAGE_RATE.value:
            self._global_retry_after_ns = max(
                self._global_retry_after_ns,
                timestamp_ns + self.config.message_rate_cooldown_ns,
            )

    def _rejection_delay_ns(self, *, consecutive_count: int, reason: str) -> int:
        base = self.config.rejection_cooldown_ns
        if reason == RiskReason.MESSAGE_RATE.value:
            base = max(base, self.config.message_rate_cooldown_ns)
        cap = max(base, self.config.rejection_max_cooldown_ns)
        delay = min(base, cap)
        for _ in range(consecutive_count - 1):
            if delay >= cap:
                break
            delay = min(
                cap,
                math.ceil(delay * self.config.rejection_backoff_multiplier),
            )
        return delay

    def _quote_for_cancel(self, request: CancelRequest) -> ManagedQuote | None:
        return next(
            (
                quote
                for quote in self._managed.values()
                if (
                    request.order_id is not None
                    and quote.exchange_order_id == request.order_id
                )
                or (
                    request.client_order_id is not None
                    and quote.client_order_id == request.client_order_id
                )
            ),
            None,
        )

    def _quote_for_client_id(self, client_order_id: str) -> ManagedQuote | None:
        return next(
            (
                quote
                for quote in self._managed.values()
                if quote.client_order_id == client_order_id
            ),
            None,
        )

    def _replacement_reason(
        self, managed: ManagedQuote, target: DesiredQuote, timestamp_ns: int
    ) -> str | None:
        age = timestamp_ns - managed.sent_timestamp_ns
        if age >= self.config.stale_after_ns:
            return "stale"
        if age < self.config.minimum_quote_lifetime_ns:
            return None
        if (
            abs(target.price_ticks - managed.price_ticks)
            >= self.config.price_change_threshold_ticks
            and target.price_ticks != managed.price_ticks
        ):
            return "price_change"
        if (
            abs(target.quantity - managed.remaining_quantity)
            >= self.config.quantity_change_threshold
            and target.quantity != managed.remaining_quantity
        ):
            return "quantity_change"
        if age >= self.config.refresh_interval_ns:
            return "refresh"
        return None
