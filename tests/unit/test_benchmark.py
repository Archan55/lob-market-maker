from __future__ import annotations

from pathlib import Path

import pytest

from lobmm.benchmark import (
    _benchmark_config,
    _configured_events,
    _percentile,
    _selected_events,
    benchmark_events,
    run_benchmark,
    run_full_benchmark,
)
from lobmm.config import AppConfig
from lobmm.data import sha256_file, write_parquet
from lobmm.synthetic import generate_synthetic_events


def test_percentile_uses_deterministic_nearest_rank() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert _percentile(values, 0.05) == 1.0
    assert _percentile(values, 0.5) == 2.0
    assert _percentile(values, 0.95) == 4.0
    with pytest.raises(ValueError, match="at least one"):
        _percentile([], 0.5)
    with pytest.raises(ValueError, match="probability"):
        _percentile(values, 1.1)


def test_full_benchmark_measures_repeated_complete_event_loop() -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 40,
                "regime_length": 20,
                "reset_interval": 30,
            },
            "backtest": {
                "warmup_events": 5,
                "timer_interval_ns": 10_000_000,
            },
            "output": {"write_plots": False},
        }
    )

    result = run_full_benchmark(
        config,
        repeats=2,
        event_count=30,
        warmup=False,
        measure_memory=False,
    )

    assert result.event_count == 30
    assert result.repeat_count == 2
    assert len(result.elapsed_seconds) == 2
    assert len(result.events_per_second) == 2
    assert result.median_events_per_second > 0
    assert result.median_scheduler_events_per_market_event >= 1
    assert result.traced_peak_memory_bytes is None
    assert len(result.event_stream_sha256) == 64
    assert "event loop only" in result.timing_scope


def test_reference_benchmark_profiles_real_book_application() -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 20,
                "regime_length": 20,
                "reset_interval": 20,
            }
        }
    )
    result = run_benchmark(config)

    assert result.event_count == 20
    assert result.events_per_second > 0
    assert result.peak_memory_bytes > 0
    assert "book.py" in result.profile_top
    assert result.as_dict()["random_seed"] == config.random_seed

    with pytest.raises(ValueError, match="empty"):
        benchmark_events([], random_seed=1, dataset="empty")


def test_full_benchmark_warmup_memory_and_argument_guards() -> None:
    config = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 20,
                "regime_length": 20,
                "reset_interval": 20,
            },
            "backtest": {"warmup_events": 2},
            "output": {"write_plots": False},
        }
    )
    result = run_full_benchmark(
        config,
        repeats=1,
        event_count=20,
        warmup=True,
        measure_memory=True,
    )

    assert result.traced_peak_memory_bytes is not None
    assert result.traced_peak_memory_bytes > 0
    assert result.as_dict()["repeat_count"] == 1

    with pytest.raises(ValueError, match="repeats"):
        run_full_benchmark(config, repeats=0)
    with pytest.raises(ValueError, match="event_count"):
        run_full_benchmark(config, event_count=0)


def test_historical_benchmark_loading_checksum_and_filters(tmp_path: Path) -> None:
    synthetic = AppConfig.model_validate(
        {
            "synthetic": {
                "event_count": 20,
                "regime_length": 20,
                "reset_interval": 20,
            }
        }
    )
    events = generate_synthetic_events(synthetic)
    path = write_parquet(events, tmp_path / "events.parquet")
    checksum = sha256_file(path)
    historical = AppConfig.model_validate(
        {
            "instrument": {"symbol": "TEST"},
            "data": {
                "input_path": str(path),
                "provenance": {
                    "source_type": "historical",
                    "provider": "fictional fixture",
                    "venue": "TEST",
                    "symbol": "TEST",
                    "dataset_id": "fixture",
                    "session_start": "2024-01-02",
                    "session_end": "2024-01-02",
                    "checksum_sha256": checksum,
                    "license_notes": "Repository-owned fixture.",
                },
            },
        }
    )

    loaded, dataset = _configured_events(historical)
    assert loaded == events
    assert dataset == "fictional fixture/fixture/TEST"
    limited = _benchmark_config(historical, event_count=3, track_memory=True)
    assert limited.backtest.event_limit == 3
    assert limited.backtest.track_memory

    mismatch = historical.model_copy(
        update={
            "data": historical.data.model_copy(
                update={
                    "provenance": historical.data.provenance.model_copy(
                        update={"checksum_sha256": "0" * 64}
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        _configured_events(mismatch)

    missing = historical.model_copy(
        update={
            "data": historical.data.model_copy(
                update={"input_path": tmp_path / "missing.parquet"}
            )
        }
    )
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _configured_events(missing)

    empty_window = historical.model_copy(
        update={
            "backtest": historical.backtest.model_copy(
                update={"start_timestamp_ns": events[-1].timestamp_ns + 1}
            )
        }
    )
    with pytest.raises(ValueError, match="no events"):
        _selected_events(empty_window, events)
