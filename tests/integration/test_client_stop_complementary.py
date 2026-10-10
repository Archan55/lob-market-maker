"""Independent public-output checks for the opt-in client-stop boundary.

The constructed tape has two external units ahead of each four-unit quote,
then six units join behind. Cash and reachable inventory below are derived
directly from fill quantities and outstanding requests, without engine risk,
portfolio, queue or session-study helpers.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import BacktestResult, run_backtest
from lobmm.channels import OrderedChannel
from lobmm.config import AppConfig, dump_config
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.orders import NewOrderRequest
from lobmm.strategies.base import StrategyAction
from lobmm.strategy_runtime import StrategyRuntime

pytestmark = pytest.mark.integration
STRATEGIES = tuple(StrategyName)


def _config(
    strategy: StrategyName,
    *,
    stop: int = 200,
    horizon: int = 1_600,
    entry: int = 1,
    cancel: int = 100,
    report: int = 500,
    market: int = 0,
    message_limit: int | None = None,
    post_only: bool = True,
) -> AppConfig:
    value = dump_config(AppConfig())
    value["instrument"].update(tick_size="0.01")
    value["latency"].update(
        market_data_ns=market,
        order_entry_ns=entry,
        cancellation_ns=cancel,
        fill_report_ns=report,
    )
    value["queue_model"]["price_through_fills"] = False
    value["fees"].update(
        maker_fee_per_unit="0.002",
        maker_rebate_per_unit="0.0005",
        taker_fee_per_unit="0.003",
        proportional_fee_rate="0.0001",
    )
    value["strategy"].update(
        name=strategy.value,
        strategy_id="complementary-stop",
        order_size=4,
        inventory_penalty_ticks=0.0,
        imbalance_coefficient_ticks=0.0,
        volatility_multiplier=0.0,
        minimum_quote_lifetime_ns=1_000_000,
        refresh_interval_ns=1_000_000,
        stale_after_ns=1_000_000,
        quantity_change_threshold=10,
        post_only=post_only,
    )
    value["risk"].update(
        max_abs_inventory=8,
        max_order_size=4,
        max_total_open_quantity=8,
        max_open_orders=2,
        max_quote_age_ns=1_000_000,
        max_messages_per_second=message_limit,
    )
    value["backtest"].update(
        warmup_events=2,
        timer_interval_ns=100,
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=stop,
        observation_end_timestamp_ns=horizon,
        session_end_policy="mark",
    )
    value["output"]["write_plots"] = False
    return AppConfig.model_validate(value)


def _tape(
    extra: tuple[tuple[int, EventType, Side, int, int], ...] = (),
) -> list[MarketEvent]:
    rows = [
        (0, EventType.SNAPSHOT, Side.BID, 99, 2),
        (0, EventType.SNAPSHOT, Side.ASK, 101, 2),
        (0, EventType.SNAPSHOT, Side.BID, 97, 20),
        (0, EventType.SNAPSHOT, Side.ASK, 103, 20),
        (100, EventType.ADD, Side.BID, 99, 6),
        (100, EventType.ADD, Side.ASK, 101, 6),
        *extra,
        (1_000, EventType.ADD, Side.BID, 94, 1),
    ]
    return [
        MarketEvent(timestamp, sequence, kind, side, price, quantity)
        for sequence, (timestamp, kind, side, price, quantity) in enumerate(
            sorted(rows, key=lambda row: row[0]), 1
        )
    ]


def _run(
    tmp_path: Path,
    strategy: StrategyName,
    *,
    extra: tuple[tuple[int, EventType, Side, int, int], ...] = (),
    **kwargs: Any,
) -> BacktestResult:
    return run_backtest(
        _config(strategy, **kwargs),
        _tape(extra),
        run_name=strategy.value,
        output_root=tmp_path,
        generate_plots=False,
    )


def _table(result: BacktestResult, name: str) -> pl.DataFrame:
    return pl.read_parquet(result.run_directory / f"{name}.parquet")


def _reachable_oracle(result: BacktestResult) -> tuple[int, int]:
    """Exhaust every hypothetical remaining partial-fill combination."""

    diagnostic = result.diagnostics["client_stop"]
    exposures = [
        (int(order["side"]), int(order["remaining_quantity"]))
        for order in diagnostic["outstanding_orders"]
    ] + [
        (int(request["side"]), int(request["quantity"]))
        for request in diagnostic["in_flight_entries"]
    ]
    inventory = int(result.diagnostics["end_inventory"])
    reachable = [
        inventory
        + sum(side * fill for (side, _), fill in zip(exposures, fills, strict=True))
        for fills in itertools.product(
            *(range(quantity + 1) for _, quantity in exposures)
        )
    ]
    expected = min(reachable), max(reachable)
    assert (
        diagnostic["reachable_inventory_min"],
        diagnostic["reachable_inventory_max"],
    ) == expected
    return expected


def _cash_oracle(result: BacktestResult, *, mark: int = 100) -> None:
    """Signed cash plus marked inventory, with literal configured costs."""

    inventory = 0
    trade_cash = Decimal(0)
    fees = Decimal(0)
    rebates = Decimal(0)
    for fill in _table(result, "fills").iter_rows(named=True):
        quantity = int(fill["quantity"])
        signed = int(fill["side"]) * quantity
        notional = Decimal(quantity * int(fill["price_ticks"])) * Decimal("0.01")
        inventory += signed
        trade_cash -= Decimal(signed * int(fill["price_ticks"])) * Decimal("0.01")
        if fill["liquidity_role"] == "maker":
            per_unit_fee = Decimal("0.002")
            rebates += Decimal(quantity) * Decimal("0.0005")
        else:
            assert fill["liquidity_role"] == "taker"
            per_unit_fee = Decimal("0.003")
        fees += Decimal(quantity) * per_unit_fee + notional * Decimal("0.0001")
    cash = trade_cash - fees + rebates
    final = _table(result, "pnl").row(-1, named=True)
    assert final["inventory"] == inventory
    assert final["trade_cash"] == pytest.approx(float(trade_cash), abs=1e-12)
    assert final["fees"] == pytest.approx(float(fees), abs=1e-12)
    assert final["rebates"] == pytest.approx(float(rebates), abs=1e-12)
    assert final["cash"] == pytest.approx(float(cash), abs=1e-12)
    assert final["net_pnl"] == pytest.approx(
        float(cash + Decimal(inventory * mark) * Decimal("0.01")), abs=1e-12
    )


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_market_at_stop_and_cancel_arrival_can_fill_before_shutdown_cancel(
    tmp_path: Path, strategy: StrategyName, side: Side
) -> None:
    price = 99 if side is Side.BID else 101
    result = _run(
        tmp_path,
        strategy,
        extra=(
            (200, EventType.TRADE, side, price, 4),
            (300, EventType.TRADE, side, price, 1),
            (301, EventType.TRADE, side, price, 1),
        ),
    )
    fills = _table(result, "fills")
    # Two units of initial external priority, then two + one own units.
    assert fills["quantity"].to_list() == [2, 1]
    assert fills["exchange_fill_timestamp_ns"].to_list() == [200, 300]
    assert fills["strategy_notification_timestamp_ns"].to_list() == [700, 800]
    assert result.diagnostics["end_inventory"] == int(side) * 3
    assert result.diagnostics["client_stop"]["known_inventory"] == int(side) * 3
    cancelled = _table(result, "orders").filter(pl.col("new_status") == "cancelled")
    assert cancelled["timestamp_ns"].to_list() == [300, 300]
    assert "expired" not in _table(result, "orders")["new_status"].to_list()
    assert _table(result, "quotes")["timestamp_ns"].max() < 200
    assert _reachable_oracle(result) == (int(side) * 3, int(side) * 3)
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
@pytest.mark.parametrize("horizon", (899, 1_600))
def test_inflight_entry_after_stop_can_take_depth_then_cancel_its_partial_remainder(
    tmp_path: Path, strategy: StrategyName, side: Side, horizon: int
) -> None:
    limit = 99 if side is Side.BID else 101
    result = _run(
        tmp_path,
        strategy,
        stop=100,
        horizon=horizon,
        entry=400,
        cancel=50,
        post_only=False,
        extra=(
            # Remove both inner levels before adding the new opposing quote:
            # the external book stays uncrossed during the constructed shift.
            (200, EventType.CANCEL, Side.BID, 99, 8),
            (200, EventType.CANCEL, Side.ASK, 101, 8),
            (200, EventType.ADD, side.opposite, limit, 2),
        ),
    )
    diagnostic = result.diagnostics["client_stop"]
    fills = _table(result, "fills")
    # At entry the only executable external depth within this limit is two.
    assert fills["quantity"].to_list() == [2]
    assert fills["side"].to_list() == [int(side)]
    assert fills["price_ticks"].to_list() == [limit]
    assert fills["liquidity_role"].to_list() == ["taker"]
    assert fills["exchange_fill_timestamp_ns"].to_list() == [400]
    assert fills["send_timestamp_ns"].to_list() == [0]
    assert fills["exchange_arrival_timestamp_ns"].to_list() == [400]
    assert result.diagnostics["end_inventory"] == int(side) * 2
    assert diagnostic["known_inventory"] == (0 if horizon == 899 else int(side) * 2)
    assert fills["strategy_notification_timestamp_ns"].to_list() == (
        [None] if horizon == 899 else [900]
    )

    entry_snapshot = _table(result, "pnl").filter(pl.col("timestamp_ns") == 400)
    assert entry_snapshot.height == 1
    row = entry_snapshot.row(0, named=True)
    entry_notional = Decimal(2 * limit) * Decimal("0.01")
    entry_fee = Decimal("0.006") + entry_notional * Decimal("0.0001")
    assert row["inventory"] == int(side) * 2
    assert row["fees"] == pytest.approx(float(entry_fee), abs=1e-12)
    assert row["rebates"] == 0
    assert row["cash"] == pytest.approx(
        float(-Decimal(int(side)) * entry_notional - entry_fee), abs=1e-12
    )

    if horizon == 899:
        assert sorted(
            (order["side"], order["remaining_quantity"])
            for order in diagnostic["outstanding_orders"]
        ) == ([(-1, 4), (1, 2)] if side is Side.BID else [(-1, 2), (1, 4)])
        assert _reachable_oracle(result) == ((-2, 4) if side is Side.BID else (-4, 2))
        assert len(diagnostic["cancel_requests"]) == 2
    else:
        cancelled = _table(result, "orders").filter(pl.col("new_status") == "cancelled")
        assert cancelled["timestamp_ns"].to_list() == [950, 950]
        assert diagnostic["outstanding_orders"] == []
        assert diagnostic["unresolved_client_order_ids"] == []
        assert [
            item["send_timestamp_ns"] for item in diagnostic["cancel_requests"]
        ] == [
            100,
            100,
            900,
            900,
        ]
        assert _reachable_oracle(result) == (int(side) * 2, int(side) * 2)
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_short_horizon_preserves_partial_order_and_unreported_true_inventory(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(
        tmp_path,
        strategy,
        horizon=250,
        extra=((200, EventType.TRADE, Side.BID, 99, 4),),
    )
    diagnostic = result.diagnostics["client_stop"]
    assert result.diagnostics["end_inventory"] == 2
    assert diagnostic["known_inventory"] == 0
    assert sorted(
        (order["side"], order["remaining_quantity"])
        for order in diagnostic["outstanding_orders"]
    ) == [(-1, 4), (1, 2)]
    assert diagnostic["in_flight_entries"] == []
    assert diagnostic["pending_channels"]["cancellations"] == 2
    assert diagnostic["pending_channels"]["execution_reports"] == 3
    assert _table(result, "fills")["strategy_notification_timestamp_ns"].to_list() == [
        None
    ]
    assert _table(result, "market")["timestamp_ns"].max() == 200
    assert result.diagnostics["events_processed"] == _table(result, "market").height
    assert diagnostic["tape_end_timestamp_ns"] == 1_000
    assert _table(result, "pnl")["timestamp_ns"][-1] == 250
    assert _reachable_oracle(result) == (-2, 4)
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_unknown_cancel_of_inflight_entry_retries_after_delayed_acceptance(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=100, entry=400, cancel=50)
    diagnostic = result.diagnostics["client_stop"]
    reports = diagnostic["execution_reports"]
    unknown = [report for report in reports if report["reason"] == "unknown_order"]
    assert len(unknown) == 2
    assert {report["exchange_timestamp_ns"] for report in unknown} == {150}
    assert {report["notification_timestamp_ns"] for report in unknown} == {650}
    assert [item["send_timestamp_ns"] for item in diagnostic["cancel_requests"]] == [
        100,
        100,
        900,
        900,
    ]
    orders = _table(result, "orders")
    assert orders.filter(pl.col("new_status") == "live")["timestamp_ns"].to_list() == [
        400,
        400,
    ]
    assert orders.filter(pl.col("new_status") == "cancelled")[
        "timestamp_ns"
    ].to_list() == [
        950,
        950,
    ]
    assert diagnostic["outstanding_orders"] == []
    assert diagnostic["in_flight_entries"] == []
    assert diagnostic["known_inventory"] == 0
    assert _table(result, "quotes")["timestamp_ns"].max() < 100
    assert _reachable_oracle(result) == (0, 0)
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_acceptance_not_yet_known_leaves_live_entries_reserved_at_horizon(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=100, horizon=800, entry=400, cancel=50)
    diagnostic = result.diagnostics["client_stop"]
    assert len(diagnostic["outstanding_orders"]) == 2
    assert diagnostic["in_flight_entries"] == []
    assert len(diagnostic["unresolved_client_order_ids"]) == 2
    assert len(diagnostic["cancel_requests"]) == 2
    assert diagnostic["pending_channels"]["execution_reports"] == 2
    assert (
        _table(result, "orders").filter(pl.col("new_status") == "cancelled").is_empty()
    )
    assert _reachable_oracle(result) == (-4, 4)
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("horizon", (100, 149, 150))
def test_finite_horizon_retains_entries_not_yet_arrived_at_venue(
    tmp_path: Path, strategy: StrategyName, horizon: int
) -> None:
    result = _run(
        tmp_path,
        strategy,
        stop=100,
        horizon=horizon,
        entry=400,
        cancel=50,
        report=0,
    )
    diagnostic = result.diagnostics["client_stop"]
    assert _table(result, "orders").is_empty()
    assert diagnostic["outstanding_orders"] == []
    assert len(diagnostic["in_flight_entries"]) == 2
    assert len(diagnostic["unresolved_client_order_ids"]) == 2
    assert diagnostic["pending_channels"]["new_orders"] == 2
    assert all(
        not request["outcome_unobserved_after_tape"]
        for request in diagnostic["in_flight_entries"]
    )
    unknown = [
        report
        for report in diagnostic["execution_reports"]
        if report["reason"] == "unknown_order"
    ]
    # The observation horizon is inclusive, including zero-delay reports.
    assert len(unknown) == (2 if horizon == 150 else 0)
    assert diagnostic["pending_channels"].get("cancellations", 0) == (
        0 if horizon == 150 else 2
    )
    assert _reachable_oracle(result) == (-4, 4)
    assert _table(result, "pnl")["timestamp_ns"][-1] == horizon


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("horizon", (949, 950, 1_449, 1_450))
def test_cancel_arrival_and_ack_are_separate_inclusive_horizon_boundaries(
    tmp_path: Path, strategy: StrategyName, horizon: int
) -> None:
    result = _run(tmp_path, strategy, stop=100, horizon=horizon, entry=400, cancel=50)
    diagnostic = result.diagnostics["client_stop"]
    cancelled = horizon >= 950
    acknowledged = horizon >= 1_450
    assert len(diagnostic["outstanding_orders"]) == (0 if cancelled else 2)
    assert len(diagnostic["unresolved_client_order_ids"]) == (0 if acknowledged else 2)
    assert diagnostic["accounting_complete"] is cancelled
    assert diagnostic["client_knowledge_complete"] is acknowledged
    reports = [
        report
        for report in diagnostic["execution_reports"]
        if report["type"] == "cancelled"
    ]
    assert len(reports) == (2 if acknowledged else 0)
    assert _reachable_oracle(result) == ((0, 0) if cancelled else (-4, 4))
    assert _table(result, "pnl")["timestamp_ns"][-1] == horizon


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_shutdown_cancels_bypass_exhausted_strategy_message_rate(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, message_limit=2)
    assert result.diagnostics["submitted_order_messages"] == 2
    diagnostic = result.diagnostics["client_stop"]
    assert len(diagnostic["cancel_requests"]) == 2
    assert {item["send_timestamp_ns"] for item in diagnostic["cancel_requests"]} == {
        200
    }
    assert (
        _table(result, "orders").filter(pl.col("new_status") == "cancelled").height == 2
    )
    assert diagnostic["outstanding_orders"] == []
    assert _reachable_oracle(result) == (0, 0)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_entry_after_tape_is_unresolved_and_never_matched_against_stale_depth(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=1_000, horizon=2_000, entry=1_001, cancel=1)
    diagnostic = result.diagnostics["client_stop"]
    assert _table(result, "orders").is_empty()
    assert _table(result, "fills").is_empty()
    assert diagnostic["outstanding_orders"] == []
    assert len(diagnostic["in_flight_entries"]) == 2
    assert len(diagnostic["unresolved_client_order_ids"]) == 2
    assert {
        request["scheduled_arrival_timestamp_ns"]
        for request in diagnostic["in_flight_entries"]
    } == {1_001}
    assert all(
        request["outcome_unobserved_after_tape"]
        for request in diagnostic["in_flight_entries"]
    )
    assert _table(result, "market")["timestamp_ns"].max() == 1_000
    assert result.diagnostics["end_inventory"] == diagnostic["known_inventory"] == 0
    assert _reachable_oracle(result) == (-4, 4)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_entry_exactly_at_tape_end_is_accepted_then_cancelled_without_expiry(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=1_000, entry=1_000, cancel=0)
    orders = _table(result, "orders")
    assert orders.filter(pl.col("new_status") == "live").height == 2
    cancelled = orders.filter(pl.col("new_status") == "cancelled")
    assert cancelled["timestamp_ns"].to_list() == [1_000, 1_000]
    assert "expired" not in orders["new_status"].to_list()
    assert result.diagnostics["client_stop"]["in_flight_entries"] == []
    assert _reachable_oracle(result) == (0, 0)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_existing_order_cancel_after_tape_cannot_certify_missing_fills(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=900, cancel=200, report=400)
    diagnostic = result.diagnostics["client_stop"]
    cancelled = _table(result, "orders").filter(pl.col("new_status") == "cancelled")
    assert cancelled.is_empty()
    reports = [
        report
        for report in diagnostic["execution_reports"]
        if report["type"] == "cancelled"
    ]
    assert reports == []
    assert len(diagnostic["outstanding_orders"]) == 2
    assert len(diagnostic["unobserved_cancel_arrivals"]) == 2
    assert diagnostic["accounting_complete"] is False
    assert diagnostic["client_knowledge_complete"] is False
    assert diagnostic["in_flight_entries"] == []
    assert _table(result, "market")["timestamp_ns"].max() == 1_000
    assert _table(result, "pnl")["timestamp_ns"][-1] == 1_600
    assert _reachable_oracle(result) == (-4, 4)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_extended_tape_reveals_fill_before_previously_unobserved_cancel(
    tmp_path: Path, strategy: StrategyName
) -> None:
    config = _config(strategy, stop=900, cancel=200, report=400)
    prefix = _tape()
    short = run_backtest(config, prefix, run_name="short", output_root=tmp_path)
    extended = [
        *prefix,
        MarketEvent(1099, len(prefix) + 1, EventType.TRADE, Side.BID, 99, 3),
        MarketEvent(1200, len(prefix) + 2, EventType.ADD, Side.BID, 94, 1),
    ]
    full = run_backtest(config, extended, run_name="covered", output_root=tmp_path)
    assert short.diagnostics["end_inventory"] == 0
    assert len(short.diagnostics["client_stop"]["outstanding_orders"]) == 2
    assert _reachable_oracle(short) == (-4, 4)
    assert (
        full.diagnostics["end_inventory"]
        == full.diagnostics["client_stop"]["known_inventory"]
        == 1
    )
    assert full.diagnostics["client_stop"]["outstanding_orders"] == []
    assert _reachable_oracle(full) == (1, 1)
    assert _table(full, "pnl")["trade_cash_ticks"][-1] == -99
    assert _table(full, "fills")["exchange_fill_timestamp_ns"].to_list() == [1099]
    assert _table(full, "orders").filter(pl.col("new_status") == "cancelled")[
        "timestamp_ns"
    ].to_list() == [1100, 1100]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_post_tape_expiry_cannot_certify_missing_fills(
    tmp_path: Path, strategy: StrategyName, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from lobmm.channels import OrderedChannel
    from lobmm.orders import NewOrderRequest

    original = OrderedChannel.send

    def send(channel: Any, request: Any, **kwargs: Any) -> Any:
        if isinstance(request, NewOrderRequest):
            request = replace(request, expire_timestamp_ns=1100)
        return original(channel, request, **kwargs)

    monkeypatch.setattr(OrderedChannel, "send", send)
    result = _run(tmp_path, strategy, stop=900, cancel=200, report=400)
    assert (
        _table(result, "orders")
        .filter(pl.col("new_status").is_in(["expired", "cancelled"]))
        .is_empty()
    )
    assert len(result.diagnostics["client_stop"]["unobserved_expiries"]) == 2
    assert _reachable_oracle(result) == (-4, 4)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_late_market_and_reports_continue_without_any_post_stop_decision(
    tmp_path: Path, strategy: StrategyName, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = StrategyRuntime.decide
    decisions: list[int] = []

    def observe(
        runtime: StrategyRuntime,
        *,
        timestamp_ns: int,
        allow_new_quotes: bool = True,
    ) -> tuple[StrategyAction, ...]:
        decisions.append(timestamp_ns)
        assert timestamp_ns < 200
        return original(
            runtime, timestamp_ns=timestamp_ns, allow_new_quotes=allow_new_quotes
        )

    monkeypatch.setattr(StrategyRuntime, "decide", observe)
    result = _run(tmp_path, strategy, market=100)
    diagnostic = result.diagnostics["client_stop"]
    assert decisions
    assert result.diagnostics["market_data_messages_delivered"] == len(_tape())
    assert result.diagnostics["execution_reports_delivered"] == 4
    assert _table(result, "orders")["send_timestamp_ns"].unique().to_list() == [100]
    assert _table(result, "market")["timestamp_ns"].max() == 1_000
    assert diagnostic["mark_source_timestamp_ns"] == 1_000
    assert diagnostic["mark_age_ns"] == 600
    assert _reachable_oracle(result) == (0, 0)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("horizon", (499, 500, 550))
def test_explicit_order_expiry_keeps_its_own_time_and_delayed_ack_after_client_stop(
    tmp_path: Path,
    strategy: StrategyName,
    horizon: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = OrderedChannel.send

    def expiring_entry(channel: Any, payload: Any, **kwargs: Any) -> Any:
        # These strategies have no TIF setting. Amend only the dispatched
        # immutable request to exercise the existing explicit expiry contract.
        if isinstance(payload, NewOrderRequest):
            payload = replace(payload, expire_timestamp_ns=500)
        return original(channel, payload, **kwargs)

    monkeypatch.setattr(OrderedChannel, "send", expiring_entry)
    result = _run(tmp_path, strategy, horizon=horizon, cancel=400, report=50)
    diagnostic = result.diagnostics["client_stop"]
    orders = _table(result, "orders")
    expired = orders.filter(pl.col("new_status") == "expired")
    assert orders.filter(pl.col("new_status") == "cancelled").is_empty()
    assert expired["timestamp_ns"].to_list() == ([] if horizon == 499 else [500, 500])
    assert len(diagnostic["outstanding_orders"]) == (2 if horizon == 499 else 0)
    assert diagnostic["pending_channels"]["cancellations"] == 2
    assert len(diagnostic["cancel_requests"]) == 2
    assert {item["send_timestamp_ns"] for item in diagnostic["cancel_requests"]} == {
        200
    }
    assert diagnostic["client_knowledge_complete"] is (horizon == 550)
    expiry_reports = [
        report
        for report in diagnostic["execution_reports"]
        if report["type"] == "expired"
    ]
    assert len(expiry_reports) == (2 if horizon == 550 else 0)
    assert {report["exchange_timestamp_ns"] for report in expiry_reports} == (
        {500} if horizon == 550 else set()
    )
    assert {report["notification_timestamp_ns"] for report in expiry_reports} == (
        {550} if horizon == 550 else set()
    )
    assert _reachable_oracle(result) == ((-4, 4) if horizon == 499 else (0, 0))
    assert _table(result, "fills").is_empty()
    _cash_oracle(result)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_stop_on_initial_market_timestamp_suppresses_every_decision_and_has_no_residue(
    tmp_path: Path, strategy: StrategyName
) -> None:
    result = _run(tmp_path, strategy, stop=0, horizon=2_000)
    diagnostic = result.diagnostics["client_stop"]
    assert _table(result, "quotes").is_empty()
    assert _table(result, "orders").is_empty()
    assert _table(result, "fills").is_empty()
    assert diagnostic["outstanding_orders"] == []
    assert diagnostic["in_flight_entries"] == []
    assert diagnostic["cancel_requests"] == []
    assert diagnostic["unresolved_client_order_ids"] == []
    assert all(count == 0 for count in diagnostic["pending_channels"].values())
    assert result.diagnostics["end_inventory"] == diagnostic["known_inventory"] == 0
    assert _table(result, "pnl")["timestamp_ns"][-1] == 2_000
    assert _reachable_oracle(result) == (0, 0)
    _cash_oracle(result)
