"""Complementary closed-form checks using public artifacts, without observers.

This suite does not import the stress oracle. Two units stand ahead of four own
units; later trade budgets and cancel timing give direct expected quantities.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig, dump_config
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.orders import NewOrderRequest
from lobmm.report import ReportError, compare_runs, human_readable_summary
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import TransportFault, TransportFaultPlan


def config(
    strategy: StrategyName = StrategyName.FIXED_SPREAD,
    *,
    horizon: int = 1100,
    report: int = 20,
    entry: int = 20,
    cancel: int = 60,
) -> AppConfig:
    raw = dump_config(AppConfig())
    raw["latency"].update(
        market_data_ns=0,
        order_entry_ns=entry,
        cancellation_ns=cancel,
        fill_report_ns=report,
    )
    raw["queue_model"]["price_through_fills"] = False
    raw["strategy"].update(
        name=strategy.value,
        order_size=4,
        inventory_penalty_ticks=0,
        imbalance_coefficient_ticks=0,
        volatility_multiplier=0,
        minimum_quote_lifetime_ns=100000,
        refresh_interval_ns=100000,
        stale_after_ns=100000,
    )
    raw["risk"].update(
        max_abs_inventory=8,
        max_order_size=4,
        max_total_open_quantity=8,
        max_quote_age_ns=100000,
    )
    raw["backtest"].update(
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=100,
        observation_end_timestamp_ns=horizon,
        warmup_events=4,
        timer_interval_ns=100000,
    )
    raw["output"]["write_plots"] = False
    return AppConfig.model_validate(raw)


def tape(side: int = 1, *, trade: int = 160) -> list[MarketEvent]:
    rows = [
        (0, "ADD", 1, 99, 2),
        (0, "ADD", -1, 101, 2),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (50, "ADD", 1, 99, 10),
        (50, "ADD", -1, 101, 10),
        (100, "TRADE", side, 99 if side == 1 else 101, 3),
        (trade, "TRADE", side, 99 if side == 1 else 101, 2),
        (1000, "ADD", 1, 90, 1),
    ]
    return [
        MarketEvent(t, i, EventType(k), Side(s), p, q)
        for i, (t, k, s, p, q) in enumerate(rows, 1)
    ]


@pytest.mark.parametrize("strategy", tuple(StrategyName))
@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize("trade,units", ((159, 3), (160, 3), (161, 1)))
def test_historical_first_cancel_boundary(
    tmp_path: Path, strategy: StrategyName, side: int, trade: int, units: int
) -> None:
    result = run_backtest(
        config(strategy),
        tape(side, trade=trade),
        run_name="boundary",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            (TransportFault("cancellations", 0 if side == 1 else 1, "duplicate"),)
        ),
    )
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills["quantity"].sum() == units
    assert result.diagnostics["end_inventory"] == side * units
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet").to_dicts()[-1]
    assert pnl["trade_cash_ticks"] == -side * units * (99 if side == 1 else 101)


@pytest.mark.parametrize("strategy", tuple(StrategyName))
@pytest.mark.parametrize("side", (1, -1))
def test_all_duplicates_preserve_economics_and_default_artifacts(
    tmp_path: Path, strategy: StrategyName, side: int
) -> None:
    baseline = run_backtest(
        config(strategy), tape(side), run_name="baseline", output_root=tmp_path
    )
    duplicate = run_backtest(
        config(strategy),
        tape(side),
        run_name="duplicate",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            tuple(
                TransportFault(ch, i, "duplicate")
                for ch, count in (
                    ("new_orders", 2),
                    ("cancellations", 2),
                    ("execution_reports", 8),
                )
                for i in range(count)
            )
        ),
    )
    for name in (
        "orders",
        "fills",
        "inventory",
        "pnl",
        "market",
        "quotes",
        "risk_events",
        "markouts",
        "run_config",
    ):
        suffix = ".yaml" if name == "run_config" else ".parquet"
        assert (baseline.run_directory / (name + suffix)).read_bytes() == (
            duplicate.run_directory / (name + suffix)
        ).read_bytes()
    assert "transport_faults" not in baseline.diagnostics
    assert not (baseline.run_directory / "transport_faults.json").exists()
    assert duplicate.diagnostics["client_stop"]["client_knowledge_complete"]
    with pytest.raises(ReportError, match="transport fault plan"):
        compare_runs([baseline.run_directory, duplicate.run_directory])


@pytest.mark.parametrize("horizon", (180, 1100, 10000000))
def test_no_timeout_release_for_dropped_entries(tmp_path: Path, horizon: int) -> None:
    result = run_backtest(
        config(horizon=horizon),
        tape(),
        run_name="drop",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            tuple(TransportFault("new_orders", i, "drop") for i in (0, 1))
        ),
    )
    stop = result.diagnostics["client_stop"]
    assert stop["accounting_complete"] and not stop["client_knowledge_complete"]
    assert (
        len(stop["dropped_entries_at_venue"])
        == len(stop["unresolved_client_order_ids"])
        == 2
    )
    assert stop["reachable_inventory_min"] == stop["reachable_inventory_max"] == 0
    knowledge = stop["client_knowledge"]
    assert (knowledge["inventory_min"], knowledge["inventory_max"]) == (-4, 4)
    assert sum(o["reservation_quantity"] for o in knowledge["orders"]) == 8
    assert all(
        r["type"] == "cancel_rejected" and r["reason"] == "unknown_order"
        for r in stop["execution_reports"]
    )


@pytest.mark.parametrize("side", (1, -1))
def test_terminal_ack_discloses_lost_fill_without_inventing_knowledge(
    tmp_path: Path, side: int
) -> None:
    result = run_backtest(
        config(),
        tape(side),
        run_name="missing",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            (TransportFault("execution_reports", 2, "drop"),)
        ),
    )
    stop = result.diagnostics["client_stop"]
    # One of the two partial fills was not notified; remaining-quantity evidence
    # establishes total inventory, but the strategy still knows only two units.
    assert result.diagnostics["end_inventory"] == side * 3
    assert stop["known_inventory"] == side * 2
    assert stop["unresolved_client_order_ids"] == []
    assert stop["accounting_complete"] and not stop["client_knowledge_complete"]
    assert (
        stop["client_knowledge"]["inventory_min"]
        == stop["client_knowledge"]["inventory_max"]
        == side * 3
    )
    assert (
        sum(o["missing_fill_quantity_min"] for o in stop["client_knowledge"]["orders"])
        == 1
    )
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills["strategy_notification_timestamp_ns"].null_count() == 1
    rendered = human_readable_summary(result.metrics)
    assert "Client execution knowledge complete: False" in rendered
    assert "transport_faults.json" in rendered


@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize("horizon", (159, 160, 179, 180))
def test_finite_horizon_and_cancel_loss(
    tmp_path: Path, side: int, horizon: int
) -> None:
    result = run_backtest(
        config(horizon=horizon),
        tape(side),
        run_name="horizon",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            (TransportFault("cancellations", 0 if side == 1 else 1, "drop"),)
        ),
    )
    expected = 1 if horizon < 160 else 3
    assert result.diagnostics["end_inventory"] == side * expected
    stop = result.diagnostics["client_stop"]
    assert not stop["accounting_complete"] and not stop["client_knowledge_complete"]
    assert len(stop["outstanding_orders"]) == (2 if horizon < 160 else 1)
    assert stop["known_inventory"] == side * (1 if horizon < 180 else 3)


def test_expiry_is_venue_evidence_even_when_cancel_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = StrategyRuntime.decide

    def decide(runtime: StrategyRuntime, **kwargs: object) -> tuple[object, ...]:
        return tuple(
            replace(action, expire_timestamp_ns=170)
            if isinstance(action, NewOrderRequest)
            else action
            for action in original(runtime, **kwargs)
        )

    monkeypatch.setattr(StrategyRuntime, "decide", decide)
    result = run_backtest(
        config(),
        tape(),
        run_name="expiry",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            (TransportFault("cancellations", 0, "drop"),)
        ),
    )
    assert result.diagnostics["end_inventory"] == 3
    assert result.diagnostics["client_stop"]["accounting_complete"]
    assert result.diagnostics["client_stop"]["client_knowledge_complete"]
    assert any(
        r["type"] == "expired"
        for r in result.diagnostics["client_stop"]["execution_reports"]
    )


def test_faults_require_explicit_finite_stop(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite client_stop"):
        run_backtest(
            AppConfig(),
            tape(),
            run_name="invalid",
            output_root=tmp_path,
            transport_faults=TransportFaultPlan(),
        )


@pytest.mark.parametrize(
    "fault",
    (
        ("market_data", 0, "drop"),
        ("new_orders", -1, "drop"),
        ("new_orders", True, "drop"),
        ("new_orders", 0, "retry"),
    ),
)
def test_plan_rejects_invalid_faults(fault: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        TransportFault(*fault)


def test_plan_rejects_conflicting_identity() -> None:
    with pytest.raises(ValueError, match="one action"):
        TransportFaultPlan(
            (
                TransportFault("new_orders", 0, "drop"),
                TransportFault("new_orders", 0, "duplicate"),
            )
        )
    with pytest.raises(ValueError, match="immutable"):
        TransportFaultPlan([])


@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize("action", ("drop", "duplicate"))
@pytest.mark.parametrize("horizon", (219, 220, 300))
def test_partial_taker_after_stop_with_missing_or_duplicate_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    side: int,
    action: str,
    horizon: int,
) -> None:
    original = StrategyRuntime.decide

    def decide(runtime: StrategyRuntime, **kwargs: object) -> tuple[object, ...]:
        return tuple(
            replace(a, price_ticks=101 if side == 1 else 99, post_only=False)
            for a in original(runtime, **kwargs)
            if isinstance(a, NewOrderRequest) and int(a.side) == side
        )

    monkeypatch.setattr(StrategyRuntime, "decide", decide)
    events = [
        *tape(side)[:4],
        MarketEvent(100, 5, EventType.ADD, Side.BID, 90, 1),
        MarketEvent(1000, 6, EventType.ADD, Side.BID, 90, 1),
    ]
    result = run_backtest(
        config(horizon=horizon, entry=200),
        events,
        run_name="late-taker",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            (
                TransportFault("new_orders", 0, "duplicate"),
                TransportFault("execution_reports", 2, action),
            )
        ),
    )
    # Exactly two opposite units are executable at the request's limit. The
    # other two rest after stop until the delivered acceptance causes a retry.
    assert result.diagnostics["end_inventory"] == side * 2
    fills = pl.read_parquet(result.run_directory / "fills.parquet").to_dicts()
    assert len(fills) == 1 and fills[0]["quantity"] == 2
    assert fills[0]["liquidity_role"] == "taker"
    assert fills[0]["exchange_fill_timestamp_ns"] == 200
    stop = result.diagnostics["client_stop"]
    assert stop["known_inventory"] == (
        side * 2 if action == "duplicate" and horizon >= 220 else 0
    )
    assert stop["accounting_complete"] == (horizon >= 280)
    assert stop["client_knowledge_complete"] == (
        action == "duplicate" and horizon >= 300
    )
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet").to_dicts()[-1]
    assert pnl["trade_cash_ticks"] == -side * 2 * (101 if side == 1 else 99)


@pytest.mark.parametrize("side", (1, -1))
def test_zero_latency_duplicate_cancel_stays_in_later_causal_wave(
    tmp_path: Path, side: int
) -> None:
    result = run_backtest(
        config(entry=0, cancel=0, report=0, horizon=100),
        tape(side),
        run_name="zero",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(
            tuple(
                TransportFault(ch, i, "duplicate")
                for ch, count in (
                    ("new_orders", 2),
                    ("cancellations", 2),
                    ("execution_reports", 8),
                )
                for i in range(count)
            )
        ),
    )
    assert result.diagnostics["end_inventory"] == side
    assert result.diagnostics["client_stop"]["accounting_complete"]
    assert result.diagnostics["client_stop"]["client_knowledge_complete"]
