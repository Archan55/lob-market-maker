"""Delayed strategy knowledge boundary."""

from __future__ import annotations

from lobmm.book import L2Book
from lobmm.enums import ValidationMode
from lobmm.events import BookView, MarketEvent
from lobmm.orders import ExecutionReport
from lobmm.strategies.base import MarketMakingStrategy, StrategyAction


class StrategyRuntime:
    """Own the observed book and delayed account knowledge.

    This class deliberately has no field referring to the exchange, true book,
    true portfolio, scheduler, or post-run metrics.
    """

    def __init__(
        self,
        *,
        strategy: MarketMakingStrategy,
        max_abs_inventory: int,
        depth: int = 5,
    ) -> None:
        if max_abs_inventory <= 0:
            raise ValueError("max_abs_inventory must be positive")
        if depth <= 0:
            raise ValueError("depth must be positive")
        self.strategy = strategy
        self.max_abs_inventory = max_abs_inventory
        self.depth = depth
        self.observed_book = L2Book(ValidationMode.LENIENT)
        self.market_messages_delivered = 0
        self.reports_delivered = 0
        self._last_market_timestamp_ns = 0
        self._last_sequence = -1

    @property
    def known_inventory(self) -> int:
        return self.strategy.known_inventory

    @property
    def latest_view(self) -> BookView | None:
        return self.strategy.latest_view

    @property
    def observed_sequence(self) -> int:
        return self._last_sequence

    def deliver_market(
        self, event: MarketEvent, *, notification_timestamp_ns: int
    ) -> None:
        if notification_timestamp_ns < event.timestamp_ns:
            raise ValueError("market data cannot arrive before exchange time")
        if event.sequence_number < self._last_sequence:
            raise ValueError("market-data delivery sequence moved backward")
        self.observed_book.apply(event)
        self._last_market_timestamp_ns = notification_timestamp_ns
        self._last_sequence = event.sequence_number
        self.market_messages_delivered += 1
        self.strategy.on_market_data(
            self.observed_book.view(
                notification_timestamp_ns, event.sequence_number, self.depth
            )
        )

    def deliver_report(
        self, report: ExecutionReport, *, notification_timestamp_ns: int
    ) -> None:
        self.strategy.on_execution_report(
            report, notification_timestamp_ns=notification_timestamp_ns
        )
        self.reports_delivered += 1

    def decide(
        self, *, timestamp_ns: int, allow_new_quotes: bool = True
    ) -> tuple[StrategyAction, ...]:
        return self.strategy.decide(
            timestamp_ns=timestamp_ns,
            max_abs_inventory=self.max_abs_inventory,
            allow_new_quotes=allow_new_quotes,
        )

    def record_action_sent(self, action: StrategyAction, *, timestamp_ns: int) -> None:
        """Record an action only after it passes the local outbound gate."""

        self.strategy.record_action_sent(action, timestamp_ns=timestamp_ns)

    def record_action_blocked(
        self,
        action: StrategyAction,
        *,
        timestamp_ns: int,
        reason: str,
    ) -> None:
        """Feed synchronous local-risk feedback into quote management."""

        self.strategy.record_action_blocked(
            action,
            timestamp_ns=timestamp_ns,
            reason=reason,
        )
