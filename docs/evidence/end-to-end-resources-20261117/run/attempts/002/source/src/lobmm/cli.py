"""Typer command-line interface for data, backtests, reports, and benchmarks."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Annotated, cast

import polars as pl
import typer

from lobmm.backtest import run_backtest
from lobmm.benchmark import run_benchmark, run_full_benchmark
from lobmm.case_study import publish_case_study
from lobmm.config import (
    AppConfig,
    DatasetProvenanceConfig,
    DatasetSourceType,
    load_config,
)
from lobmm.data import (
    LOBSTERCSVAdapter,
    LOBSTERData,
    TardisCSVAdapter,
    TardisData,
    TardisTimestampSource,
    sha256_file,
    write_dataset_manifest,
)
from lobmm.data.loaders import load_events, write_parquet
from lobmm.enums import QueueAllocation, StrategyName, ValidationMode
from lobmm.events import MarketEvent
from lobmm.experiments import ExperimentSpec, StrategyAblation, run_experiment
from lobmm.report import compare_runs, console_summary, generate_report
from lobmm.synthetic import generate_synthetic_events
from lobmm.validation import validate_event_stream, validate_frame

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Historical Level 2 market-making research simulator. "
        "No live trading or exchange connectivity."
    ),
)


def _failure(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from None


def _csv_values(value: str, *, option: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise ValueError(f"{option} must contain at least one value")
    return values


def _persist_historical_conversion(
    frame: pl.DataFrame,
    *,
    output: Path,
    provider: str,
    venue: str,
    symbol: str,
    dataset_id: str,
    session_start: str,
    session_end: str | None,
    license_notes: str,
    adapter_name: str,
    adapter_parameters: Mapping[str, object],
    source_paths: Sequence[Path],
    adapter_diagnostics: Mapping[str, int],
    manifest_path: Path | None,
) -> dict[str, object]:
    validation = validate_frame(frame)
    canonical_path = write_parquet(frame, output)
    checksum = sha256_file(canonical_path)
    provenance = DatasetProvenanceConfig(
        source_type=DatasetSourceType.HISTORICAL,
        provider=provider,
        venue=venue,
        symbol=symbol,
        dataset_id=dataset_id,
        session_start=session_start,
        session_end=session_end or session_start,
        checksum_sha256=checksum,
        license_notes=license_notes,
    )
    manifest = write_dataset_manifest(
        canonical_path,
        provenance=provenance,
        adapter_name=adapter_name,
        adapter_parameters=adapter_parameters,
        source_paths=source_paths,
        event_count=validation.event_count,
        adapter_diagnostics=adapter_diagnostics,
        validation_diagnostics=validation.diagnostics,
        manifest_path=manifest_path,
    )
    return {
        "canonical_path": str(canonical_path),
        "manifest_path": str(manifest.manifest_path.resolve()),
        "event_count": validation.event_count,
        "canonical_sha256": checksum,
        "provenance": provenance.artifact(),
        "adapter_diagnostics": dict(adapter_diagnostics),
    }


def _configured_events(config: AppConfig) -> list[MarketEvent]:
    if config.data.input_path is None:
        return list(generate_synthetic_events(config))
    path = config.data.input_path
    expected = config.data.provenance.checksum_sha256
    if expected is not None:
        observed = sha256_file(path)
        if observed != expected:
            raise ValueError(
                "configured dataset checksum mismatch: "
                f"expected {expected}, observed {observed}"
            )
    return list(load_events(path))


@app.command("generate-synthetic")
def generate_synthetic_command(
    config_path: Annotated[Path, typer.Option("--config", exists=True, readable=True)],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Generate deterministic canonical synthetic Level 2 data."""

    try:
        config = load_config(config_path)
        events = generate_synthetic_events(config)
        path = write_parquet(events, output)
        typer.echo(
            f"Wrote {len(events):,} canonical events to {path}\n"
            "Synthetic data is a functional demonstration, not evidence of "
            "real-world profitability."
        )
    except Exception as exc:
        _failure(exc)


@app.command("ingest-tardis")
def ingest_tardis_command(
    book: Annotated[Path, typer.Option("--book", exists=True, readable=True)],
    output: Annotated[Path, typer.Option("--output")],
    tick_size: Annotated[str, typer.Option("--tick-size")],
    venue: Annotated[str, typer.Option("--venue")],
    symbol: Annotated[str, typer.Option("--symbol")],
    dataset_id: Annotated[str, typer.Option("--dataset-id")],
    session_start: Annotated[str, typer.Option("--session-start")],
    license_notes: Annotated[str, typer.Option("--license-notes")],
    trades: Annotated[
        Path | None,
        typer.Option("--trades", exists=True, readable=True),
    ] = None,
    provider: Annotated[str, typer.Option("--provider")] = "Tardis.dev",
    session_end: Annotated[str | None, typer.Option("--session-end")] = None,
    quantity_multiplier: Annotated[
        str,
        typer.Option("--quantity-multiplier"),
    ] = "1",
    timestamp_source: Annotated[
        str,
        typer.Option("--timestamp-source"),
    ] = "local_timestamp",
    trade_match_window_ns: Annotated[
        int,
        typer.Option("--trade-match-window-ns", min=0),
    ] = 1_000_000,
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Optional sidecar destination."),
    ] = None,
) -> None:
    """Convert Tardis incremental L2/trade CSV files to canonical Parquet."""

    try:
        adapter = TardisCSVAdapter(
            tick_size=tick_size,
            quantity_multiplier=quantity_multiplier,
            timestamp_source=cast(TardisTimestampSource, timestamp_source),
            trade_match_window_ns=trade_match_window_ns,
        )
        frame = adapter.checked(TardisData(book=book, trades=trades))
        instrument = adapter.last_instrument
        if instrument is None:
            raise ValueError("Tardis adapter did not identify an instrument")
        if (
            instrument[0].casefold() != venue.casefold()
            or instrument[1].casefold() != symbol.casefold()
        ):
            raise ValueError(
                "declared venue/symbol does not match Tardis data: "
                f"{(venue, symbol)!r} != {instrument!r}"
            )
        document = _persist_historical_conversion(
            frame,
            output=output,
            provider=provider,
            venue=venue,
            symbol=symbol,
            dataset_id=dataset_id,
            session_start=session_start,
            session_end=session_end,
            license_notes=license_notes,
            adapter_name=adapter.name,
            adapter_parameters={
                "tick_size": tick_size,
                "quantity_multiplier": quantity_multiplier,
                "timestamp_source": timestamp_source,
                "trade_match_window_ns": trade_match_window_ns,
            },
            source_paths=tuple(path for path in (book, trades) if path is not None),
            adapter_diagnostics=adapter.last_diagnostics,
            manifest_path=manifest,
        )
        typer.echo(json.dumps(document, indent=2, sort_keys=True))
    except Exception as exc:
        _failure(exc)


@app.command("ingest-lobster")
def ingest_lobster_command(
    messages: Annotated[Path, typer.Option("--messages", exists=True, readable=True)],
    orderbook: Annotated[
        Path,
        typer.Option("--orderbook", exists=True, readable=True),
    ],
    output: Annotated[Path, typer.Option("--output")],
    tick_size: Annotated[str, typer.Option("--tick-size")],
    symbol: Annotated[str, typer.Option("--symbol")],
    dataset_id: Annotated[str, typer.Option("--dataset-id")],
    trading_date: Annotated[str, typer.Option("--trading-date")],
    levels: Annotated[int, typer.Option("--levels", min=1)],
    license_notes: Annotated[str, typer.Option("--license-notes")],
    provider: Annotated[str, typer.Option("--provider")] = "LOBSTER",
    venue: Annotated[str, typer.Option("--venue")] = "NASDAQ",
    timezone: Annotated[str, typer.Option("--timezone")] = "America/New_York",
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Optional sidecar destination."),
    ] = None,
) -> None:
    """Convert paired LOBSTER message/order-book CSV files to canonical Parquet."""

    try:
        adapter = LOBSTERCSVAdapter(
            tick_size=tick_size,
            trading_date=trading_date,
            levels=levels,
            timezone=timezone,
        )
        frame = adapter.checked(LOBSTERData(messages=messages, orderbook=orderbook))
        document = _persist_historical_conversion(
            frame,
            output=output,
            provider=provider,
            venue=venue,
            symbol=symbol,
            dataset_id=dataset_id,
            session_start=trading_date,
            session_end=trading_date,
            license_notes=license_notes,
            adapter_name=adapter.name,
            adapter_parameters={
                "tick_size": tick_size,
                "trading_date": trading_date,
                "levels": levels,
                "timezone": timezone,
            },
            source_paths=(messages, orderbook),
            adapter_diagnostics=adapter.last_diagnostics,
            manifest_path=manifest,
        )
        typer.echo(json.dumps(document, indent=2, sort_keys=True))
    except Exception as exc:
        _failure(exc)


@app.command("validate-data")
def validate_data_command(
    input_path: Annotated[Path, typer.Option("--input", exists=True, readable=True)],
    lenient: Annotated[
        bool,
        typer.Option(
            "--lenient",
            help="Record and clamp repairable depth errors instead of failing.",
        ),
    ] = False,
) -> None:
    """Validate schema, order, depth, and uncrossed-book invariants."""

    try:
        events = load_events(input_path)
        mode = ValidationMode.LENIENT if lenient else ValidationMode.STRICT
        result = validate_event_stream(events, mode=mode)
        typer.echo(
            json.dumps(
                {
                    "valid": result.valid,
                    "event_count": result.event_count,
                    "event_stream_sha256": result.event_stream_sha256,
                    "validation_mode": result.mode.value,
                    "issue_count": result.issue_count,
                    "diagnostics": dict(result.diagnostics),
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not result.valid and not lenient:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        _failure(exc)


@app.command("backtest")
def backtest_command(
    config_path: Annotated[Path, typer.Option("--config", exists=True, readable=True)],
    run_name: Annotated[str, typer.Option("--run-name")],
) -> None:
    """Run one configured historical or synthetic replay."""

    try:
        config = load_config(config_path)
        events = _configured_events(config)
        result = run_backtest(config, events, run_name=run_name)
        typer.echo(console_summary(result.run_directory))
        typer.echo(f"Run artifacts: {result.run_directory.resolve()}")
    except Exception as exc:
        _failure(exc)


@app.command("compare")
def compare_command(
    run_directories: Annotated[
        list[Path],
        typer.Argument(
            help="Two or more completed run directories using the same stream."
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Comparison artifact directory."),
    ] = None,
) -> None:
    """Compare completed strategy runs without inventing performance claims."""

    try:
        if len(run_directories) < 2:
            raise ValueError("compare requires at least two run directories")
        frame = compare_runs(run_directories, output_directory=output)
        typer.echo(frame.write_csv())
    except Exception as exc:
        _failure(exc)


@app.command("report")
def report_command(
    run_directory: Annotated[Path, typer.Option("--run", exists=True, file_okay=False)],
) -> None:
    """Regenerate JSON/table/plot presentation from one valid run."""

    try:
        report_result = generate_report(run_directory)
        typer.echo(console_summary(run_directory))
        typer.echo(
            f"Generated {len(report_result.plot_paths)} plot files under "
            f"{report_result.plots_directory}"
        )
    except Exception as exc:
        _failure(exc)


@app.command("benchmark")
def benchmark_command(
    config_path: Annotated[Path, typer.Option("--config", exists=True, readable=True)],
) -> None:
    """Measure reference replay throughput; values are never hardcoded."""

    try:
        result = run_benchmark(load_config(config_path))
        typer.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    except Exception as exc:
        _failure(exc)


@app.command("benchmark-full")
def benchmark_full_command(
    config_path: Annotated[Path, typer.Option("--config", exists=True, readable=True)],
    repeats: Annotated[
        int,
        typer.Option("--repeats", min=1, help="Number of unprofiled timed repeats."),
    ] = 5,
    event_count: Annotated[
        int | None,
        typer.Option(
            "--event-count",
            min=1,
            help="Synthetic size or maximum historical events to replay.",
        ),
    ] = None,
    no_warmup: Annotated[
        bool,
        typer.Option("--no-warmup", help="Skip the untimed warmup run."),
    ] = False,
    no_memory: Annotated[
        bool,
        typer.Option("--no-memory", help="Skip the separate traced-memory pass."),
    ] = False,
) -> None:
    """Measure the complete causal event loop with repeated unprofiled runs."""

    try:
        result = run_full_benchmark(
            load_config(config_path),
            repeats=repeats,
            event_count=event_count,
            warmup=not no_warmup,
            measure_memory=not no_memory,
        )
        typer.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    except Exception as exc:
        _failure(exc)


@app.command("experiment")
def experiment_command(
    config_path: Annotated[Path, typer.Option("--config", exists=True, readable=True)],
    name: Annotated[str, typer.Option("--name")],
    strategies: Annotated[
        str | None,
        typer.Option(
            "--strategies",
            help="Comma-separated strategy names; defaults to the configured strategy.",
        ),
    ] = None,
    latency_multipliers: Annotated[
        str,
        typer.Option(
            "--latency-multipliers",
            help="Comma-separated multipliers applied to every latency channel.",
        ),
    ] = "0.5,1,2",
    queue_allocations: Annotated[
        str,
        typer.Option(
            "--queue-allocations",
            help="Comma-separated queue cancellation-allocation assumptions.",
        ),
    ] = "back_of_queue,pro_rata,front_of_queue",
    fee_multipliers: Annotated[
        str,
        typer.Option("--fee-multipliers", help="Comma-separated cost multipliers."),
    ] = "1",
    ablations: Annotated[
        str,
        typer.Option(
            "--ablations",
            help=(
                "Comma-separated signal cases: full, no_inventory, "
                "no_explicit_imbalance, no_volatility_widening."
            ),
        ),
    ] = "full",
    output_root: Annotated[
        Path,
        typer.Option("--output-root", help="Experiment artifact root directory."),
    ] = Path("experiments"),
    allow_large_grid: Annotated[
        bool,
        typer.Option(
            "--allow-large-grid",
            help="Allow more than 100 backtest cases in one invocation.",
        ),
    ] = False,
) -> None:
    """Run a reproducible latency, queue, fee, and signal sensitivity grid."""

    try:
        config = load_config(config_path)
        events = _configured_events(config)
        selected_strategies = (
            (config.strategy.name,)
            if strategies is None
            else tuple(
                StrategyName(value)
                for value in _csv_values(strategies, option="--strategies")
            )
        )
        spec = ExperimentSpec(
            strategies=selected_strategies,
            latency_multipliers=tuple(
                float(value)
                for value in _csv_values(
                    latency_multipliers,
                    option="--latency-multipliers",
                )
            ),
            queue_allocations=tuple(
                QueueAllocation(value)
                for value in _csv_values(
                    queue_allocations,
                    option="--queue-allocations",
                )
            ),
            fee_multipliers=tuple(
                Decimal(value)
                for value in _csv_values(
                    fee_multipliers,
                    option="--fee-multipliers",
                )
            ),
            ablations=tuple(
                StrategyAblation(value)
                for value in _csv_values(ablations, option="--ablations")
            ),
        )
        if spec.case_count > 100 and not allow_large_grid:
            raise ValueError(
                f"experiment expands to {spec.case_count} cases; reduce the grid "
                "or pass --allow-large-grid"
            )
        result = run_experiment(
            config,
            events,
            name=name,
            spec=spec,
            output_root=output_root,
        )
        typer.echo(
            json.dumps(
                {
                    "case_count": len(result.cases),
                    "event_stream_sha256": result.event_stream_sha256,
                    "experiment_directory": str(result.experiment_directory.resolve()),
                },
                indent=2,
                sort_keys=True,
            )
        )
    except Exception as exc:
        _failure(exc)


@app.command("demo")
def demo_command(
    output_root: Annotated[
        Path,
        typer.Option("--output-root", help="Backtest and comparison artifact root."),
    ] = Path("runs"),
    experiment_root: Annotated[
        Path,
        typer.Option("--experiment-root", help="Sensitivity artifact root."),
    ] = Path("experiments"),
    publish: Annotated[
        bool,
        typer.Option(
            "--publish-case-study",
            help="Refresh the curated GitHub-readable case study under docs.",
        ),
    ] = False,
    skip_sensitivity: Annotated[
        bool,
        typer.Option(
            "--skip-sensitivity",
            help="Skip the nine-case latency/queue experiment.",
        ),
    ] = False,
) -> None:
    """Run the complete deterministic portfolio demonstration in one command."""

    try:
        synthetic_config = load_config(Path("configs/synthetic.yaml"))
        events = generate_synthetic_events(synthetic_config)
        canonical_path = write_parquet(
            events,
            Path("data/processed/synthetic.parquet"),
        )
        validate_event_stream(events, mode=synthetic_config.data.validation_mode)

        run_specs = (
            ("fixed-spread-demo", Path("configs/fixed_spread.yaml")),
            ("inventory-demo", Path("configs/inventory_aware.yaml")),
            ("microprice-demo", Path("configs/microprice.yaml")),
        )
        run_directories: list[Path] = []
        loaded_configs: dict[str, AppConfig] = {}
        for run_name, config_path in run_specs:
            config = load_config(config_path)
            result = run_backtest(
                config,
                events,
                run_name=run_name,
                output_root=output_root,
            )
            run_directories.append(result.run_directory)
            loaded_configs[run_name] = config

        comparison_directory = output_root / "comparison"
        compare_runs(run_directories, output_directory=comparison_directory)

        experiment_directory: Path | None = None
        if not skip_sensitivity:
            microprice_config = loaded_configs["microprice-demo"]
            experiment = run_experiment(
                microprice_config,
                events,
                name="microprice-sensitivity",
                spec=ExperimentSpec(strategies=(microprice_config.strategy.name,)),
                output_root=experiment_root,
            )
            experiment_directory = experiment.experiment_directory

        case_study_path: str | None = None
        if publish:
            study = publish_case_study(
                run_directories,
                comparison_directory=comparison_directory,
                experiment_directory=experiment_directory,
            )
            case_study_path = str(study.markdown_path.resolve())

        typer.echo(
            json.dumps(
                {
                    "canonical_data": str(canonical_path),
                    "runs": [str(path.resolve()) for path in run_directories],
                    "comparison": str(comparison_directory.resolve()),
                    "experiment": (
                        str(experiment_directory.resolve())
                        if experiment_directory is not None
                        else None
                    ),
                    "case_study": case_study_path,
                },
                indent=2,
                sort_keys=True,
            )
        )
    except Exception as exc:
        _failure(exc)


if __name__ == "__main__":
    app()
