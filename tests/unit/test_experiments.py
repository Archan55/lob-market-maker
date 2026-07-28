from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest

from lobmm.config import AppConfig
from lobmm.enums import QueueAllocation, StrategyName
from lobmm.experiments import (
    ExperimentError,
    ExperimentSpec,
    StrategyAblation,
    config_for_case,
    event_stream_sha256,
    iter_cases,
    run_experiment,
)
from lobmm.synthetic import generate_synthetic_events


def test_experiment_spec_validates_empty_and_negative_dimensions() -> None:
    with pytest.raises(ExperimentError, match="strategy"):
        ExperimentSpec(strategies=())
    with pytest.raises(ExperimentError, match="latency"):
        ExperimentSpec(
            strategies=(StrategyName.FIXED_SPREAD,),
            latency_multipliers=(-1.0,),
        )
    with pytest.raises(ExperimentError, match="fee"):
        ExperimentSpec(
            strategies=(StrategyName.FIXED_SPREAD,),
            fee_multipliers=(Decimal("-1"),),
        )
    with pytest.raises(ExperimentError, match="finite"):
        ExperimentSpec(
            strategies=(StrategyName.FIXED_SPREAD,),
            latency_multipliers=(float("nan"),),
        )
    with pytest.raises(ExperimentError, match="finite"):
        ExperimentSpec(
            strategies=(StrategyName.FIXED_SPREAD,),
            fee_multipliers=(Decimal("Infinity"),),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "strategies",
            (StrategyName.FIXED_SPREAD, StrategyName.FIXED_SPREAD),
        ),
        ("latency_multipliers", (1.0, 1.0)),
        (
            "queue_allocations",
            (QueueAllocation.BACK_OF_QUEUE, QueueAllocation.BACK_OF_QUEUE),
        ),
        ("fee_multipliers", (Decimal("1"), Decimal("1.0"))),
        ("ablations", (StrategyAblation.FULL, StrategyAblation.FULL)),
    ],
)
def test_experiment_spec_rejects_duplicate_dimensions(
    field: str,
    value: tuple[object, object],
) -> None:
    arguments: dict[str, object] = {"strategies": (StrategyName.FIXED_SPREAD,)}
    arguments[field] = value
    with pytest.raises(ExperimentError, match="duplicates"):
        ExperimentSpec(**arguments)  # type: ignore[arg-type]


def test_case_expansion_and_configuration_are_deterministic(tmp_path) -> None:
    spec = ExperimentSpec(
        strategies=(StrategyName.FIXED_SPREAD, StrategyName.MICROPRICE),
        latency_multipliers=(0.0, 2.0),
        queue_allocations=(QueueAllocation.PRO_RATA,),
        fee_multipliers=(Decimal("1.5"),),
        ablations=(
            StrategyAblation.FULL,
            StrategyAblation.NO_INVENTORY,
        ),
    )
    cases = iter_cases(spec)
    assert spec.case_count == 8
    assert len(cases) == 8
    assert len({case.run_name for case in cases}) == 8

    base = AppConfig()
    config = config_for_case(base, cases[-1], runs_directory=tmp_path)
    assert config.latency.market_data_ns == base.latency.market_data_ns * 2
    assert config.queue_model.cancellation_allocation is QueueAllocation.PRO_RATA
    assert config.fees.maker_rebate_per_unit == Decimal("0.0003")
    assert config.strategy.inventory_penalty_ticks == 0.0
    assert config.output.runs_directory == tmp_path
    assert not config.output.write_plots


def test_event_stream_hash_changes_with_event_content() -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 30,
                "regime_length": 20,
                "reset_interval": 30,
            }
        }
    )
    events = generate_synthetic_events(config)
    first = event_stream_sha256(events)
    assert first == event_stream_sha256(list(events))
    assert first != event_stream_sha256(events[:-1])


def test_close_multipliers_have_distinct_run_directories() -> None:
    spec = ExperimentSpec(
        strategies=(StrategyName.FIXED_SPREAD,),
        latency_multipliers=(1.0, 1.004),
        queue_allocations=(QueueAllocation.BACK_OF_QUEUE,),
    )

    names = {case.run_name for case in iter_cases(spec)}

    assert len(names) == 2
    assert any("lat1p004" in name for name in names)


def test_run_experiment_writes_auditable_grid(tmp_path) -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 60,
                "regime_length": 20,
                "reset_interval": 40,
            },
            "backtest": {
                "warmup_events": 10,
                "timer_interval_ns": 10_000_000,
            },
            "output": {"write_plots": False},
        }
    )
    events = generate_synthetic_events(config)
    spec = ExperimentSpec(
        strategies=(StrategyName.FIXED_SPREAD,),
        latency_multipliers=(0.5, 1.0),
        queue_allocations=(QueueAllocation.BACK_OF_QUEUE,),
        fee_multipliers=(Decimal("1"),),
        ablations=(StrategyAblation.FULL,),
    )
    result = run_experiment(
        config,
        events,
        name="small-grid",
        spec=spec,
        output_root=tmp_path,
    )

    assert len(result.results) == 2
    assert len(result.event_stream_sha256) == 64
    assert (result.experiment_directory / "experiment.json").is_file()
    assert (result.experiment_directory / "sensitivity.csv").is_file()
    assert (result.experiment_directory / "sensitivity_overview.png").is_file()
    report = (result.experiment_directory / "REPORT.md").read_text(encoding="utf-8")
    assert "Synthetic demonstration" in report
    saved = pl.read_parquet(result.experiment_directory / "sensitivity.parquet")
    assert saved.height == 2
    assert set(saved["latency_multiplier"]) == {0.5, 1.0}
