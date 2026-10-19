"""Separate public-output checks using a different, fully filled race witness.

Three external units ahead of five own units: trade five fills two at 40;
trade three at the cancellation boundary 70 fills the other three. Query 60
cannot know the second execution; query 80 can. No study observer or oracle is
imported. Extra scenarios isolate travelling entries, expiry and partial takers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig, dump_config
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.orders import NewOrderRequest
from lobmm.reconciliation import ReconciliationJournal, ReconciliationPlan
from lobmm.report import ReportError, compare_runs, human_readable_summary
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import TransportFault, TransportFaultPlan


def config(
    strategy: StrategyName = StrategyName.FIXED_SPREAD,
    *,
    horizon: int = 300,
    report: int = 200,
    entry: int = 20,
    cancel: int = 20,
) -> AppConfig:
    raw = dump_config(AppConfig())
    raw["latency"].update(
        market_data_ns=0,
        order_entry_ns=entry,
        cancellation_ns=cancel,
        fill_report_ns=report,
    )
    raw["strategy"].update(
        name=strategy.value,
        order_size=5,
        inventory_penalty_ticks=0,
        imbalance_coefficient_ticks=0,
        volatility_multiplier=0,
        minimum_quote_lifetime_ns=10000,
        refresh_interval_ns=10000,
        stale_after_ns=10000,
    )
    raw["risk"].update(
        max_abs_inventory=10,
        max_order_size=5,
        max_total_open_quantity=10,
        max_quote_age_ns=10000,
    )
    raw["backtest"].update(
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=50,
        observation_end_timestamp_ns=horizon,
        warmup_events=4,
        timer_interval_ns=10000,
    )
    raw["queue_model"]["price_through_fills"] = False
    raw["output"]["write_plots"] = False
    return AppConfig.model_validate(raw)


def events(side: int) -> list[MarketEvent]:
    rows = [
        (0, "ADD", 1, 99, 3),
        (0, "ADD", -1, 101, 3),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (30, "ADD", 1, 99, 10),
        (30, "ADD", -1, 101, 10),
        (40, "TRADE", side, 99 if side == 1 else 101, 5),
        (70, "TRADE", side, 99 if side == 1 else 101, 3),
        (250, "ADD", 1, 90, 1),
    ]
    return [
        MarketEvent(t, i, EventType(k), Side(s), p, q)
        for i, (t, k, s, p, q) in enumerate(rows, 1)
    ]


@pytest.mark.parametrize("strategy", tuple(StrategyName))
@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize(
    "horizon,known,complete",
    (
        (79, 0, False),
        (80, 2, False),
        (99, 2, False),
        (100, 5, True),
        (220, 5, True),
        (240, 5, True),
        (300, 5, True),
    ),
)
def test_frozen_snapshot_then_late_originals(
    tmp_path: Path,
    strategy: StrategyName,
    side: int,
    horizon: int,
    known: int,
    complete: bool,
) -> None:
    result = run_backtest(
        config(strategy, horizon=horizon),
        events(side),
        run_name="race",
        output_root=tmp_path,
        reconciliation=ReconciliationPlan(
            (60,) if horizon < 80 else (60, 80), response_latency_ns=20
        ),
    )
    stop = result.diagnostics["client_stop"]
    assert result.diagnostics["end_inventory"] == side * 5
    assert stop["known_inventory"] == side * known
    assert stop["client_knowledge_complete"] is complete
    response = result.diagnostics["reconciliation"]["responses"][0]
    assert (
        response["request_timestamp_ns"]
        == response["exchange_snapshot_timestamp_ns"]
        == response["as_of_timestamp_ns"]
        == 60
    )
    target = response["orders"][0 if side == 1 else 1]
    assert [f["quantity"] for f in target["fills"]] == [2]
    assert target["status"] == "partially_filled" and target["remaining_quantity"] == 3
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills["quantity"].to_list() == [2, 3]
    assert fills["strategy_notification_timestamp_ns"].to_list() == [
        80 if horizon >= 80 else None,
        100 if horizon >= 100 else None,
    ]
    assert len(stop["cancel_requests"]) == 2
    ledger = result.diagnostics["reconciliation"]["client_execution_ledger"]
    assert ledger["trade_cash_ticks"] == -side * known * (99 if side == 1 else 101)
    if complete:
        assert len(ledger["fills"]) == 2
        assert stop["unresolved_client_order_ids"] == []


@pytest.mark.parametrize("side", (1, -1))
@pytest.mark.parametrize("query", (69, 70, 71))
def test_query_at_fill_cancel_boundary(tmp_path: Path, side: int, query: int) -> None:
    loss = TransportFaultPlan(
        tuple(TransportFault("execution_reports", i, "drop") for i in range(8))
    )
    result = run_backtest(
        config(),
        events(side),
        run_name="boundary",
        output_root=tmp_path,
        transport_faults=loss,
        reconciliation=ReconciliationPlan((query,)),
    )
    stop = result.diagnostics["client_stop"]
    assert stop["known_inventory"] == side * (2 if query == 69 else 5)
    assert stop["client_knowledge_complete"] is (query >= 70)
    assert (
        pl.read_parquet(result.run_directory / "pnl.parquet")["inventory"][-1]
        == side * 5
    )


@pytest.mark.parametrize("entry_dropped", (False, True))
def test_absence_before_entry_arrival_never_releases_reservation(
    tmp_path: Path, entry_dropped: bool
) -> None:
    rules = [TransportFault("execution_reports", i, "drop") for i in range(8)]
    if entry_dropped:
        rules.extend(TransportFault("new_orders", i, "drop") for i in (0, 1))
    result = run_backtest(
        config(entry=100, cancel=0),
        events(1),
        run_name="travelling",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(tuple(rules)),
        reconciliation=ReconciliationPlan((60,), response_latency_ns=100),
    )
    response = result.diagnostics["reconciliation"]["responses"][0]
    assert all(o["status"] is None and o["fills"] == [] for o in response["orders"])
    knowledge = result.diagnostics["client_stop"]["client_knowledge"]
    assert (knowledge["inventory_min"], knowledge["inventory_max"]) == (-5, 5)
    assert sum(o["reservation_quantity"] for o in knowledge["orders"]) == 10
    assert not knowledge["complete"]
    assert len(result.diagnostics["client_stop"]["cancel_requests"]) == 2


@pytest.mark.parametrize(
    "expiry,horizon,known,complete", ((90, 120, 2, True), (260, 300, 2, False))
)
def test_partial_taker_and_covered_or_uncovered_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    expiry: int,
    horizon: int,
    known: int,
    complete: bool,
) -> None:
    # Inject one explicit strategy command, then exercise the ordinary public
    # backtest. One ask at 101 supplies only two units of the five-unit taker.
    sent = False

    def decide(
        _: Any, *, timestamp_ns: int, allow_new_quotes: bool = True
    ) -> tuple[NewOrderRequest, ...]:
        nonlocal sent
        if sent or not allow_new_quotes:
            return ()
        sent = True
        return (
            NewOrderRequest(
                "taker",
                Side.BID,
                101,
                5,
                "s",
                timestamp_ns,
                timestamp_ns,
                expire_timestamp_ns=expiry,
            ),
        )

    monkeypatch.setattr(StrategyRuntime, "decide", decide)
    rows = [
        (0, 1, 99, 3),
        (0, -1, 101, 2),
        (0, 1, 98, 20),
        (0, -1, 102, 20),
        (250, 1, 90, 1),
    ]
    tape = [
        MarketEvent(t, i, EventType.ADD, Side(s), p, q)
        for i, (t, s, p, q) in enumerate(rows, 1)
    ]
    loss = TransportFaultPlan(
        tuple(
            [TransportFault("cancellations", 0, "drop")]
            + [TransportFault("execution_reports", i, "drop") for i in range(8)]
        )
    )
    result = run_backtest(
        config(horizon=horizon),
        tape,
        run_name="expiry",
        output_root=tmp_path,
        transport_faults=loss,
        reconciliation=ReconciliationPlan((100,)),
    )
    stop = result.diagnostics["client_stop"]
    assert stop["known_inventory"] == known
    assert stop["client_knowledge_complete"] is complete
    assert stop["accounting_complete"] is complete
    assert (
        result.diagnostics["reconciliation"]["client_execution_ledger"][
            "trade_cash_ticks"
        ]
        == -202
    )
    assert pl.read_parquet(result.run_directory / "fills.parquet")[
        "liquidity_role"
    ].to_list() == ["taker"]


def test_input_identity_and_default_preserved(tmp_path: Path) -> None:
    base = run_backtest(
        config(),
        events(1),
        run_name="base",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(),
    )
    recovered = run_backtest(
        config(),
        events(1),
        run_name="query",
        output_root=tmp_path,
        transport_faults=TransportFaultPlan(),
        reconciliation=ReconciliationPlan((80,)),
    )
    for name in (
        "orders",
        "pnl",
        "inventory",
        "quotes",
        "market",
        "risk_events",
        "markouts",
    ):
        assert (base.run_directory / f"{name}.parquet").read_bytes() == (
            recovered.run_directory / f"{name}.parquet"
        ).read_bytes()
    assert "reconciliation" not in base.diagnostics
    assert not (base.run_directory / "reconciliation.json").exists()
    assert "Finite offline reconciliation" in human_readable_summary(recovered.metrics)
    with pytest.raises(ReportError, match="reconciliation plan"):
        compare_runs([base.run_directory, recovered.run_directory])


@pytest.mark.parametrize(
    "changes",
    (
        {"query_timestamps_ns": ()},
        {"query_timestamps_ns": tuple(range(17))},
        {"query_timestamps_ns": [50]},
        {"query_timestamps_ns": (80, 70)},
        {"query_timestamps_ns": (80, 80)},
        {"query_timestamps_ns": (True,)},
        {"query_timestamps_ns": (-1,)},
        {"request_latency_ns": -1},
        {"response_latency_ns": True},
        {"snapshot_lag_ns": -1},
        {"fill_limit_per_order": -1},
        {"include_order_state": 1},
    ),
)
def test_invalid_bounded_plan(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ReconciliationPlan((60,)), **changes)


@pytest.mark.parametrize("queries", ((49,), (301,)))
def test_queries_require_post_stop_horizon(queries: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="queries must lie"):
        run_backtest(
            config(),
            events(1),
            run_name="invalid",
            persist_artifacts=False,
            reconciliation=ReconciliationPlan(queries),
        )


def test_reconciliation_requires_client_stop() -> None:
    with pytest.raises(ValueError, match="reconciliation requires"):
        run_backtest(
            AppConfig(),
            events(1),
            run_name="invalid",
            persist_artifacts=False,
            reconciliation=ReconciliationPlan((50,)),
        )


@pytest.mark.parametrize("field", ("snapshot", "as-of", "fill", "state"))
def test_reject_future_evidence_at_client_boundary(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    original = ReconciliationJournal.snapshot

    def corrupt(journal: Any, request: Any, arrival_ns: int, tape_end_ns: int) -> Any:
        response = original(journal, request, arrival_ns, tape_end_ns)
        if field == "snapshot":
            return replace(response, exchange_snapshot_timestamp_ns=arrival_ns + 1)
        if field == "as-of":
            return replace(response, as_of_timestamp_ns=arrival_ns + 1)
        order = response.orders[0]
        if field == "fill":
            order = replace(
                order,
                fills=(
                    replace(order.fills[0], exchange_fill_timestamp_ns=arrival_ns + 1),
                ),
            )
        else:
            order = replace(order, state_timestamp_ns=arrival_ns + 1)
        return replace(response, orders=(order, *response.orders[1:]))

    monkeypatch.setattr(ReconciliationJournal, "snapshot", corrupt)
    with pytest.raises(ValueError, match=r"noncausal|exceeds"):
        run_backtest(
            config(),
            events(1),
            run_name="corrupt",
            persist_artifacts=False,
            reconciliation=ReconciliationPlan((80,)),
        )
