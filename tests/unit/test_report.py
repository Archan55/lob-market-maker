from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import polars as pl
import pytest

from lobmm.report import (
    REQUIRED_PLOT_FILENAMES,
    ReportError,
    compare_runs,
    generate_report,
    human_readable_summary,
    load_run_artifacts,
)


def _metrics(net_pnl: float, *, fill_events: int = 2) -> dict[str, object]:
    return {
        "research_disclaimer": (
            "Synthetic demonstration results are not evidence of profitability."
        ),
        "trading_activity": {
            "fill_events": fill_events,
            "fill_rate": 0.5,
            "turnover": 2_000.0,
        },
        "pnl": {
            "gross_pnl": net_pnl + 0.2,
            "fees": 0.3,
            "rebates": 0.1,
            "net_pnl": net_pnl,
            "maximum_drawdown": 0.4,
        },
        "inventory": {"end_of_session": 1},
        "execution_quality": {"realized_spread_ticks": 0.25},
        "engineering": {"events_processed": 100},
    }


def _write_run(
    parent: Path,
    name: str,
    *,
    strategy: str,
    net_pnl: float,
    tables: bool = True,
) -> Path:
    run = parent / name
    run.mkdir()
    (run / "metrics.json").write_text(
        json.dumps(_metrics(net_pnl)),
        encoding="utf-8",
    )
    (run / "summary.json").write_text(
        json.dumps(
            {
                "run_name": name,
                "strategy": strategy,
                "symbol": "TEST",
                "net_pnl": net_pnl,
                "event_count": 100,
                "event_stream_sha256": "a" * 64,
                "dataset_provenance": {
                    "provider": "fixture",
                    "dataset_id": "fixture-stream",
                },
            }
        ),
        encoding="utf-8",
    )
    (run / "diagnostics.json").write_text(
        json.dumps({"synthetic_demonstration": True}),
        encoding="utf-8",
    )
    (run / "run_config.yaml").write_text(
        "\n".join(
            [
                "instrument:",
                "  symbol: TEST",
                "strategy:",
                f"  name: {strategy}",
            ]
        ),
        encoding="utf-8",
    )
    if tables:
        _write_tables(run, net_pnl)
    return run


def _write_tables(run: Path, net_pnl: float) -> None:
    pl.DataFrame(
        {
            "timestamp_ns": [0, 1_000_000_000, 1_000_000_000, 2_000_000_000],
            "sequence_number": [0, 1, 2, 3],
            "midpoint_ticks": [100.0, 100.5, 100.5, 101.0],
            "spread_regime": ["tight", "wide", "wide", "tight"],
            "volatility_regime": ["low", "high", "high", "low"],
            "liquidity_regime": ["deep", "thin", "thin", "deep"],
        }
    ).write_parquet(run / "market.parquet")
    pl.DataFrame(
        {
            "timestamp_ns": [100_000_000, 200_000_000, 1_100_000_000],
            "side": [1, -1, 1],
            "action": ["submit", "submit", "cancel"],
            "price_ticks": [99, 102, 99],
        }
    ).write_parquet(run / "quotes.parquet")
    pl.DataFrame(
        {
            "timestamp_ns": [0, 1_000_000_000, 2_000_000_000],
            "inventory": [0, 2, 1],
        }
    ).write_parquet(run / "inventory.parquet")
    pl.DataFrame(
        {
            "timestamp_ns": [0, 1_000_000_000, 2_000_000_000],
            "gross_pnl": [0.0, net_pnl / 2 + 0.2, net_pnl + 0.2],
            "net_pnl": [0.0, net_pnl / 2, net_pnl],
        }
    ).write_parquet(run / "pnl.parquet")
    pl.DataFrame(
        {
            "fill_id": ["F1", "F2"],
            "order_id": ["O1", "O2"],
            "side": [1, -1],
            "quantity": [2, 1],
            "price_ticks": [100, 101],
            "exchange_fill_timestamp_ns": [900_000_000, 1_900_000_000],
            "queue_ahead_estimate": [10, 5],
        }
    ).write_parquet(run / "fills.parquet")
    pl.DataFrame(
        {
            "transition_id": ["T1", "T2"],
            "order_id": ["O1", "O2"],
            "new_status": ["filled", "filled"],
            "timestamp_ns": [900_000_000, 1_900_000_000],
            "queue_ahead_estimate": [12, 6],
        }
    ).write_parquet(run / "orders.parquet")
    pl.DataFrame(
        {
            "fill_id": ["F1", "F1", "F2", "F2"],
            "horizon_ns": [
                100_000_000,
                1_000_000_000,
                100_000_000,
                1_000_000_000,
            ],
            "markout_ticks": [0.2, 0.4, -0.1, 0.1],
        }
    ).write_parquet(run / "markouts.parquet")
    pl.DataFrame(
        schema={
            "timestamp_ns": pl.Int64,
            "reason": pl.String,
            "detail": pl.String,
        }
    ).write_parquet(run / "risk_events.parquet")


def test_load_artifacts_and_human_summary(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "fixed-demo",
        strategy="fixed_spread",
        net_pnl=1.25,
    )
    artifacts = load_run_artifacts(run)
    assert artifacts.run_name == "fixed-demo"
    assert artifacts.strategy == "fixed_spread"
    assert artifacts.symbol == "TEST"
    assert artifacts.table("market").height == 4
    assert artifacts.table("latency_sensitivity").is_empty()

    summary = human_readable_summary(artifacts)
    assert "Run: fixed-demo" in summary
    assert "Strategy: fixed_spread" in summary
    assert "Net P&L: 1.25" in summary
    assert "Research disclaimer:" in summary


def test_generate_report_creates_every_required_plot(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "inventory-demo",
        strategy="inventory_aware",
        net_pnl=2.0,
    )
    result = generate_report(run)
    assert matplotlib.get_backend().lower() == "agg"
    assert tuple(result.plot_paths) == REQUIRED_PLOT_FILENAMES
    assert result.summary_text.startswith("Run: inventory-demo")
    assert (run / "report_summary.txt").read_text(encoding="utf-8").endswith("\n")
    for path in result.plot_paths.values():
        assert path.is_file()
        assert path.stat().st_size > 100
        assert path.read_bytes().startswith(b"\x89PNG")


def test_generate_report_uses_honest_no_data_panels(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "empty-demo",
        strategy="microprice",
        net_pnl=0.0,
        tables=False,
    )
    result = generate_report(run)
    assert set(result.plot_paths) == set(REQUIRED_PLOT_FILENAMES)
    assert all(path.stat().st_size > 100 for path in result.plot_paths.values())


def test_compare_runs_writes_stable_machine_readable_outputs(
    tmp_path: Path,
) -> None:
    first = _write_run(
        tmp_path,
        "b-run",
        strategy="inventory_aware",
        net_pnl=-0.5,
    )
    second = _write_run(
        tmp_path,
        "a-run",
        strategy="fixed_spread",
        net_pnl=1.5,
    )
    output = tmp_path / "comparison-output"
    table = compare_runs([first, second], output)

    assert table["run_name"].to_list() == ["a-run", "b-run"]
    assert table["strategy"].to_list() == ["fixed_spread", "inventory_aware"]
    assert table["net_pnl"].to_list() == [1.5, -0.5]
    assert (output / "comparison.csv").is_file()
    assert (output / "comparison.json").is_file()
    assert (output / "strategy_comparison.png").stat().st_size > 100
    records = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    assert [row["run_name"] for row in records] == ["a-run", "b-run"]


def test_compare_runs_default_destination_is_next_to_runs(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "only-run",
        strategy="fixed_spread",
        net_pnl=0.0,
        tables=False,
    )
    compare_runs([run])
    assert (tmp_path / "comparison" / "comparison.csv").is_file()


def test_compare_runs_rejects_different_event_streams(tmp_path: Path) -> None:
    first = _write_run(
        tmp_path,
        "first",
        strategy="fixed_spread",
        net_pnl=0.0,
    )
    second = _write_run(
        tmp_path,
        "second",
        strategy="microprice",
        net_pnl=0.0,
    )
    summary_path = second / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["event_stream_sha256"] = "b" * 64
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ReportError, match="event-stream SHA-256"):
        compare_runs([first, second], tmp_path / "comparison")


def test_missing_or_malformed_artifacts_fail_clearly(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="does not exist"):
        load_run_artifacts(tmp_path / "missing")

    run = tmp_path / "incomplete"
    run.mkdir()
    with pytest.raises(ReportError, match=r"metrics\.json"):
        load_run_artifacts(run)

    (run / "metrics.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ReportError, match="must contain an object"):
        load_run_artifacts(run)


def test_malformed_parquet_fails_with_artifact_path(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "broken",
        strategy="fixed_spread",
        net_pnl=0.0,
        tables=False,
    )
    (run / "orders.parquet").write_bytes(b"not parquet")
    with pytest.raises(ReportError, match=r"orders\.parquet"):
        load_run_artifacts(run)


def test_compare_requires_at_least_one_run(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="at least one"):
        compare_runs([], tmp_path / "comparison")
