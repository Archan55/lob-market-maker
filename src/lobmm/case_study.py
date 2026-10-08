"""Publish a small, honest, GitHub-readable demonstration from run artifacts."""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

import polars as pl

from lobmm.report import (
    RunArtifacts,
    compare_runs,
    generate_report,
    load_run_artifacts,
)


class CaseStudyError(ValueError):
    """Raised when run artifacts cannot support a consistent case study."""


@dataclass(frozen=True, slots=True)
class CaseStudyResult:
    markdown_path: Path
    assets_directory: Path
    asset_paths: tuple[Path, ...]


_REQUIRED_STRATEGIES = {"fixed_spread", "inventory_aware", "microprice"}
_REQUIRED_EXPERIMENT_COLUMNS = {
    "run_name",
    "strategy",
    "ablation",
    "latency_multiplier",
    "order_entry_latency_ns",
    "queue_allocation",
    "fee_multiplier",
    "events_processed",
    "risk_event_count",
    "fill_rate",
    "net_pnl",
    "maximum_drawdown",
    "mean_absolute_inventory",
    "synthetic_demonstration",
}


def publish_case_study(
    run_directories: Sequence[str | Path],
    *,
    comparison_directory: str | Path,
    assets_directory: str | Path = "docs/assets/case-study",
    markdown_path: str | Path = "docs/CASE_STUDY.md",
    experiment_directory: str | Path | None = None,
) -> CaseStudyResult:
    """Publish curated plots and mechanics-focused metrics from completed runs."""

    runs = [load_run_artifacts(path) for path in run_directories]
    strategies = [run.strategy for run in runs]
    if (
        len(runs) != len(_REQUIRED_STRATEGIES)
        or set(strategies) != _REQUIRED_STRATEGIES
    ):
        raise CaseStudyError(
            "case study requires exactly one fixed_spread, inventory_aware, "
            "and microprice run"
        )
    classification_values = [run.summary.get("synthetic_demonstration") for run in runs]
    if any(type(value) is not bool for value in classification_values):
        raise CaseStudyError(
            "case-study runs must record synthetic_demonstration as a boolean"
        )
    classifications = set(classification_values)
    if len(classifications) != 1:
        raise CaseStudyError("case study cannot mix synthetic and historical runs")
    synthetic = bool(classifications.pop())
    stream_hashes = {_run_stream_hash(run) for run in runs}
    if None in stream_hashes:
        raise CaseStudyError("case-study runs must record an event-stream SHA-256")
    if len(stream_hashes) != 1:
        raise CaseStudyError("case-study runs do not replay the same event stream")
    stream_hash = next(iter(stream_hashes))
    assert stream_hash is not None
    provenance_values = [run.summary.get("dataset_provenance") for run in runs]
    if any(not isinstance(value, Mapping) for value in provenance_values):
        raise CaseStudyError("case-study runs must record dataset provenance")
    provenance_mappings = [
        value for value in provenance_values if isinstance(value, Mapping)
    ]
    for classification, provenance in zip(
        classification_values,
        provenance_mappings,
        strict=True,
    ):
        source_type = provenance.get("source_type")
        if source_type not in {"synthetic", "historical"}:
            raise CaseStudyError(
                "case-study dataset provenance must record a valid source_type"
            )
        if classification is not (source_type == "synthetic"):
            raise CaseStudyError(
                "synthetic_demonstration disagrees with dataset provenance"
            )
    provenances = {
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        for value in provenance_values
    }
    if len(provenances) != 1:
        raise CaseStudyError("case-study runs have different dataset provenance")
    event_count_values = [run.summary.get("event_count") for run in runs]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in event_count_values
    ):
        raise CaseStudyError(
            "case-study runs must record a positive integer event_count"
        )
    event_counts = {
        value
        for value in event_count_values
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if len(event_counts) != 1:
        raise CaseStudyError("case-study runs do not record the same event count")
    event_count = event_counts.pop()

    experiment_manifest: dict[str, Any] = {}
    sensitivity_interpretation = ""
    sensitivity: Path | None = None
    if experiment_directory is not None:
        sensitivity, sensitivity_table, experiment_manifest = (
            _validated_experiment_artifacts(
                Path(experiment_directory),
                event_stream_hash=stream_hash,
                event_count=event_count,
                synthetic=synthetic,
            )
        )
        sensitivity_interpretation = _sensitivity_interpretation(sensitivity_table)

    comparison_path = Path(comparison_directory)
    compare_runs(
        [run.run_directory for run in runs],
        output_directory=comparison_path,
    )
    destination = Path(assets_directory)
    destination.mkdir(parents=True, exist_ok=True)

    microprice = next(run for run in runs if run.strategy == "microprice")
    if sensitivity is not None:
        shutil.copy2(
            sensitivity,
            microprice.run_directory / "latency_sensitivity.parquet",
        )
        generate_report(microprice.run_directory)

    selected: list[tuple[Path, str]] = [
        (
            comparison_path / "strategy_comparison.png",
            "synthetic_strategy_comparison.png",
        ),
        (
            microprice.run_directory / "plots" / "midpoint_and_quotes.png",
            "microprice_quotes.png",
        ),
        (
            microprice.run_directory / "plots" / "inventory_over_time.png",
            "microprice_inventory.png",
        ),
        (
            microprice.run_directory / "plots" / "markout_by_horizon.png",
            "microprice_markouts.png",
        ),
        (
            microprice.run_directory / "plots" / "latency_sensitivity.png",
            "latency_queue_sensitivity.png",
        ),
    ]
    if experiment_directory is not None:
        selected.append(
            (
                Path(experiment_directory) / "sensitivity_overview.png",
                "sensitivity_overview.png",
            )
        )

    copied: list[Path] = []
    for source, filename in selected:
        if not source.is_file():
            raise CaseStudyError(f"required case-study artifact is missing: {source}")
        target = destination / filename
        shutil.copy2(source, target)
        copied.append(target)

    output = Path(markdown_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        _case_study_markdown(
            runs,
            synthetic=synthetic,
            assets_relative=Path("assets") / destination.name,
            experiment_manifest=experiment_manifest,
            event_stream_hash=stream_hash,
            sensitivity_interpretation=sensitivity_interpretation,
        ),
        encoding="utf-8",
    )
    return CaseStudyResult(output, destination, tuple(copied))


def _validated_experiment_artifacts(
    experiment: Path,
    *,
    event_stream_hash: str,
    event_count: int,
    synthetic: bool,
) -> tuple[Path, pl.DataFrame, dict[str, Any]]:
    sensitivity = experiment / "sensitivity.parquet"
    if not sensitivity.is_file():
        raise CaseStudyError(f"experiment table does not exist: {sensitivity}")
    manifest_path = experiment / "experiment.json"
    if not manifest_path.is_file():
        raise CaseStudyError(f"experiment manifest does not exist: {manifest_path}")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseStudyError(
            f"cannot read experiment manifest: {manifest_path}"
        ) from exc
    if not isinstance(document, dict):
        raise CaseStudyError("experiment manifest must contain a JSON object")
    experiment_manifest: dict[str, Any] = document
    if experiment_manifest.get("event_stream_sha256") != event_stream_hash:
        raise CaseStudyError(
            "experiment and case-study runs use different event streams"
        )
    manifest_event_count = experiment_manifest.get("event_count")
    if type(manifest_event_count) is not int or manifest_event_count != event_count:
        raise CaseStudyError(
            "experiment event_count does not match the case-study runs"
        )
    manifest_classification = experiment_manifest.get("synthetic_demonstration")
    if (
        type(manifest_classification) is not bool
        or manifest_classification is not synthetic
    ):
        raise CaseStudyError(
            "experiment classification does not match the case-study runs"
        )
    try:
        sensitivity_table = pl.read_parquet(sensitivity)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise CaseStudyError(f"cannot read experiment table: {sensitivity}") from exc
    missing = _REQUIRED_EXPERIMENT_COLUMNS.difference(sensitivity_table.columns)
    if missing:
        raise CaseStudyError(
            "experiment table is missing required columns: "
            + ", ".join(sorted(missing))
        )
    manifest_case_count = experiment_manifest.get("case_count")
    if (
        type(manifest_case_count) is not int
        or manifest_case_count <= 0
        or manifest_case_count != sensitivity_table.height
    ):
        raise CaseStudyError(
            "experiment case_count does not match the sensitivity table"
        )
    run_names = sensitivity_table.get_column("run_name").to_list()
    if any(not isinstance(value, str) or not value for value in run_names) or len(
        set(run_names)
    ) != len(run_names):
        raise CaseStudyError("experiment table run_name values must be unique")
    processed_values = sensitivity_table.get_column("events_processed").to_list()
    if any(
        type(value) is not int or value != event_count for value in processed_values
    ):
        raise CaseStudyError(
            "experiment table events_processed values do not match the runs"
        )
    classifications = sensitivity_table.get_column("synthetic_demonstration").to_list()
    if any(
        type(value) is not bool or value is not synthetic for value in classifications
    ):
        raise CaseStudyError("experiment table classification does not match the runs")
    overview = experiment / "sensitivity_overview.png"
    if not overview.is_file():
        raise CaseStudyError(f"experiment overview does not exist: {overview}")
    return sensitivity, sensitivity_table, experiment_manifest


def _case_study_markdown(
    runs: list[RunArtifacts],
    *,
    synthetic: bool,
    assets_relative: Path,
    experiment_manifest: dict[str, Any],
    event_stream_hash: str,
    sensitivity_interpretation: str,
) -> str:
    classification = (
        "SYNTHETIC DEMONSTRATION" if synthetic else "HISTORICAL REPLAY RESEARCH"
    )
    interpretation = (
        "This study demonstrates simulator mechanics and controlled sensitivity. "
        "It is not evidence of expected market profitability."
        if synthetic
        else "Results are conditional on the recorded path and configured execution "
        "assumptions; they are not predictions or trading advice."
    )
    lines = [
        "# Market-making simulator case study",
        "",
        f"> **{classification}.** {interpretation}",
        "",
        "The same canonical Level 2 stream is replayed through fixed-spread, "
        "inventory-aware, and microprice quoting. The strategy sees only delayed "
        "market data and delayed execution reports; future states are used only "
        "for post-run markouts.",
        "",
        "## Mechanics comparison",
        "",
        "| Strategy | Fills | Fill rate | Mean abs. inventory | Near limit | "
        "Max drawdown | Risk blocks | Venue rejects | Scheduler/market event |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in sorted(runs, key=lambda value: value.strategy):
        metrics = run.metrics
        diagnostics = run.diagnostics
        activity = _section(metrics, "trading_activity")
        inventory = _section(metrics, "inventory")
        pnl = _section(metrics, "pnl")
        risk = _section(metrics, "risk")
        processed = float(diagnostics.get("events_processed", 0) or 0)
        scheduled = float(diagnostics.get("scheduler_events_processed", 0) or 0)
        ratio = scheduled / processed if processed else 0.0
        lines.append(
            f"| {run.strategy} | {int(activity.get('fill_events', 0) or 0):,} | "
            f"{float(activity.get('fill_rate', 0) or 0):.3f} | "
            f"{float(inventory.get('mean_absolute', 0) or 0):.2f} | "
            f"{100 * float(inventory.get('time_near_limits_fraction', 0) or 0):.1f}% | "
            f"{float(pnl.get('maximum_drawdown', 0) or 0):.2f} | "
            f"{int(risk.get('event_count', 0) or 0):,} | "
            f"{int(diagnostics.get('exchange_rejected_quote_attempts', 0) or 0):,} | "
            f"{ratio:.2f} |"
        )
    pnl_interpretation = (
        "Synthetic P&L is intentionally not used to rank the strategies. "
        "Inventory behavior, execution activity, risk interventions, and "
        "sensitivity are the meaningful outputs here."
        if synthetic
        else "P&L is not used as a standalone ranking. Inventory behavior, "
        "execution activity, risk interventions, provenance, and model "
        "sensitivity must be considered together."
    )
    lines.extend(
        (
            "",
            pnl_interpretation,
            "",
            f"![Strategy comparison]({(assets_relative / 'synthetic_strategy_comparison.png').as_posix()})",
            "",
            "## Delayed quotes and inventory",
            "",
            f"![Microprice quotes]({(assets_relative / 'microprice_quotes.png').as_posix()})",
            "",
            f"![Microprice inventory]({(assets_relative / 'microprice_inventory.png').as_posix()})",
            "",
            "## Execution quality and model sensitivity",
            "",
            f"![Markouts]({(assets_relative / 'microprice_markouts.png').as_posix()})",
            "",
            f"![Latency and queue sensitivity]({(assets_relative / 'latency_queue_sensitivity.png').as_posix()})",
            "",
        )
    )
    if sensitivity_interpretation:
        lines.extend((sensitivity_interpretation, ""))
    if experiment_manifest:
        lines.extend(
            (
                f"- Experiment cases: **{experiment_manifest.get('case_count', 'n/a')}**",
                f"- Canonical event-stream SHA-256: `{event_stream_hash}`",
                "",
                f"![Sensitivity overview]({(assets_relative / 'sensitivity_overview.png').as_posix()})",
                "",
            )
        )
    lines.extend(
        (
            "## Controlled cancellation-policy validation",
            "",
            "A separate [controlled end-to-end replay](QUEUE_CANCELLATION.md) "
            "uses the canonical own-10/add-50/cancel-60/trade-65 fixture to "
            "discriminate the three policies on both sides with positive "
            "latency and audited fee/cash accounting. Reproduce and verify "
            "that witness using the commands in its report.",
            "",
            "## Reproduce",
            "",
            "```bash",
            "python -m lobmm.cli demo --publish-case-study",
            "```",
            "",
            "Bulk run data remains ignored because it is reproducible. This page and "
            "its curated images are the small reviewable artifacts intended for the "
            "repository.",
            "",
        )
    )
    return "\n".join(lines)


def _section(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _run_stream_hash(run: RunArtifacts) -> str | None:
    value = run.summary.get("event_stream_sha256")
    if value in {None, ""}:
        value = run.diagnostics.get("event_stream_sha256")
    return str(value) if value not in {None, ""} else None


def _sensitivity_interpretation(table: pl.DataFrame) -> str:
    rows = table.to_dicts()
    required = {
        "latency_multiplier",
        "fill_rate",
        "mean_absolute_inventory",
    }
    if not rows or not required.issubset(table.columns):
        return ""

    latencies = sorted(
        {
            float(row["latency_multiplier"])
            for row in rows
            if _finite(row.get("latency_multiplier")) is not None
        }
    )
    sentences: list[str] = []
    if len(latencies) >= 2:
        low, high = latencies[0], latencies[-1]
        low_rows = [
            row for row in rows if _finite(row.get("latency_multiplier")) == low
        ]
        high_rows = [
            row for row in rows if _finite(row.get("latency_multiplier")) == high
        ]
        low_fill = _mean_column(low_rows, "fill_rate")
        high_fill = _mean_column(high_rows, "fill_rate")
        low_inventory = _mean_column(low_rows, "mean_absolute_inventory")
        high_inventory = _mean_column(high_rows, "mean_absolute_inventory")
        if None not in (low_fill, high_fill, low_inventory, high_inventory):
            sentences.append(
                f"Across this grid, moving from {low:g}x to {high:g}x configured "
                f"latency changed mean absolute inventory from {low_inventory:.2f} "
                f"to {high_inventory:.2f} and fill rate from {low_fill:.3f} to "
                f"{high_fill:.3f}."
            )

    queue_names = {str(row.get("queue_allocation")) for row in rows}
    if len(queue_names) > 1 and not _queue_results_differ(rows):
        sentences.append(
            "The queue-allocation policies produced identical recorded metrics on "
            "this short path, so this demonstration does not discriminate among "
            "them."
        )
    return " ".join(sentences)


def _queue_results_differ(rows: list[dict[str, Any]]) -> bool:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            row.get("strategy"),
            row.get("ablation"),
            row.get("latency_multiplier"),
            row.get("fee_multiplier"),
        )
        groups.setdefault(key, []).append(row)
    metrics = (
        "fill_rate",
        "mean_absolute_inventory",
        "maximum_drawdown",
        "net_pnl",
    )
    for group in groups.values():
        if len({str(row.get("queue_allocation")) for row in group}) < 2:
            continue
        for metric in metrics:
            values = [
                value
                for row in group
                if (value := _finite(row.get(metric))) is not None
            ]
            if values and max(values) - min(values) > 1e-12:
                return True
    return False


def _mean_column(rows: list[dict[str, Any]], column: str) -> float | None:
    values = [value for row in rows if (value := _finite(row.get(column))) is not None]
    return fmean(values) if values else None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
