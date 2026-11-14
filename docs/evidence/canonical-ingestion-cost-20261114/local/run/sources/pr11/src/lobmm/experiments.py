"""Reproducible multi-run sensitivity and strategy-ablation experiments."""

from __future__ import annotations

import itertools
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from typing import Any

import polars as pl

from lobmm.backtest import BacktestResult, _select_events, run_backtest
from lobmm.config import AppConfig
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.enums import QueueAllocation, StrategyName
from lobmm.events import MarketEvent
from lobmm.validation import validate_event_stream


class ExperimentError(ValueError):
    """Raised when an experiment specification is invalid."""


class StrategyAblation(StrEnum):
    """Signals that can be removed without changing the strategy implementation."""

    FULL = "full"
    NO_INVENTORY = "no_inventory"
    NO_EXPLICIT_IMBALANCE = "no_explicit_imbalance"
    NO_VOLATILITY_WIDENING = "no_volatility_widening"


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """Cartesian experiment grid applied to one immutable event stream."""

    strategies: tuple[StrategyName, ...]
    latency_multipliers: tuple[float, ...] = (0.5, 1.0, 2.0)
    queue_allocations: tuple[QueueAllocation, ...] = (
        QueueAllocation.BACK_OF_QUEUE,
        QueueAllocation.PRO_RATA,
        QueueAllocation.FRONT_OF_QUEUE,
    )
    fee_multipliers: tuple[Decimal, ...] = (Decimal("1"),)
    ablations: tuple[StrategyAblation, ...] = (StrategyAblation.FULL,)

    def __post_init__(self) -> None:
        if not self.strategies:
            raise ExperimentError("at least one strategy is required")
        if not self.latency_multipliers:
            raise ExperimentError("at least one latency multiplier is required")
        if any(
            not math.isfinite(value) or value < 0 for value in self.latency_multipliers
        ):
            raise ExperimentError("latency multipliers must be finite and nonnegative")
        if not self.queue_allocations:
            raise ExperimentError("at least one queue allocation is required")
        if not self.fee_multipliers:
            raise ExperimentError("at least one fee multiplier is required")
        if any(not value.is_finite() or value < 0 for value in self.fee_multipliers):
            raise ExperimentError("fee multipliers must be finite and nonnegative")
        if not self.ablations:
            raise ExperimentError("at least one ablation is required")
        dimensions = {
            "strategies": self.strategies,
            "latency multipliers": self.latency_multipliers,
            "queue allocations": self.queue_allocations,
            "fee multipliers": self.fee_multipliers,
            "ablations": self.ablations,
        }
        for label, values in dimensions.items():
            if len(set(values)) != len(values):
                raise ExperimentError(f"{label} must not contain duplicates")

    @property
    def case_count(self) -> int:
        return (
            len(self.strategies)
            * len(self.latency_multipliers)
            * len(self.queue_allocations)
            * len(self.fee_multipliers)
            * len(self.ablations)
        )


@dataclass(frozen=True, slots=True)
class ExperimentCase:
    strategy: StrategyName
    latency_multiplier: float
    queue_allocation: QueueAllocation
    fee_multiplier: Decimal
    ablation: StrategyAblation

    @property
    def run_name(self) -> str:
        return (
            f"{self.strategy.value}__{self.ablation.value}"
            f"__lat{_number_slug(self.latency_multiplier)}"
            f"__{self.queue_allocation.value}"
            f"__fee{_number_slug(self.fee_multiplier)}"
        )


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    experiment_directory: Path
    cases: tuple[ExperimentCase, ...]
    results: tuple[BacktestResult, ...]
    table: pl.DataFrame
    event_stream_sha256: str


def iter_cases(spec: ExperimentSpec) -> tuple[ExperimentCase, ...]:
    """Expand a specification in stable, inspectable order."""

    cases = tuple(
        ExperimentCase(strategy, latency, queue, fee, ablation)
        for strategy, latency, queue, fee, ablation in itertools.product(
            spec.strategies,
            spec.latency_multipliers,
            spec.queue_allocations,
            spec.fee_multipliers,
            spec.ablations,
        )
    )
    if len({case.run_name for case in cases}) != len(cases):
        raise ExperimentError("experiment cases must have unique run names")
    return cases


def _number_slug(value: float | Decimal) -> str:
    """Encode an exact configured multiplier as a filesystem-safe token."""

    normalized = format(Decimal(str(value)).normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized.replace("-", "m").replace(".", "p")


def config_for_case(
    base: AppConfig,
    case: ExperimentCase,
    *,
    runs_directory: Path,
) -> AppConfig:
    """Return an isolated validated configuration for one experiment case."""

    latency_updates = {
        name: _scaled_ns(getattr(base.latency, name), case.latency_multiplier)
        for name in (
            "market_data_ns",
            "order_entry_ns",
            "cancellation_ns",
            "fill_report_ns",
            "market_data_jitter_ns",
            "order_entry_jitter_ns",
            "cancellation_jitter_ns",
            "fill_report_jitter_ns",
        )
    }
    fee_updates = {
        name: getattr(base.fees, name) * case.fee_multiplier
        for name in (
            "maker_fee_per_unit",
            "maker_rebate_per_unit",
            "taker_fee_per_unit",
            "proportional_fee_rate",
        )
    }
    strategy_updates: dict[str, Any] = {"name": case.strategy}
    if case.ablation is StrategyAblation.NO_INVENTORY:
        strategy_updates["inventory_penalty_ticks"] = 0.0
    elif case.ablation is StrategyAblation.NO_EXPLICIT_IMBALANCE:
        strategy_updates["imbalance_coefficient_ticks"] = 0.0
    elif case.ablation is StrategyAblation.NO_VOLATILITY_WIDENING:
        strategy_updates["volatility_multiplier"] = 0.0

    return base.model_copy(
        update={
            "latency": base.latency.model_copy(update=latency_updates),
            "fees": base.fees.model_copy(update=fee_updates),
            "queue_model": base.queue_model.model_copy(
                update={"cancellation_allocation": case.queue_allocation}
            ),
            "strategy": base.strategy.model_copy(update=strategy_updates),
            "output": base.output.model_copy(
                update={"runs_directory": runs_directory, "write_plots": False}
            ),
        }
    )


def run_experiment(
    base: AppConfig,
    events: list[MarketEvent],
    *,
    name: str,
    spec: ExperimentSpec,
    output_root: str | Path = "experiments",
) -> ExperimentResult:
    """Execute and persist a complete deterministic sensitivity grid."""

    if not events:
        raise ExperimentError("cannot run an experiment on an empty event stream")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ExperimentError("experiment name must be one safe directory name")

    cases = iter_cases(spec)
    experiment_directory = Path(output_root) / name
    runs_directory = experiment_directory / "runs"
    runs_directory.mkdir(parents=True, exist_ok=True)
    results: list[BacktestResult] = []
    rows: list[dict[str, Any]] = []
    input_validation = validate_event_stream(
        events,
        mode=base.data.validation_mode,
    )
    selected_events = _select_events(base, events)
    if not selected_events:
        raise ExperimentError("no events remain after experiment filters")

    for case in cases:
        config = config_for_case(base, case, runs_directory=runs_directory)
        result = run_backtest(
            config,
            events,
            run_name=case.run_name,
            output_root=runs_directory,
            generate_plots=False,
            input_validation=input_validation,
        )
        results.append(result)
        rows.append(_result_row(case, config, result))

    table = pl.DataFrame(rows, infer_schema_length=None)
    stream_hash = event_stream_sha256(selected_events)
    experiment_directory.mkdir(parents=True, exist_ok=True)
    table.write_parquet(experiment_directory / "sensitivity.parquet")
    table.write_csv(experiment_directory / "sensitivity.csv")
    synthetic = all(
        bool(value) for value in table.get_column("synthetic_demonstration").to_list()
    )
    overview_path, report_path = _write_visual_report(
        table,
        experiment_directory,
        synthetic_demonstration=synthetic,
    )
    manifest = {
        "name": name,
        "case_count": len(cases),
        "event_count": len(selected_events),
        "event_stream_sha256": stream_hash,
        "synthetic_demonstration": synthetic,
        "artifacts": {
            "table_parquet": "sensitivity.parquet",
            "table_csv": "sensitivity.csv",
            "overview_plot": overview_path.name,
            "report": report_path.name,
        },
        "spec": {
            "strategies": [value.value for value in spec.strategies],
            "latency_multipliers": list(spec.latency_multipliers),
            "queue_allocations": [value.value for value in spec.queue_allocations],
            "fee_multipliers": [str(value) for value in spec.fee_multipliers],
            "ablations": [value.value for value in spec.ablations],
        },
        "aggregate": _aggregate(rows),
    }
    (experiment_directory / "experiment.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ExperimentResult(
        experiment_directory=experiment_directory,
        cases=cases,
        results=tuple(results),
        table=table,
        event_stream_sha256=stream_hash,
    )


def _scaled_ns(value: int, multiplier: float) -> int:
    return max(0, round(value * multiplier))


def _metric(
    result: BacktestResult,
    section: str,
    key: str,
    default: Any = None,
) -> Any:
    nested = result.metrics.get(section)
    return nested.get(key, default) if isinstance(nested, dict) else default


def _result_row(
    case: ExperimentCase,
    config: AppConfig,
    result: BacktestResult,
) -> dict[str, Any]:
    return {
        "run_name": case.run_name,
        "strategy": case.strategy.value,
        "ablation": case.ablation.value,
        "latency_multiplier": case.latency_multiplier,
        "market_data_latency_ns": config.latency.market_data_ns,
        "order_entry_latency_ns": config.latency.order_entry_ns,
        "cancellation_latency_ns": config.latency.cancellation_ns,
        "fill_report_latency_ns": config.latency.fill_report_ns,
        "queue_allocation": case.queue_allocation.value,
        "fee_multiplier": float(case.fee_multiplier),
        "events_processed": result.diagnostics.get("events_processed"),
        "scheduler_events_processed": result.diagnostics.get(
            "scheduler_events_processed"
        ),
        "events_per_second": result.diagnostics.get("events_per_second"),
        "risk_event_count": _metric(result, "risk", "event_count", 0),
        "fill_events": _metric(result, "trading_activity", "fill_events", 0),
        "fill_rate": _metric(result, "trading_activity", "fill_rate"),
        "net_pnl": _metric(result, "pnl", "net_pnl"),
        "maximum_drawdown": _metric(result, "pnl", "maximum_drawdown"),
        "end_inventory": _metric(result, "inventory", "end_of_session"),
        "mean_absolute_inventory": _metric(result, "inventory", "mean_absolute"),
        "time_near_limits_fraction": _metric(
            result, "inventory", "time_near_limits_fraction"
        ),
        "realized_spread_ticks": _metric(
            result, "execution_quality", "realized_spread_ticks"
        ),
        "synthetic_demonstration": result.diagnostics.get("synthetic_demonstration"),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    numeric = {
        key: [
            float(row[key])
            for row in rows
            if isinstance(row.get(key), (int, float))
            and not isinstance(row.get(key), bool)
        ]
        for key in (
            "events_per_second",
            "fill_rate",
            "net_pnl",
            "maximum_drawdown",
            "mean_absolute_inventory",
        )
    }
    return {
        key: {
            "mean": fmean(values),
            "median": median(values),
            "minimum": min(values),
            "maximum": max(values),
        }
        for key, values in numeric.items()
        if values
    }


def _write_visual_report(
    table: pl.DataFrame,
    output_directory: Path,
    *,
    synthetic_demonstration: bool,
) -> tuple[Path, Path]:
    """Write a compact four-panel diagnostic and a GitHub-readable report."""

    cache = Path(tempfile.gettempdir()) / "lobmm-matplotlib"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = table.to_dicts()
    grouped: dict[tuple[str, str, float, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row["strategy"]),
            str(row["queue_allocation"]),
            float(row["fee_multiplier"]),
            str(row["ablation"]),
        )
        grouped.setdefault(key, []).append(row)

    figure, axes = plt.subplots(2, 2, figsize=(13, 9), dpi=130)
    panels = (
        ("maximum_drawdown", "Maximum drawdown", axes[0, 0]),
        ("mean_absolute_inventory", "Mean absolute inventory", axes[0, 1]),
        ("fill_rate", "Fill rate", axes[1, 0]),
        ("risk_event_count", "Risk events", axes[1, 1]),
    )
    for key, values in sorted(grouped.items()):
        values.sort(key=lambda row: float(row["latency_multiplier"]))
        label = f"{key[0]} · {key[1]} · fee x {key[2]:g} · {key[3]}"
        x = [float(row["order_entry_latency_ns"]) / 1_000.0 for row in values]
        for metric, _title, axis in panels:
            y = [
                float(row[metric]) if row.get(metric) is not None else float("nan")
                for row in values
            ]
            axis.plot(x, y, marker="o", linewidth=1.2, label=label)
    for _metric, title, axis in panels:
        axis.set_title(title)
        axis.set_xlabel("Order-entry latency (µs)")
        axis.grid(alpha=0.25)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    if handles:
        figure.legend(
            handles,
            labels,
            loc="lower center",
            ncol=min(3, len(labels)),
            fontsize=7,
        )
    qualifier = (
        "SYNTHETIC DEMONSTRATION — engineering diagnostics, not expected returns"
        if synthetic_demonstration
        else "HISTORICAL RESEARCH — conditional on replay and queue assumptions"
    )
    figure.suptitle(f"Sensitivity overview\n{qualifier}", fontsize=12)
    figure.tight_layout(rect=(0, 0.1, 1, 0.94))
    overview_path = output_directory / "sensitivity_overview.png"
    figure.savefig(overview_path, bbox_inches="tight")
    plt.close(figure)

    report_path = output_directory / "REPORT.md"
    report_path.write_text(
        _markdown_report(
            rows,
            overview_filename=overview_path.name,
            synthetic_demonstration=synthetic_demonstration,
        ),
        encoding="utf-8",
    )
    return overview_path, report_path


def _markdown_report(
    rows: list[dict[str, Any]],
    *,
    overview_filename: str,
    synthetic_demonstration: bool,
) -> str:
    label = (
        "Synthetic demonstration"
        if synthetic_demonstration
        else "Historical replay research"
    )
    disclaimer = (
        "These results validate mechanics and sensitivity behavior; they are not "
        "evidence of real-world profitability."
        if synthetic_demonstration
        else "Results remain conditional on the recorded path, latency, fees, and "
        "queue assumptions; they are not predictions or trading advice."
    )
    lines = [
        "# Sensitivity experiment",
        "",
        f"**Classification:** {label}",
        "",
        disclaimer,
        "",
        f"![Sensitivity overview]({overview_filename})",
        "",
        "## Cases",
        "",
        "| Strategy | Ablation | Latency x | Queue model | Fee x | Fill rate | "
        "Mean abs. inventory | Max drawdown | Net P&L |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row["strategy"]),
            str(row["ablation"]),
            float(row["latency_multiplier"]),
            str(row["queue_allocation"]),
            float(row["fee_multiplier"]),
        ),
    )
    for row in ordered[:100]:
        lines.append(
            "| {strategy} | {ablation} | {latency_multiplier:g} | "
            "{queue_allocation} | {fee_multiplier:g} | {fill_rate:.4f} | "
            "{mean_absolute_inventory:.3f} | {maximum_drawdown:.4f} | "
            "{net_pnl:.4f} |".format(**row)
        )
    if len(ordered) > 100:
        lines.extend(
            (
                "",
                f"Table truncated to 100 of {len(ordered)} cases; see "
                "`sensitivity.csv` for the complete grid.",
            )
        )
    lines.extend(
        (
            "",
            "The complete table is available as `sensitivity.csv` and "
            "`sensitivity.parquet`. `experiment.json` records the exact grid and "
            "SHA-256 fingerprint of the canonical event stream.",
            "",
        )
    )
    return "\n".join(lines)


def spec_as_dict(spec: ExperimentSpec) -> dict[str, Any]:
    """Return a JSON-safe representation useful to API callers."""

    raw = asdict(spec)
    return {
        "strategies": [value.value for value in raw["strategies"]],
        "latency_multipliers": list(raw["latency_multipliers"]),
        "queue_allocations": [value.value for value in raw["queue_allocations"]],
        "fee_multipliers": [str(value) for value in raw["fee_multipliers"]],
        "ablations": [value.value for value in raw["ablations"]],
    }
