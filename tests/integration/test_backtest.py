from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import yaml

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig
from lobmm.synthetic import generate_synthetic_events


def small_config() -> AppConfig:
    base = AppConfig()
    return base.model_copy(
        update={
            "data": base.data.model_copy(
                update={"input_path": Path("data/processed/synthetic.parquet")}
            ),
            "synthetic": base.synthetic.model_copy(update={"event_count": 200}),
            "backtest": base.backtest.model_copy(
                update={"warmup_events": 20, "event_limit": 200}
            ),
            "output": base.output.model_copy(update={"write_plots": False}),
        }
    )


def test_backtest_writes_complete_structured_artifacts(tmp_path) -> None:
    config = small_config()
    events = generate_synthetic_events(config)
    result = run_backtest(
        config,
        events,
        run_name="integration",
        output_root=tmp_path,
        generate_plots=False,
    )
    expected = {
        "run_config.yaml",
        "summary.json",
        "metrics.json",
        "diagnostics.json",
        "orders.parquet",
        "fills.parquet",
        "inventory.parquet",
        "pnl.parquet",
        "quotes.parquet",
        "risk_events.parquet",
        "market.parquet",
        "markouts.parquet",
    }
    assert expected <= {path.name for path in result.run_directory.iterdir()}
    summary = json.loads(
        (result.run_directory / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["synthetic_demonstration"] is True
    assert summary["dataset_provenance"]["source_type"] == "synthetic"
    assert summary["dataset_provenance"]["synthetic_demonstration"] is True
    diagnostics = json.loads(
        (result.run_directory / "diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["synthetic_demonstration"] is True
    assert diagnostics["dataset_provenance"] == summary["dataset_provenance"]
    assert len(summary["event_stream_sha256"]) == 64
    assert diagnostics["event_stream_sha256"] == summary["event_stream_sha256"]
    saved_config = yaml.safe_load(
        (result.run_directory / "run_config.yaml").read_text(encoding="utf-8")
    )
    assert saved_config["data"]["input_path"].endswith("synthetic.parquet")
    assert saved_config["data"]["provenance"]["source_type"] == "synthetic"
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    if fills.height:
        assert (
            fills["exchange_fill_timestamp_ns"]
            >= fills["exchange_arrival_timestamp_ns"]
        ).all()
        assert (
            fills["exchange_arrival_timestamp_ns"] >= fills["send_timestamp_ns"]
        ).all()
        assert (fills["send_timestamp_ns"] >= fills["decision_timestamp_ns"]).all()


def test_same_seed_produces_identical_decisions_and_fills(tmp_path) -> None:
    config = small_config()
    events = generate_synthetic_events(config)
    first = run_backtest(
        config,
        events,
        run_name="one",
        output_root=tmp_path,
        generate_plots=False,
    )
    second = run_backtest(
        config,
        events,
        run_name="two",
        output_root=tmp_path,
        generate_plots=False,
    )
    assert pl.read_parquet(first.run_directory / "quotes.parquet").equals(
        pl.read_parquet(second.run_directory / "quotes.parquet")
    )
    assert pl.read_parquet(first.run_directory / "fills.parquet").equals(
        pl.read_parquet(second.run_directory / "fills.parquet")
    )
