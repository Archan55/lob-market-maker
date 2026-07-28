"""Measured reference-engine benchmarking and profiling."""

from __future__ import annotations

import cProfile
import io
import math
import platform
import pstats
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from lobmm.backtest import run_backtest
from lobmm.book import L2Book
from lobmm.config import AppConfig
from lobmm.data import event_stream_sha256, sha256_file
from lobmm.data.loaders import load_events
from lobmm.enums import ValidationMode
from lobmm.events import MarketEvent
from lobmm.synthetic import generate_synthetic_events
from lobmm.validation import validate_event_stream


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    event_count: int
    elapsed_seconds: float
    events_per_second: float
    peak_memory_bytes: int
    python_version: str
    platform: str
    random_seed: int
    dataset: str
    profile_top: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FullBenchmarkResult:
    """Repeated measurements of the complete scheduler/exchange/strategy loop."""

    event_count: int
    repeat_count: int
    elapsed_seconds: tuple[float, ...]
    events_per_second: tuple[float, ...]
    median_elapsed_seconds: float
    p95_elapsed_seconds: float
    median_events_per_second: float
    p05_events_per_second: float
    median_scheduler_events_per_market_event: float
    traced_peak_memory_bytes: int | None
    python_version: str
    platform: str
    random_seed: int
    dataset: str
    event_stream_sha256: str
    timing_scope: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def benchmark_events(
    events: list[MarketEvent],
    *,
    random_seed: int,
    dataset: str,
    profile_rows: int = 10,
) -> BenchmarkResult:
    """Measure and profile one deterministic reference-book replay."""

    if not events:
        raise ValueError("cannot benchmark an empty event stream")
    book = L2Book(ValidationMode.LENIENT)
    profiler = cProfile.Profile()
    tracemalloc.start()
    started = time.perf_counter()
    profiler.enable()
    for event in events:
        book.apply(event)
    profiler.disable()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats(
        "cumulative"
    ).print_stats(profile_rows)
    return BenchmarkResult(
        event_count=len(events),
        elapsed_seconds=elapsed,
        events_per_second=len(events) / elapsed if elapsed else float("inf"),
        peak_memory_bytes=peak,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        random_seed=random_seed,
        dataset=dataset,
        profile_top=stream.getvalue(),
    )


def run_benchmark(config: AppConfig) -> BenchmarkResult:
    """Load configured data or generate the configured deterministic stream."""

    events, dataset = _configured_events(config)
    validate_event_stream(events, mode=config.data.validation_mode)
    return benchmark_events(events, random_seed=config.random_seed, dataset=dataset)


def run_full_benchmark(
    config: AppConfig,
    *,
    repeats: int = 5,
    event_count: int | None = None,
    warmup: bool = True,
    measure_memory: bool = True,
) -> FullBenchmarkResult:
    """Measure the full causal event loop separately from artifact generation."""

    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if event_count is not None and event_count <= 0:
        raise ValueError("event_count must be positive")

    effective = _benchmark_config(config, event_count=event_count, track_memory=False)
    events, dataset = _configured_events(effective)
    input_validation = validate_event_stream(
        events,
        mode=effective.data.validation_mode,
    )
    stream_hash = event_stream_sha256(events)
    elapsed_values: list[float] = []
    throughput_values: list[float] = []
    scheduler_ratios: list[float] = []
    processed_values: list[int] = []

    with tempfile.TemporaryDirectory(prefix="lobmm-full-benchmark-") as temporary:
        root = Path(temporary)
        if warmup:
            run_backtest(
                effective,
                events,
                run_name="warmup",
                output_root=root,
                generate_plots=False,
                input_validation=input_validation,
                persist_artifacts=False,
            )
        for index in range(repeats):
            result = run_backtest(
                effective,
                events,
                run_name=f"timed-{index:03d}",
                output_root=root,
                generate_plots=False,
                input_validation=input_validation,
                persist_artifacts=False,
            )
            elapsed = float(result.diagnostics["wall_clock_seconds"])
            processed = int(result.diagnostics["events_processed"])
            scheduled = int(result.diagnostics["scheduler_events_processed"])
            processed_values.append(processed)
            elapsed_values.append(elapsed)
            throughput_values.append(
                processed / elapsed if elapsed > 0 else float("inf")
            )
            scheduler_ratios.append(scheduled / processed)

        traced_peak: int | None = None
        if measure_memory:
            memory_config = _benchmark_config(
                effective,
                event_count=event_count,
                track_memory=True,
            )
            memory_result = run_backtest(
                memory_config,
                events,
                run_name="memory-profile",
                output_root=root,
                generate_plots=False,
                input_validation=input_validation,
                persist_artifacts=False,
            )
            peak = memory_result.diagnostics.get("peak_memory_bytes")
            traced_peak = int(peak) if peak is not None else None

    return FullBenchmarkResult(
        event_count=int(median(processed_values)),
        repeat_count=repeats,
        elapsed_seconds=tuple(elapsed_values),
        events_per_second=tuple(throughput_values),
        median_elapsed_seconds=median(elapsed_values),
        p95_elapsed_seconds=_percentile(elapsed_values, 0.95),
        median_events_per_second=median(throughput_values),
        p05_events_per_second=_percentile(throughput_values, 0.05),
        median_scheduler_events_per_market_event=median(scheduler_ratios),
        traced_peak_memory_bytes=traced_peak,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        random_seed=effective.random_seed,
        dataset=dataset,
        event_stream_sha256=stream_hash,
        timing_scope=(
            "Unprofiled scheduler/exchange/strategy event loop only; metrics, "
            "Parquet serialization, and plots are outside the timed interval. "
            "The traced-memory pass is separate from timed repeats."
        ),
    )


def _configured_events(config: AppConfig) -> tuple[list[MarketEvent], str]:
    input_path = config.data.input_path
    if input_path is None:
        events = generate_synthetic_events(config)
    else:
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(f"configured benchmark data does not exist: {path}")
        expected = config.data.provenance.checksum_sha256
        if expected is not None:
            observed = sha256_file(path)
            if observed != expected:
                raise ValueError(
                    "configured dataset checksum mismatch: "
                    f"expected {expected}, observed {observed}"
                )
        events = load_events(path)
    validate_event_stream(events, mode=config.data.validation_mode)
    events = _selected_events(config, events)
    dataset = (
        f"{config.data.provenance.provider}/"
        f"{config.data.provenance.dataset_id}/"
        f"{config.data.provenance.symbol}"
    )
    return events, dataset


def _selected_events(
    config: AppConfig,
    events: list[MarketEvent],
) -> list[MarketEvent]:
    selected = [
        event
        for event in events
        if (
            config.backtest.start_timestamp_ns is None
            or event.timestamp_ns >= config.backtest.start_timestamp_ns
        )
        and (
            config.backtest.end_timestamp_ns is None
            or event.timestamp_ns <= config.backtest.end_timestamp_ns
        )
    ]
    if config.backtest.event_limit is not None:
        selected = selected[: config.backtest.event_limit]
    if not selected:
        raise ValueError("no events remain after benchmark filters")
    return selected


def _benchmark_config(
    config: AppConfig,
    *,
    event_count: int | None,
    track_memory: bool,
) -> AppConfig:
    synthetic = config.synthetic
    backtest = config.backtest
    if event_count is not None:
        if config.data.input_path is None:
            synthetic = synthetic.model_copy(update={"event_count": event_count})
            backtest = backtest.model_copy(update={"event_limit": None})
        else:
            backtest = backtest.model_copy(update={"event_limit": event_count})
    backtest = backtest.model_copy(update={"track_memory": track_memory})
    return config.model_copy(
        update={
            "synthetic": synthetic,
            "backtest": backtest,
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(values)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return ordered[index]
