from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
import typer

import lobmm.cli as cli
from lobmm.config import AppConfig
from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import MarketEvent


def _events() -> list[MarketEvent]:
    return [
        MarketEvent(0, 0, EventType.SNAPSHOT, Side.BID, 100, 10),
        MarketEvent(0, 1, EventType.SNAPSHOT, Side.ASK, 102, 10),
    ]


def _install_lightweight_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 10,
                "regime_length": 20,
                "reset_interval": 20,
            },
            "output": {"write_plots": False},
        }
    )
    events = _events()

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_configured_events", lambda _: events)
    monkeypatch.setattr(cli, "generate_synthetic_events", lambda _: events)
    monkeypatch.setattr(cli, "load_events", lambda _: events)
    monkeypatch.setattr(cli, "write_parquet", lambda _rows, path: Path(path))
    monkeypatch.setattr(
        cli,
        "validate_event_stream",
        lambda *_args, **_kwargs: SimpleNamespace(
            valid=True,
            event_count=len(events),
            event_stream_sha256="a" * 64,
            mode=ValidationMode.STRICT,
            issue_count=0,
            diagnostics={},
        ),
    )

    def fake_backtest(
        _config: AppConfig,
        _events: list[MarketEvent],
        *,
        run_name: str,
        output_root: str | Path | None = None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        root = Path(output_root) if output_root is not None else tmp_path
        return SimpleNamespace(run_directory=root / run_name)

    monkeypatch.setattr(cli, "run_backtest", fake_backtest)
    monkeypatch.setattr(cli, "console_summary", lambda _: "summary")
    monkeypatch.setattr(
        cli,
        "compare_runs",
        lambda *_args, **_kwargs: pl.DataFrame({"strategy": ["fixed_spread"]}),
    )
    monkeypatch.setattr(
        cli,
        "generate_report",
        lambda run: SimpleNamespace(
            plot_paths={"plot": Path(run) / "plot.png"},
            plots_directory=Path(run) / "plots",
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_benchmark",
        lambda _: SimpleNamespace(as_dict=lambda: {"events": 10}),
    )
    monkeypatch.setattr(
        cli,
        "run_full_benchmark",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"median_events_per_second": 1.0}
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_experiment",
        lambda *_args, **_kwargs: SimpleNamespace(
            cases=(object(),),
            event_stream_sha256="a" * 64,
            experiment_directory=tmp_path / "experiment",
        ),
    )
    monkeypatch.setattr(
        cli,
        "publish_case_study",
        lambda *_args, **_kwargs: SimpleNamespace(
            markdown_path=tmp_path / "CASE_STUDY.md"
        ),
    )


def test_cli_research_workflow_commands_have_lightweight_success_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_lightweight_workflow(monkeypatch, tmp_path)
    config_path = tmp_path / "config.yaml"

    cli.generate_synthetic_command(config_path, tmp_path / "events.parquet")
    cli.validate_data_command(tmp_path / "events.parquet", lenient=True)
    cli.backtest_command(config_path, "backtest")
    cli.compare_command([tmp_path / "one", tmp_path / "two"], tmp_path / "compare")
    cli.report_command(tmp_path / "backtest")
    cli.benchmark_command(config_path)
    cli.benchmark_full_command(
        config_path,
        repeats=2,
        event_count=10,
        no_warmup=True,
        no_memory=True,
    )
    cli.experiment_command(
        config_path,
        "small-grid",
        strategies="fixed_spread",
        latency_multipliers="1",
        queue_allocations="back_of_queue",
        fee_multipliers="1",
        ablations="full",
        output_root=tmp_path / "experiments",
        allow_large_grid=False,
    )
    cli.demo_command(
        tmp_path / "runs",
        tmp_path / "experiments",
        publish=True,
        skip_sensitivity=False,
    )

    output = capsys.readouterr().out
    assert "Synthetic data is a functional demonstration" in output
    assert "median_events_per_second" in output
    assert "case_study" in output


def test_cli_helpers_and_failures_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._csv_values(" a, ,b ", option="--values") == ("a", "b")
    with pytest.raises(ValueError, match="at least one"):
        cli._csv_values(" , ", option="--values")

    _install_lightweight_workflow(monkeypatch, tmp_path)
    with pytest.raises(typer.Exit) as exc_info:
        cli.compare_command([tmp_path / "only"], None)
    assert exc_info.value.exit_code == 1
    assert "requires at least two" in capsys.readouterr().err

    with pytest.raises(typer.Exit) as large_grid:
        cli.experiment_command(
            tmp_path / "config.yaml",
            "too-large",
            strategies="fixed_spread,inventory_aware,microprice",
            latency_multipliers="0.5,1,2,3",
            queue_allocations="back_of_queue,pro_rata,front_of_queue",
            fee_multipliers="0,1,2",
            ablations=(
                "full,no_inventory,no_explicit_imbalance,no_volatility_widening"
            ),
            output_root=tmp_path / "experiments",
            allow_large_grid=False,
        )
    assert large_grid.value.exit_code == 1
    assert "experiment expands to" in capsys.readouterr().err
