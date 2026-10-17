"""Separate closed-form public-output review: 3 ahead, 5 own, fills 2+3.

No main study observer, oracle, fixture or arithmetic is imported. The older
snapshot is live with 2 filled; the newer is terminal with all 5 filled. Delays
invert their receipt order without changing either immutable snapshot.
"""

from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig, dump_config
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.reconciliation import ReconciliationPlan
from lobmm.transport import TransportDelay, TransportFault, TransportFaultPlan


@pytest.mark.parametrize("strategy", tuple(StrategyName))
@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize("limit", (None, 1))
@pytest.mark.parametrize("horizon", (119, 120, 220))
def test_public_frozen_reordering_and_late_originals(
    tmp_path: Path, strategy: StrategyName, side: int, limit: int | None, horizon: int
) -> None:
    raw = dump_config(AppConfig())
    raw["latency"].update(
        market_data_ns=0, order_entry_ns=10, cancellation_ns=20, fill_report_ns=0
    )
    raw["strategy"].update(
        name=strategy.value,
        order_size=5,
        inventory_penalty_ticks=0,
        imbalance_coefficient_ticks=0,
        volatility_multiplier=0,
        minimum_quote_lifetime_ns=1000,
        refresh_interval_ns=1000,
        stale_after_ns=1000,
    )
    raw["risk"].update(
        max_abs_inventory=10,
        max_order_size=5,
        max_total_open_quantity=10,
        max_quote_age_ns=1000,
    )
    raw["backtest"].update(
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=30,
        observation_end_timestamp_ns=horizon,
        warmup_events=4,
        timer_interval_ns=1000,
    )
    raw["queue_model"]["price_through_fills"] = False
    raw["output"]["write_plots"] = False
    rows = [
        (0, "ADD", 1, 99, 3),
        (0, "ADD", -1, 101, 3),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (15, "ADD", side, 99 if side == 1 else 101, 8),
        (20, "TRADE", side, 99 if side == 1 else 101, 5),
        (50, "TRADE", side, 99 if side == 1 else 101, 3),
        (200, "ADD", 1, 90, 1),
    ]
    tape = [
        MarketEvent(t, i, EventType(k), Side(s), p, q)
        for i, (t, k, s, p, q) in enumerate(rows, 1)
    ]
    faults = TransportFaultPlan(
        (TransportFault("reconciliation_responses", 0, "duplicate"),),
        (
            TransportDelay("execution_reports", 2, 140),
            TransportDelay("execution_reports", 3, 140),
            TransportDelay("reconciliation_responses", 0, 70),
        ),
    )
    result = run_backtest(
        AppConfig.model_validate(raw),
        tape,
        run_name="complementary",
        output_root=tmp_path,
        reconciliation=ReconciliationPlan(
            (40, 60), response_latency_ns=10, fill_limit_per_order=limit
        ),
        transport_faults=faults,
    )
    recovery = result.diagnostics["reconciliation"]
    stop = result.diagnostics["client_stop"]
    assert len(stop["cancel_requests"]) == 2
    assert stop["unresolved_client_order_ids"] == []
    assert recovery["responses"][0]["as_of_timestamp_ns"] == 40
    old = recovery["responses"][0]["orders"][0 if side == 1 else 1]
    newer = recovery["responses"][1]["orders"][0 if side == 1 else 1]
    assert (old["status"], old["remaining_quantity"]) == ("partially_filled", 3)
    assert (newer["status"], newer["remaining_quantity"]) == ("filled", 0)
    expected_receipts = ["Q0001"] + (["Q0000"] if horizon >= 120 else [])
    assert [r["query_id"] for r in recovery["receipts"]] == expected_receipts
    assert recovery["receipts"][0]["response_timestamp_ns"] == 70
    if horizon >= 120:
        assert recovery["receipts"][1]["recovered_fill_ids"] == []
        assert (
            recovery["receipts"][1]["client_knowledge"]
            == recovery["receipts"][0]["client_knowledge"]
        )
    notified = 5 if limit is None or horizon >= 190 else 2
    assert stop["known_inventory"] == side * notified
    knowledge = stop["client_knowledge"]
    assert knowledge["inventory_min"] == knowledge["inventory_max"] == side * 5
    assert knowledge["complete"] == (notified == 5)
    assert all(o["reservation_quantity"] == 0 for o in knowledge["orders"])
    ledger = recovery["client_execution_ledger"]
    assert len(ledger["fills"]) == (2 if notified == 5 else 1)
    assert ledger["trade_cash_ticks"] == -side * notified * (99 if side == 1 else 101)
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills["quantity"].to_list() == [2, 3]
    assert fills["strategy_notification_timestamp_ns"].to_list() == [
        70,
        70 if limit is None else (190 if horizon >= 190 else None),
    ]
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    assert pnl["inventory"][-1] == side * 5
    assert pnl["trade_cash_ticks"][-1] == -side * 5 * (99 if side == 1 else 101)
    sends = result.diagnostics["transport_faults"]["sends"]
    response_sends = [s for s in sends if s["channel"] == "reconciliation_responses"]
    assert [s["nominal_delivery_timestamp_ns"] for s in response_sends] == [50, 70]
    assert [s["effective_delivery_timestamp_ns"] for s in response_sends] == [120, 70]
    delivered = result.diagnostics["transport_faults"]["deliveries"]
    response_delivered = [
        s for s in delivered if s["channel"] == "reconciliation_responses"
    ]
    assert len(response_delivered) == (3 if horizon >= 120 else 1)
    if horizon >= 120:
        assert response_delivered[-1]["duplicate_ignored"]
    else:
        assert stop["pending_channels"]["reconciliation_responses"] == 2
