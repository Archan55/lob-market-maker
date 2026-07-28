from __future__ import annotations

from decimal import Decimal

from lobmm.config import StrategyConfig
from lobmm.enums import (
    EventType,
    LiquidityRole,
    OrderStatus,
    ReportType,
    Side,
    StrategyName,
)
from lobmm.events import BookView, MarketEvent
from lobmm.orders import ExecutionReport, Fill
from lobmm.strategies import make_strategy
from lobmm.strategies.base import MarketMakingStrategy
from lobmm.strategy_runtime import StrategyRuntime


def view(
    *,
    midpoint: float = 100.5,
    microprice: float = 100.75,
    imbalance: float = 0.5,
    sequence: int = 1,
) -> BookView:
    return BookView(
        timestamp_ns=10,
        sequence_number=sequence,
        best_bid_ticks=100,
        best_ask_ticks=101,
        best_bid_quantity=30,
        best_ask_quantity=10,
        midpoint_ticks=midpoint,
        microprice_ticks=microprice,
        imbalance=imbalance,
        bids=((100, 30),),
        asks=((101, 10),),
    )


def desired(strategy: MarketMakingStrategy, inventory: int = 0) -> dict[Side, int]:
    return {
        quote.side: quote.price_ticks
        for quote in strategy.desired_quotes(
            view=view(), known_inventory=inventory, max_abs_inventory=100
        )
    }


def test_fixed_spread_quotes_around_midpoint() -> None:
    strategy = make_strategy(
        StrategyConfig(name=StrategyName.FIXED_SPREAD, base_half_spread_ticks=1)
    )
    quotes = desired(strategy)
    assert quotes == {Side.BID: 99, Side.ASK: 102}


def test_inventory_aware_lowers_reservation_when_long() -> None:
    strategy = make_strategy(
        StrategyConfig(
            name=StrategyName.INVENTORY_AWARE,
            base_half_spread_ticks=1,
            inventory_penalty_ticks=4,
        )
    )
    flat = desired(strategy)
    long = desired(strategy, inventory=50)
    assert long[Side.BID] < flat[Side.BID]
    assert long[Side.ASK] < flat[Side.ASK]


def test_microprice_uses_only_observed_signal_and_backward_volatility() -> None:
    strategy = make_strategy(
        StrategyConfig(
            name=StrategyName.MICROPRICE,
            imbalance_coefficient_ticks=2,
            volatility_window=3,
        )
    )
    for sequence, midpoint in enumerate((100.0, 101.0, 99.0), 1):
        strategy.on_market_data(
            view(midpoint=midpoint, microprice=midpoint, sequence=sequence)
        )
    assert strategy.recent_volatility_ticks > 0
    quotes = strategy.desired_quotes(
        view=view(sequence=4), known_inventory=0, max_abs_inventory=100
    )
    assert quotes[0].price_ticks < quotes[1].price_ticks


def test_quote_management_avoids_duplicate_new_messages() -> None:
    strategy = make_strategy(
        StrategyConfig(
            minimum_quote_lifetime_ns=0,
            refresh_interval_ns=1_000,
            stale_after_ns=2_000,
        )
    )
    strategy.on_market_data(view())
    first = strategy.decide(timestamp_ns=10, max_abs_inventory=100)
    second = strategy.decide(timestamp_ns=11, max_abs_inventory=100)
    assert len(first) == 2
    assert second == ()


def test_known_inventory_changes_only_on_delayed_fill_report() -> None:
    strategy = make_strategy(StrategyConfig())
    strategy.on_market_data(view())
    actions = strategy.decide(timestamp_ns=10, max_abs_inventory=100)
    buy = next(action for action in actions if action.side is Side.BID)
    fill = Fill(
        fill_id="F1",
        order_id="O1",
        client_order_id=buy.client_order_id,
        strategy_id=buy.strategy_id,
        side=Side.BID,
        quantity=3,
        price_ticks=buy.price_ticks,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=20,
        fee=Decimal("0"),
        rebate=Decimal("0"),
    )
    report = ExecutionReport(
        report_id="R1",
        report_type=ReportType.PARTIAL_FILL,
        exchange_timestamp_ns=20,
        order_id="O1",
        client_order_id=buy.client_order_id,
        order_status=OrderStatus.PARTIALLY_FILLED,
        remaining_quantity=buy.quantity - 3,
        fill=fill,
    )
    assert strategy.known_inventory == 0
    strategy.on_execution_report(report, notification_timestamp_ns=25)
    assert strategy.known_inventory == 3
    strategy.on_execution_report(report, notification_timestamp_ns=25)
    assert strategy.known_inventory == 3


def test_runtime_owns_separate_observed_book() -> None:
    runtime = StrategyRuntime(
        strategy=make_strategy(StrategyConfig()), max_abs_inventory=10
    )
    runtime.deliver_market(
        MarketEvent(
            1,
            1,
            event_type=EventType.ADD,
            side=Side.BID,
            price_ticks=100,
            quantity=10,
        ),
        notification_timestamp_ns=2,
    )
    assert runtime.observed_book.best_bid == 100
    assert not hasattr(runtime, "exchange")
    assert not hasattr(runtime, "true_book")
