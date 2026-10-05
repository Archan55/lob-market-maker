from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from lobmm.case_study import (
    CaseStudyError,
    _sensitivity_interpretation,
    publish_case_study,
)


def _write_run(
    root: Path,
    name: str,
    strategy: str,
    inventory: float,
    *,
    stream_hash: str = "a" * 64,
) -> Path:
    run = root / name
    plots = run / "plots"
    plots.mkdir(parents=True)
    metrics = {
        "engineering": {"events_processed": 100},
        "trading_activity": {"fill_events": 5, "fill_rate": 0.25},
        "inventory": {
            "mean_absolute": inventory,
            "time_near_limits_fraction": 0.1,
            "end_of_session": -2,
        },
        "pnl": {
            "net_pnl": 1.0,
            "gross_pnl": 1.0,
            "fees": 0.0,
            "rebates": 0.0,
            "maximum_drawdown": 2.0,
        },
        "risk": {"event_count": 1},
        "execution_quality": {"realized_spread_ticks": 0.5},
        "research_disclaimer": "Synthetic demonstration.",
    }
    summary = {
        "run_name": name,
        "strategy": strategy,
        "symbol": "SYNTH",
        "event_count": 100,
        "synthetic_demonstration": True,
        "event_stream_sha256": stream_hash,
        "dataset_provenance": {
            "provider": "fixture",
            "dataset_id": "fixture-stream",
            "source_type": "synthetic",
        },
    }
    diagnostics = {
        "events_processed": 100,
        "scheduler_events_processed": 400,
        "exchange_rejected_quote_attempts": 2,
    }
    (run / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run / "diagnostics.json").write_text(
        json.dumps(diagnostics),
        encoding="utf-8",
    )
    for filename in (
        "midpoint_and_quotes.png",
        "inventory_over_time.png",
        "markout_by_horizon.png",
        "latency_sensitivity.png",
    ):
        (plots / filename).write_bytes(b"fixture-image")
    return run


def _write_experiment_manifest(
    experiment: Path,
    *,
    stream_hash: str = "a" * 64,
    case_count: int = 1,
    event_count: int = 100,
    synthetic: bool = True,
) -> None:
    (experiment / "experiment.json").write_text(
        json.dumps(
            {
                "case_count": case_count,
                "event_count": event_count,
                "event_stream_sha256": stream_hash,
                "synthetic_demonstration": synthetic,
            }
        ),
        encoding="utf-8",
    )


def test_publish_case_study_copies_curated_assets_and_writes_honest_page(
    tmp_path: Path,
) -> None:
    fixed = _write_run(tmp_path, "fixed", "fixed_spread", 12.0)
    inventory = _write_run(tmp_path, "inventory", "inventory_aware", 8.0)
    micro = _write_run(tmp_path, "micro", "microprice", 5.0)
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    pl.DataFrame(
        {
            "run_name": ["microprice__full__lat1__back_of_queue__fee1"],
            "strategy": ["microprice"],
            "queue_allocation": ["back_of_queue"],
            "ablation": ["full"],
            "fee_multiplier": [1.0],
            "order_entry_latency_ns": [100_000],
            "latency_multiplier": [1.0],
            "events_processed": [100],
            "risk_event_count": [1],
            "fill_rate": [0.25],
            "mean_absolute_inventory": [5.0],
            "maximum_drawdown": [2.0],
            "net_pnl": [1.0],
            "synthetic_demonstration": [True],
        }
    ).write_parquet(experiment / "sensitivity.parquet")
    _write_experiment_manifest(experiment)
    (experiment / "sensitivity_overview.png").write_bytes(b"fixture-image")

    result = publish_case_study(
        [fixed, inventory, micro],
        comparison_directory=tmp_path / "comparison",
        assets_directory=tmp_path / "assets" / "case-study",
        markdown_path=tmp_path / "CASE_STUDY.md",
        experiment_directory=experiment,
    )

    page = result.markdown_path.read_text(encoding="utf-8")
    assert "SYNTHETIC DEMONSTRATION" in page
    assert "not evidence of expected market profitability" in page
    assert "Mean abs. inventory" in page
    assert "Venue rejects" in page
    assert "[controlled end-to-end replay](QUEUE_CANCELLATION.md)" in page
    assert len(result.asset_paths) == 6
    assert all(path.is_file() for path in result.asset_paths)


def test_publish_case_study_rejects_mismatched_event_streams(tmp_path: Path) -> None:
    fixed = _write_run(tmp_path, "fixed", "fixed_spread", 12.0)
    inventory = _write_run(tmp_path, "inventory", "inventory_aware", 8.0)
    micro = _write_run(
        tmp_path,
        "micro",
        "microprice",
        5.0,
        stream_hash="b" * 64,
    )

    with pytest.raises(CaseStudyError, match="same event stream"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison",
            assets_directory=tmp_path / "assets",
            markdown_path=tmp_path / "CASE_STUDY.md",
        )


def test_sensitivity_interpretation_describes_latency_and_queue_evidence() -> None:
    rows = pl.DataFrame(
        {
            "strategy": ["microprice"] * 4,
            "ablation": ["full"] * 4,
            "fee_multiplier": [1.0] * 4,
            "latency_multiplier": [0.5, 0.5, 2.0, 2.0],
            "queue_allocation": [
                "back_of_queue",
                "front_of_queue",
                "back_of_queue",
                "front_of_queue",
            ],
            "fill_rate": [0.04, 0.04, 0.02, 0.02],
            "mean_absolute_inventory": [10.0, 10.0, 20.0, 20.0],
            "maximum_drawdown": [1.0] * 4,
            "net_pnl": [0.0] * 4,
        }
    )

    interpretation = _sensitivity_interpretation(rows)

    assert "0.5x to 2x" in interpretation
    assert "10.00 to 20.00" in interpretation
    assert "does not discriminate" in interpretation
    assert _sensitivity_interpretation(pl.DataFrame()) == ""

    changed = rows.with_columns(
        pl.when(pl.col("queue_allocation") == "front_of_queue")
        .then(pl.lit(0.03))
        .otherwise(pl.col("fill_rate"))
        .alias("fill_rate")
    )
    assert "does not discriminate" not in _sensitivity_interpretation(changed)


def test_case_study_rejects_incomplete_or_inconsistent_run_sets(
    tmp_path: Path,
) -> None:
    fixed = _write_run(tmp_path, "fixed", "fixed_spread", 12.0)
    inventory = _write_run(tmp_path, "inventory", "inventory_aware", 8.0)
    micro = _write_run(tmp_path, "micro", "microprice", 5.0)

    with pytest.raises(CaseStudyError, match="exactly one"):
        publish_case_study(
            [fixed, micro],
            comparison_directory=tmp_path / "comparison-short",
            assets_directory=tmp_path / "assets-short",
            markdown_path=tmp_path / "short.md",
        )

    inventory_summary_path = inventory / "summary.json"
    inventory_summary = json.loads(inventory_summary_path.read_text(encoding="utf-8"))
    inventory_summary["synthetic_demonstration"] = False
    inventory_summary_path.write_text(
        json.dumps(inventory_summary),
        encoding="utf-8",
    )
    with pytest.raises(CaseStudyError, match="cannot mix"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-mixed",
            assets_directory=tmp_path / "assets-mixed",
            markdown_path=tmp_path / "mixed.md",
        )

    inventory_summary["synthetic_demonstration"] = True
    inventory_summary["dataset_provenance"] = {
        "provider": "different",
        "dataset_id": "fixture-stream",
        "source_type": "synthetic",
    }
    inventory_summary_path.write_text(
        json.dumps(inventory_summary),
        encoding="utf-8",
    )
    with pytest.raises(CaseStudyError, match="different dataset provenance"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-provenance",
            assets_directory=tmp_path / "assets-provenance",
            markdown_path=tmp_path / "provenance.md",
        )


def test_case_study_requires_explicit_consistent_classification(
    tmp_path: Path,
) -> None:
    fixed = _write_run(tmp_path, "fixed", "fixed_spread", 12.0)
    inventory = _write_run(tmp_path, "inventory", "inventory_aware", 8.0)
    micro = _write_run(tmp_path, "micro", "microprice", 5.0)
    summary_path = inventory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.pop("synthetic_demonstration")
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(CaseStudyError, match="as a boolean"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-missing-classification",
        )

    summary["synthetic_demonstration"] = True
    summary["dataset_provenance"]["source_type"] = "historical"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(CaseStudyError, match="disagrees"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-wrong-classification",
        )


def test_case_study_rejects_bad_experiment_and_missing_assets(tmp_path: Path) -> None:
    fixed = _write_run(tmp_path, "fixed", "fixed_spread", 12.0)
    inventory = _write_run(tmp_path, "inventory", "inventory_aware", 8.0)
    micro = _write_run(tmp_path, "micro", "microprice", 5.0)
    experiment = tmp_path / "experiment"
    experiment.mkdir()

    with pytest.raises(CaseStudyError, match="does not exist"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-missing-table",
            experiment_directory=experiment,
            assets_directory=tmp_path / "assets-missing-table",
            markdown_path=tmp_path / "missing-table.md",
        )

    _write_experiment_manifest(experiment)
    (experiment / "sensitivity.parquet").write_bytes(b"not parquet")
    with pytest.raises(CaseStudyError, match="cannot read"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-bad-table",
            experiment_directory=experiment,
            assets_directory=tmp_path / "assets-bad-table",
            markdown_path=tmp_path / "bad-table.md",
        )

    pl.DataFrame(
        {
            "run_name": ["microprice__full__lat1__back_of_queue__fee1"],
            "strategy": ["microprice"],
            "ablation": ["full"],
            "latency_multiplier": [1.0],
            "order_entry_latency_ns": [100_000],
            "queue_allocation": ["back_of_queue"],
            "fee_multiplier": [1.0],
            "events_processed": [100],
            "risk_event_count": [0],
            "fill_rate": [0.1],
            "net_pnl": [0.0],
            "maximum_drawdown": [1.0],
            "mean_absolute_inventory": [1.0],
            "synthetic_demonstration": [True],
        }
    ).write_parquet(experiment / "sensitivity.parquet")
    (experiment / "sensitivity_overview.png").write_bytes(b"fixture-image")
    _write_experiment_manifest(experiment, stream_hash="b" * 64)
    with pytest.raises(CaseStudyError, match="different event streams"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-bad-hash",
            experiment_directory=experiment,
            assets_directory=tmp_path / "assets-bad-hash",
            markdown_path=tmp_path / "bad-hash.md",
        )

    _write_experiment_manifest(experiment, case_count=2)
    with pytest.raises(CaseStudyError, match="case_count"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-bad-count",
            experiment_directory=experiment,
            assets_directory=tmp_path / "assets-bad-count",
            markdown_path=tmp_path / "bad-count.md",
        )

    (experiment / "experiment.json").unlink()
    comparison = tmp_path / "comparison-missing-manifest"
    with pytest.raises(CaseStudyError, match="manifest does not exist"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=comparison,
            experiment_directory=experiment,
            assets_directory=tmp_path / "assets-missing-manifest",
            markdown_path=tmp_path / "missing-manifest.md",
        )
    assert not comparison.exists()

    (micro / "plots" / "midpoint_and_quotes.png").unlink()
    with pytest.raises(CaseStudyError, match="artifact is missing"):
        publish_case_study(
            [fixed, inventory, micro],
            comparison_directory=tmp_path / "comparison-missing-asset",
            assets_directory=tmp_path / "assets-missing",
            markdown_path=tmp_path / "missing-asset.md",
        )
