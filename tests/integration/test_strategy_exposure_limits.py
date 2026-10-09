"""All built-in strategies retain hard capacity through partial fills and cancel lag.

The small independent reservation ledger follows sends, venue arrivals and unit
executions. It never calls the engine's risk projection or reads its pending map.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.channels import ChannelDelivery, OrderedChannel
from lobmm.config import AppConfig
from lobmm.enums import EventType, OrderStatus, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, NewOrderRequest, UnknownOrder
from lobmm.portfolio import Portfolio
from lobmm.risk import RiskManager
from lobmm.scheduler import ScheduledEvent, Scheduler

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("strategy_name", list(StrategyName))
@pytest.mark.parametrize("filled_side", [Side.BID, Side.ASK])
def test_all_strategies_conserve_reserved_capacity_through_cancel_lag(
    strategy_name: StrategyName,
    filled_side: Side,
    tmp_path: Path,
) -> None:
    base = AppConfig()
    config = base.model_copy(
        update={
            "latency": base.latency.model_copy(
                update={
                    "market_data_ns": 100_000,
                    "order_entry_ns": 300_000,
                    "cancellation_ns": 1_000_000,
                    "fill_report_ns": 100_000,
                }
            ),
            "strategy": base.strategy.model_copy(
                update={
                    "name": strategy_name,
                    "order_size": 6,
                    "base_half_spread_ticks": 1.0,
                    "inventory_penalty_ticks": 0.0,
                    "imbalance_coefficient_ticks": 0.0,
                    "volatility_multiplier": 0.0,
                    "minimum_quote_lifetime_ns": 0,
                    "refresh_interval_ns": 100_000_000,
                    "stale_after_ns": 100_000_000,
                    "quantity_change_threshold": 1,
                    "rejection_cooldown_ns": 500_000,
                }
            ),
            "risk": base.risk.model_copy(
                update={
                    "max_abs_inventory": 6,
                    "max_order_size": 6,
                    "max_total_open_quantity": 12,
                    "max_open_orders": 2,
                    "max_quote_age_ns": 100_000_000,
                }
            ),
            "backtest": base.backtest.model_copy(
                update={"warmup_events": 2, "timer_interval_ns": 1_000_000}
            ),
            "output": base.output.model_copy(update={"write_plots": False}),
        }
    )
    fill_price = 99 if filled_side is Side.BID else 101
    raw = [
        (0, EventType.SNAPSHOT, Side.BID, 99, 2),
        (0, EventType.SNAPSHOT, Side.ASK, 101, 2),
        (1_000_000, EventType.ADD, Side.BID, 99, 6),
        (1_000_000, EventType.ADD, Side.ASK, 101, 6),
        (2_000_000, EventType.TRADE, filled_side, fill_price, 6),
        (6_000_000, EventType.ADD, Side.BID, 98, 1),
    ]
    events = [
        MarketEvent(timestamp, sequence, kind, side, price, quantity)
        for sequence, (timestamp, kind, side, price, quantity) in enumerate(raw, 1)
    ]
    instances: dict[str, Any] = {}
    reservations: dict[str, tuple[Side, int]] = {}
    pending: set[str] = set()
    consumed_fill_ids: set[str] = set()
    inventory = 0
    checks = 0
    retained_during_cancel = False
    headroom_rejections = 0
    cancel_sends: set[str] = set()
    original_send = OrderedChannel.send
    original_step = Scheduler.run_one
    original_check = RiskManager.check_order

    def initialize(cls: type[Any], label: str, patch: pytest.MonkeyPatch) -> None:
        original = cls.__init__

        def capture(obj: Any, *args: Any, **kwargs: Any) -> None:
            original(obj, *args, **kwargs)
            instances[label] = obj

        patch.setattr(cls, "__init__", capture)

    def send(channel: Any, request: Any, **kwargs: Any) -> Any:
        nonlocal retained_during_cancel
        delivery = original_send(channel, request, **kwargs)
        if isinstance(request, NewOrderRequest):
            assert request.client_order_id not in reservations
            reservations[request.client_order_id] = (request.side, request.quantity)
            pending.add(request.client_order_id)
        elif isinstance(request, CancelRequest):
            exchange: Exchange = instances["exchange"]
            order = exchange.registry.resolve_cancel(request)
            cancel_sends.add(order.client_order_id)
            if order.is_fillable:
                assert (
                    reservations[order.client_order_id][1] == order.remaining_quantity
                )
                retained_during_cancel |= order.remaining_quantity > 0
            else:
                assert order.client_order_id not in reservations
        return delivery

    def check(manager: RiskManager, **kwargs: Any) -> Any:
        nonlocal headroom_rejections
        # This ledger comes from accepted sends and actual fills, independently
        # of the implementation's RiskContext and projected exposure calculation.
        assert kwargs["inventory"] == inventory
        actual = sorted(
            (int(order.side), order.remaining_quantity)
            for order in kwargs["open_orders"]
        )
        expected = sorted(
            (int(side), quantity) for side, quantity in reservations.values()
        )
        assert actual == expected
        decision = original_check(manager, **kwargs)
        if decision.reason is not None and decision.reason.value == "position_limit":
            headroom_rejections += 1
            assert (
                inventory
                + int(kwargs["side"])
                * (
                    kwargs["quantity"]
                    + sum(
                        quantity
                        for side, quantity in reservations.values()
                        if side is kwargs["side"]
                    )
                )
            ) * int(kwargs["side"]) > 6
        return decision

    def step(scheduler: Scheduler, handler: Callable[..., Any]) -> Any:
        def observe(event: ScheduledEvent[Any], current: Scheduler) -> None:
            nonlocal inventory, checks
            source = event.payload
            if isinstance(source, ChannelDelivery):
                source = source.payload
            if isinstance(source, NewOrderRequest):
                pending.remove(source.client_order_id)
            handler(event, current)
            exchange: Exchange = instances["exchange"]
            for fill in exchange.registry.fills:
                if fill.fill_id in consumed_fill_ids:
                    continue
                consumed_fill_ids.add(fill.fill_id)
                inventory += int(fill.side) * fill.quantity
                side, quantity = reservations[fill.client_order_id]
                remainder = quantity - fill.quantity
                assert remainder >= 0
                if remainder:
                    reservations[fill.client_order_id] = (side, remainder)
                else:
                    reservations.pop(fill.client_order_id)
            if isinstance(source, NewOrderRequest):
                try:
                    order = exchange.registry.get_by_client_id(source.client_order_id)
                except UnknownOrder:
                    # Late session arrivals are rejected before registry creation.
                    assert event.timestamp_ns > 6_000_000
                    reservations.pop(source.client_order_id, None)
                else:
                    if order.status is OrderStatus.REJECTED:
                        reservations.pop(order.client_order_id)
            elif isinstance(source, CancelRequest):
                order = exchange.registry.resolve_cancel(source)
                if order.status is OrderStatus.CANCELLED:
                    reservations.pop(order.client_order_id)
            elif type(source).__name__ == "SessionEnd":
                # Session expiry is an explicit simulated venue-control policy.
                reservations_copy = tuple(reservations)
                for client_order_id in reservations_copy:
                    if client_order_id not in pending:
                        reservations.pop(client_order_id)
            assert instances["portfolio"].inventory == inventory
            live = {
                order.client_order_id: (order.side, order.remaining_quantity)
                for order in exchange.registry.live_orders
            }
            assert {
                cid: r for cid, r in reservations.items() if cid not in pending
            } == live
            reservoir = tuple(reservations.values())
            for quantities in product(
                *(range(quantity + 1) for _, quantity in reservoir)
            ):
                possible = inventory + sum(
                    int(side) * quantity
                    for (side, _), quantity in zip(reservoir, quantities, strict=True)
                )
                assert -6 <= possible <= 6
                checks += 1

        return original_step(scheduler, observe)

    with pytest.MonkeyPatch.context() as patch:
        initialize(Exchange, "exchange", patch)
        initialize(Portfolio, "portfolio", patch)
        patch.setattr(OrderedChannel, "send", send)
        patch.setattr(Scheduler, "run_one", step)
        patch.setattr(RiskManager, "check_order", check)
        result = run_backtest(
            config,
            events,
            run_name=f"{strategy_name.value}-{filled_side.name.lower()}",
            output_root=tmp_path,
        )

    assert retained_during_cancel and cancel_sends
    assert headroom_rejections > 0
    assert checks > 100
    assert not reservations and not pending
    assert result.diagnostics["open_orders_end"] == 0
    assert inventory == int(filled_side) * 4
    fills = pl.read_parquet(result.run_directory / "fills.parquet").to_dicts()
    assert [(f["side"], f["quantity"], f["price_ticks"]) for f in fills] == [
        (int(filled_side), 4, fill_price)
    ], "independent external-ahead budget: six trade units less two ahead"
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet").to_dicts()
    assert pnl[-1]["inventory"] == inventory
    assert pnl[-1]["trade_cash_ticks"] == -inventory * fill_price
    assert pnl[-1]["realized_pnl_ticks"] == 0
    assert pnl[-1]["gross_pnl_ticks"] == -inventory * fill_price + inventory * 100
