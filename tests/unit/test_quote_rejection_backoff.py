from __future__ import annotations

from lobmm.config import StrategyConfig
from lobmm.enums import OrderStatus, ReportType, Side
from lobmm.events import BookView
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest
from lobmm.strategies.base import (
    DesiredQuote,
    MarketMakingStrategy,
)


class ControlledStrategy(MarketMakingStrategy):
    def __init__(
        self,
        config: StrategyConfig,
        targets: tuple[DesiredQuote, ...],
    ) -> None:
        super().__init__(config)
        self.targets = targets

    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        del view, known_inventory, max_abs_inventory
        return self.targets


def _view() -> BookView:
    return BookView(
        timestamp_ns=100,
        sequence_number=1,
        best_bid_ticks=100,
        best_ask_ticks=102,
        best_bid_quantity=10,
        best_ask_quantity=10,
        midpoint_ticks=101.0,
        microprice_ticks=101.0,
        imbalance=0.0,
        bids=((100, 10),),
        asks=((102, 10),),
    )


def _strategy(*targets: DesiredQuote) -> ControlledStrategy:
    strategy = ControlledStrategy(
        StrategyConfig(
            minimum_quote_lifetime_ns=0,
            rejection_cooldown_ns=10,
            rejection_backoff_multiplier=2.0,
            rejection_max_cooldown_ns=100,
            message_rate_cooldown_ns=1_000,
        ),
        targets,
    )
    strategy.on_market_data(_view())
    return strategy


def test_local_risk_rejection_uses_exponential_backoff_and_separate_counts() -> None:
    strategy = _strategy(DesiredQuote(Side.BID, 99, 2))

    first = strategy.decide(timestamp_ns=100, max_abs_inventory=10)
    assert len(first) == 1
    assert isinstance(first[0], NewOrderRequest)
    strategy.record_action_blocked(
        first[0],
        timestamp_ns=100,
        reason="max_order_size",
    )

    assert strategy.active_quotes == ()
    assert strategy.desired_quote_attempts == 1
    assert strategy.risk_blocked_quote_attempts == 1
    assert strategy.rejected_quote_attempts == 1
    assert strategy.submitted_orders == 0
    assert strategy.decide(timestamp_ns=109, max_abs_inventory=10) == ()

    second = strategy.decide(timestamp_ns=110, max_abs_inventory=10)
    assert len(second) == 1
    strategy.record_action_blocked(
        second[0],
        timestamp_ns=110,
        reason="max_order_size",
    )
    assert strategy.decide(timestamp_ns=129, max_abs_inventory=10) == ()

    third = strategy.decide(timestamp_ns=130, max_abs_inventory=10)
    assert len(third) == 1
    strategy.record_action_sent(third[0], timestamp_ns=130)
    strategy.record_action_sent(third[0], timestamp_ns=130)

    assert strategy.desired_quote_attempts == 3
    assert strategy.retry_suppressed_quote_attempts == 2
    assert strategy.risk_blocked_quote_attempts == 2
    assert strategy.rejected_quote_attempts == 2
    assert strategy.submitted_orders == 1


def test_delayed_local_rejection_report_does_not_remove_a_newer_quote() -> None:
    strategy = _strategy(DesiredQuote(Side.BID, 99, 2))
    first = strategy.decide(timestamp_ns=100, max_abs_inventory=10)[0]
    assert isinstance(first, NewOrderRequest)
    strategy.record_action_blocked(
        first,
        timestamp_ns=100,
        reason="max_order_size",
    )

    replacement = strategy.decide(timestamp_ns=110, max_abs_inventory=10)[0]
    strategy.record_action_sent(replacement, timestamp_ns=110)
    strategy.on_execution_report(
        ExecutionReport(
            report_id="local-rejection",
            report_type=ReportType.REJECTED,
            exchange_timestamp_ns=100,
            order_id="",
            client_order_id=first.client_order_id,
            order_status=OrderStatus.REJECTED,
            remaining_quantity=first.quantity,
            reason="max_order_size",
        ),
        notification_timestamp_ns=115,
    )

    assert len(strategy.active_quotes) == 1
    assert strategy.active_quotes[0].client_order_id == replacement.client_order_id
    assert strategy.exchange_rejected_quote_attempts == 0
    assert strategy.rejected_quote_attempts == 1


def test_exchange_rejection_enters_backoff_after_the_delayed_report() -> None:
    strategy = _strategy(DesiredQuote(Side.ASK, 103, 2))
    request = strategy.decide(timestamp_ns=100, max_abs_inventory=10)[0]
    strategy.record_action_sent(request, timestamp_ns=100)
    strategy.on_execution_report(
        ExecutionReport(
            report_id="exchange-rejection",
            report_type=ReportType.REJECTED,
            exchange_timestamp_ns=101,
            order_id="O1",
            client_order_id=request.client_order_id,
            order_status=OrderStatus.REJECTED,
            remaining_quantity=request.quantity,
            reason="post_only_would_cross",
        ),
        notification_timestamp_ns=102,
    )

    assert strategy.exchange_rejected_quote_attempts == 1
    assert strategy.rejected_quote_attempts == 1
    assert strategy.submitted_orders == 1
    assert strategy.active_quotes == ()
    assert strategy.decide(timestamp_ns=111, max_abs_inventory=10) == ()
    assert len(strategy.decide(timestamp_ns=112, max_abs_inventory=10)) == 1


def test_message_rate_block_defers_cancel_and_clears_pending_state() -> None:
    strategy = _strategy(DesiredQuote(Side.BID, 99, 2))
    request = strategy.decide(timestamp_ns=100, max_abs_inventory=10)[0]
    strategy.record_action_sent(request, timestamp_ns=100)
    strategy.on_execution_report(
        ExecutionReport(
            report_id="accepted",
            report_type=ReportType.ACCEPTED,
            exchange_timestamp_ns=100,
            order_id="O1",
            client_order_id=request.client_order_id,
            order_status=OrderStatus.LIVE,
            remaining_quantity=request.quantity,
        ),
        notification_timestamp_ns=100,
    )

    strategy.targets = (DesiredQuote(Side.BID, 98, 2),)
    cancel = strategy.decide(timestamp_ns=110, max_abs_inventory=10)[0]
    assert isinstance(cancel, CancelRequest)
    strategy.record_action_blocked(
        cancel,
        timestamp_ns=110,
        reason="message_rate",
    )

    assert strategy.risk_blocked_cancel_attempts == 1
    assert strategy.cancel_attempts == 1
    assert strategy.cancel_requests == 0
    assert not strategy.active_quotes[0].pending_cancel
    assert strategy.decide(timestamp_ns=1_109, max_abs_inventory=10) == ()

    retry = strategy.decide(timestamp_ns=1_110, max_abs_inventory=10)
    assert len(retry) == 1
    assert isinstance(retry[0], CancelRequest)
    strategy.record_action_sent(retry[0], timestamp_ns=1_110)
    assert strategy.cancel_attempts == 2
    assert strategy.cancel_requests == 1
