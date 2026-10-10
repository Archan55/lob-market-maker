"""Run-artifact loading, research summaries, plots, and comparisons."""

from __future__ import annotations

import bisect
import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

# Some controlled research/CI environments expose a read-only home directory.
# Point Matplotlib's non-result cache at the system temporary directory before
# importing it; plot outputs still go only to the requested run directory.
_MATPLOTLIB_CACHE = Path(tempfile.gettempdir()) / "lobmm-matplotlib"
_MATPLOTLIB_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MATPLOTLIB_CACHE))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402
import yaml  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

REQUIRED_PLOT_FILENAMES: tuple[str, ...] = (
    "midpoint_and_quotes.png",
    "inventory_over_time.png",
    "gross_and_net_pnl.png",
    "drawdown.png",
    "fill_locations.png",
    "markout_distribution.png",
    "markout_by_horizon.png",
    "pnl_by_market_regime.png",
    "inventory_histogram.png",
    "queue_ahead_distribution.png",
    "latency_sensitivity.png",
    "strategy_comparison.png",
)

_TABLE_FILES: dict[str, tuple[str, ...]] = {
    "orders": ("orders.parquet",),
    "fills": ("fills.parquet",),
    "inventory": ("inventory.parquet",),
    "pnl": ("pnl.parquet",),
    "quotes": ("quotes.parquet", "quote_messages.parquet"),
    "risk_events": ("risk_events.parquet",),
    "market": ("market.parquet", "market_states.parquet"),
    "markouts": ("markouts.parquet",),
    "latency_sensitivity": ("latency_sensitivity.parquet",),
}

_COMPARISON_SCHEMA: dict[str, type[pl.DataType]] = {
    "run_name": pl.String,
    "strategy": pl.String,
    "symbol": pl.String,
    "events_processed": pl.Int64,
    "fill_events": pl.Int64,
    "fill_rate": pl.Float64,
    "turnover": pl.Float64,
    "gross_pnl": pl.Float64,
    "fees": pl.Float64,
    "rebates": pl.Float64,
    "net_pnl": pl.Float64,
    "maximum_drawdown": pl.Float64,
    "end_inventory": pl.Int64,
    "mean_absolute_inventory": pl.Float64,
    "time_near_limits_fraction": pl.Float64,
    "risk_event_count": pl.Int64,
    "scheduler_events_per_market_event": pl.Float64,
    "realized_spread_ticks": pl.Float64,
}


class ReportError(ValueError):
    """Raised when run artifacts are missing or malformed."""


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """In-memory view of one completed backtest run."""

    run_directory: Path
    metrics: Mapping[str, Any]
    summary: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    config: Mapping[str, Any]
    tables: Mapping[str, pl.DataFrame]

    @property
    def run_name(self) -> str:
        value = self.summary.get("run_name")
        return str(value) if value not in {None, ""} else self.run_directory.name

    @property
    def strategy(self) -> str:
        value = self.summary.get("strategy")
        if value not in {None, ""}:
            return str(value)
        strategy_config = self.config.get("strategy")
        if isinstance(strategy_config, Mapping):
            configured = strategy_config.get("name")
            if configured not in {None, ""}:
                return str(configured)
        return "unknown"

    @property
    def symbol(self) -> str:
        value = self.summary.get("symbol")
        if value not in {None, ""}:
            return str(value)
        instrument = self.config.get("instrument")
        if isinstance(instrument, Mapping):
            configured = instrument.get("symbol")
            if configured not in {None, ""}:
                return str(configured)
        return "unknown"

    def table(self, name: str) -> pl.DataFrame:
        """Return one known table or an empty frame when legitimately absent."""

        return self.tables.get(name, pl.DataFrame())


@dataclass(frozen=True, slots=True)
class ReportResult:
    """Files and text produced for one run."""

    run_directory: Path
    plots_directory: Path
    plot_paths: Mapping[str, Path]
    summary_text: str


def _read_json_object(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ReportError(f"required run artifact is missing: {path}")
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReportError(f"JSON artifact must contain an object: {path}")
    return document


def _read_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ReportError(f"cannot read YAML artifact {path}: {exc}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ReportError(f"YAML artifact must contain a mapping: {path}")
    return document


def load_run_artifacts(run_directory: str | Path) -> RunArtifacts:
    """Load a completed run while tolerating optional empty report inputs."""

    directory = Path(run_directory).resolve()
    if not directory.is_dir():
        raise ReportError(f"run directory does not exist: {directory}")

    metrics = _read_json_object(directory / "metrics.json", required=True)
    summary = _read_json_object(directory / "summary.json", required=False)
    diagnostics = _read_json_object(directory / "diagnostics.json", required=False)
    config = _read_yaml_object(directory / "run_config.yaml")
    tables: dict[str, pl.DataFrame] = {}
    for name, candidates in _TABLE_FILES.items():
        path = next(
            (
                directory / candidate
                for candidate in candidates
                if (directory / candidate).exists()
            ),
            None,
        )
        if path is None:
            tables[name] = pl.DataFrame()
            continue
        try:
            tables[name] = pl.read_parquet(path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ReportError(f"cannot read Parquet artifact {path}: {exc}") from exc

    return RunArtifacts(
        run_directory=directory,
        metrics=MappingProxyType(metrics),
        summary=MappingProxyType(summary),
        diagnostics=MappingProxyType(diagnostics),
        config=MappingProxyType(config),
        tables=MappingProxyType(tables),
    )


def _nested(
    document: Mapping[str, Any],
    section: str,
    key: str,
) -> Any:
    nested = document.get(section)
    return nested.get(key) if isinstance(nested, Mapping) else None


def _display(value: Any, *, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "n/a"
        return f"{value:,.{digits}g}"
    return str(value)


def human_readable_summary(
    source: RunArtifacts | Mapping[str, Any] | str | Path,
    *,
    run_name: str | None = None,
) -> str:
    """Return a concise console-ready summary without inventing missing values."""

    if isinstance(source, RunArtifacts):
        artifacts = source
        metrics = artifacts.metrics
        effective_name = run_name or artifacts.run_name
        strategy = artifacts.strategy
        symbol = artifacts.symbol
    elif isinstance(source, (str, Path)):
        artifacts = load_run_artifacts(source)
        metrics = artifacts.metrics
        effective_name = run_name or artifacts.run_name
        strategy = artifacts.strategy
        symbol = artifacts.symbol
    else:
        metrics = source
        effective_name = run_name or "run"
        strategy = "unknown"
        symbol = "unknown"

    lines = [
        f"Run: {effective_name}",
        f"Strategy: {strategy}",
        f"Instrument: {symbol}",
        f"Net P&L: {_display(_nested(metrics, 'pnl', 'net_pnl'))}",
        f"Gross P&L: {_display(_nested(metrics, 'pnl', 'gross_pnl'))}",
        f"Fees: {_display(_nested(metrics, 'pnl', 'fees'))}",
        f"Rebates: {_display(_nested(metrics, 'pnl', 'rebates'))}",
        (f"Maximum drawdown: {_display(_nested(metrics, 'pnl', 'maximum_drawdown'))}"),
        (
            "Fill events: "
            f"{_display(_nested(metrics, 'trading_activity', 'fill_events'))}"
        ),
        (f"Fill rate: {_display(_nested(metrics, 'trading_activity', 'fill_rate'))}"),
        (f"End inventory: {_display(_nested(metrics, 'inventory', 'end_of_session'))}"),
        (
            "Events processed: "
            f"{_display(_nested(metrics, 'engineering', 'events_processed'))}"
        ),
    ]
    stop = metrics.get("client_stop")
    if isinstance(stop, Mapping):
        lines.extend(
            [
                f"Client stop / observation end (ns): {stop.get('stop_timestamp_ns')} / {stop.get('observation_end_timestamp_ns')}",
                f"Known inventory: {stop.get('known_inventory')}",
                f"Unresolved venue orders / entries: {len(stop.get('outstanding_orders', []))} / {len(stop.get('in_flight_entries', []))}",
                f"Reachable inventory: [{stop.get('reachable_inventory_min')}, {stop.get('reachable_inventory_max')}]",
                f"Unresolved client order acknowledgments: {len(stop.get('unresolved_client_order_ids', []))}",
                f"Historical mark source / age (ns): {stop.get('mark_source_timestamp_ns')} / {stop.get('mark_age_ns')}",
                f"Venue observation coverage ends (ns): {stop.get('venue_observation_end_timestamp_ns')}",
                "Final P&L is covered marked accounting; later uncovered fills remain possible and inventory is retained.",
            ]
        )
    disclaimer = metrics.get("research_disclaimer")
    if disclaimer:
        lines.extend(("", f"Research disclaimer: {disclaimer}"))
    return "\n".join(lines)


console_summary = human_readable_summary


def _first_column(frame: pl.DataFrame, candidates: Sequence[str]) -> str | None:
    return next((column for column in candidates if column in frame.columns), None)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric_pairs(
    frame: pl.DataFrame,
    *,
    x_columns: Sequence[str],
    y_columns: Sequence[str],
) -> list[tuple[float, float]]:
    x_column = _first_column(frame, x_columns)
    y_column = _first_column(frame, y_columns)
    if x_column is None or y_column is None:
        return []
    output: list[tuple[float, float]] = []
    for x_raw, y_raw in frame.select(x_column, y_column).iter_rows():
        x_value = _number(x_raw)
        y_value = _number(y_raw)
        if x_value is not None and y_value is not None:
            output.append((x_value, y_value))
    return output


def _time_origin(*series: Sequence[tuple[float, float]]) -> float:
    values = [x for pairs in series for x, _ in pairs]
    return min(values, default=0.0)


def _time_values(
    pairs: Sequence[tuple[float, float]],
    origin: float,
) -> tuple[list[float], list[float]]:
    return (
        [(x - origin) / 1_000_000_000.0 for x, _ in pairs],
        [y for _, y in pairs],
    )


def _figure(title: str) -> tuple[Figure, Axes]:
    figure, axis = plt.subplots(figsize=(9, 5), dpi=120)
    axis.set_title(title)
    axis.grid(alpha=0.25)
    return figure, axis


def _no_data(axis: Axes, message: str) -> None:
    axis.text(
        0.5,
        0.5,
        message,
        ha="center",
        va="center",
        transform=axis.transAxes,
        wrap=True,
    )
    axis.set_axis_off()


def _save(figure: Figure, path: Path) -> Path:
    figure.tight_layout()
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)
    return path


def _plot_midpoint_and_quotes(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Midpoint and strategy quote decisions")
    market = artifacts.table("market")
    quotes = artifacts.table("quotes")
    midpoint = _numeric_pairs(
        market,
        x_columns=("timestamp_ns",),
        y_columns=("midpoint_ticks",),
    )
    bid_quotes: list[tuple[float, float]] = []
    ask_quotes: list[tuple[float, float]] = []
    if {"timestamp_ns", "price_ticks", "side"}.issubset(quotes.columns):
        for timestamp, price, side, action in quotes.select(
            "timestamp_ns",
            "price_ticks",
            "side",
            pl.col("action") if "action" in quotes.columns else pl.lit("submit"),
        ).iter_rows():
            if str(action).lower() not in {"submit", "replace", "new"}:
                continue
            x_value = _number(timestamp)
            y_value = _number(price)
            if x_value is None or y_value is None:
                continue
            side_text = str(side).upper()
            if side_text in {"1", "BID"}:
                bid_quotes.append((x_value, y_value))
            elif side_text in {"-1", "ASK"}:
                ask_quotes.append((x_value, y_value))

    origin = _time_origin(midpoint, bid_quotes, ask_quotes)
    plotted = False
    if midpoint:
        x_values, y_values = _time_values(midpoint, origin)
        axis.plot(x_values, y_values, label="True midpoint", color="black", linewidth=1)
        plotted = True
    for pairs, label, color, marker in (
        (bid_quotes, "Bid submissions", "tab:blue", "^"),
        (ask_quotes, "Ask submissions", "tab:red", "v"),
    ):
        if pairs:
            x_values, y_values = _time_values(pairs, origin)
            axis.scatter(
                x_values,
                y_values,
                label=label,
                color=color,
                marker=marker,
                s=18,
                alpha=0.75,
            )
            plotted = True
    if plotted:
        axis.set_xlabel("Seconds from first plotted event")
        axis.set_ylabel("Price (ticks)")
        axis.legend()
    else:
        _no_data(axis, "No midpoint or quote-decision data is available for this run.")
    return _save(figure, path)


def _plot_inventory(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Inventory over time")
    pairs = _numeric_pairs(
        artifacts.table("inventory"),
        x_columns=("timestamp_ns",),
        y_columns=("inventory",),
    )
    if pairs:
        x_values, y_values = _time_values(pairs, _time_origin(pairs))
        axis.step(x_values, y_values, where="post", color="tab:blue")
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("Seconds from first observation")
        axis.set_ylabel("Signed inventory")
    else:
        _no_data(axis, "No inventory observations are available for this run.")
    return _save(figure, path)


def _plot_pnl(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Gross and net P&L over time")
    pnl = artifacts.table("pnl")
    gross = _numeric_pairs(
        pnl,
        x_columns=("timestamp_ns",),
        y_columns=("gross_pnl",),
    )
    net = _numeric_pairs(
        pnl,
        x_columns=("timestamp_ns",),
        y_columns=("net_pnl",),
    )
    origin = _time_origin(gross, net)
    plotted = False
    for pairs, label, color in (
        (gross, "Gross P&L", "tab:gray"),
        (net, "Net P&L", "tab:green"),
    ):
        if pairs:
            x_values, y_values = _time_values(pairs, origin)
            axis.plot(x_values, y_values, label=label, color=color)
            plotted = True
    if plotted:
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("Seconds from first observation")
        axis.set_ylabel("P&L (configured currency)")
        axis.legend()
    else:
        _no_data(axis, "No marked P&L observations are available for this run.")
    return _save(figure, path)


def _plot_drawdown(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Net P&L drawdown")
    pairs = _numeric_pairs(
        artifacts.table("pnl"),
        x_columns=("timestamp_ns",),
        y_columns=("net_pnl",),
    )
    if pairs:
        pairs.sort()
        high = -math.inf
        drawdowns: list[tuple[float, float]] = []
        for timestamp, value in pairs:
            high = max(high, value)
            drawdowns.append((timestamp, high - value))
        x_values, y_values = _time_values(drawdowns, _time_origin(drawdowns))
        axis.fill_between(x_values, y_values, color="tab:red", alpha=0.35)
        axis.plot(x_values, y_values, color="tab:red")
        axis.set_xlabel("Seconds from first observation")
        axis.set_ylabel("Drawdown")
    else:
        _no_data(axis, "No net P&L series is available to calculate drawdown.")
    return _save(figure, path)


def _plot_fill_locations(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Fill locations on the true market price")
    midpoint = _numeric_pairs(
        artifacts.table("market"),
        x_columns=("timestamp_ns",),
        y_columns=("midpoint_ticks",),
    )
    fills = artifacts.table("fills")
    buy_fills: list[tuple[float, float]] = []
    sell_fills: list[tuple[float, float]] = []
    if {"price_ticks", "side"}.issubset(fills.columns):
        timestamp_column = _first_column(
            fills, ("exchange_fill_timestamp_ns", "timestamp_ns")
        )
        if timestamp_column is not None:
            for timestamp, price, side in fills.select(
                timestamp_column, "price_ticks", "side"
            ).iter_rows():
                x_value = _number(timestamp)
                y_value = _number(price)
                if x_value is None or y_value is None:
                    continue
                side_text = str(side).upper()
                if side_text in {"1", "BID"}:
                    buy_fills.append((x_value, y_value))
                elif side_text in {"-1", "ASK"}:
                    sell_fills.append((x_value, y_value))

    origin = _time_origin(midpoint, buy_fills, sell_fills)
    plotted = False
    if midpoint:
        x_values, y_values = _time_values(midpoint, origin)
        axis.plot(x_values, y_values, color="black", linewidth=1, label="Midpoint")
        plotted = True
    for pairs, label, color, marker in (
        (buy_fills, "Buy fills", "tab:blue", "^"),
        (sell_fills, "Sell fills", "tab:red", "v"),
    ):
        if pairs:
            x_values, y_values = _time_values(pairs, origin)
            axis.scatter(x_values, y_values, color=color, marker=marker, label=label)
            plotted = True
    if plotted:
        axis.set_xlabel("Seconds from first plotted event")
        axis.set_ylabel("Price (ticks)")
        axis.legend()
    else:
        _no_data(axis, "No market midpoint or fill data is available for this run.")
    return _save(figure, path)


def _column_numbers(frame: pl.DataFrame, candidates: Sequence[str]) -> list[float]:
    column = _first_column(frame, candidates)
    if column is None:
        return []
    return [
        number
        for value in frame[column].to_list()
        if (number := _number(value)) is not None
    ]


def _plot_markout_distribution(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Side-adjusted markout distribution")
    values = _column_numbers(artifacts.table("markouts"), ("markout_ticks",))
    if values:
        bins = min(30, max(5, round(math.sqrt(len(values)))))
        axis.hist(values, bins=bins, color="tab:purple", alpha=0.75)
        axis.axvline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("Markout (ticks; positive favours strategy)")
        axis.set_ylabel("Observations")
    else:
        _no_data(axis, "No finite forward markouts are available for this run.")
    return _save(figure, path)


def _plot_markout_by_horizon(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Mean markout by horizon")
    markouts = artifacts.table("markouts")
    buckets: defaultdict[int, list[float]] = defaultdict(list)
    if {"horizon_ns", "markout_ticks"}.issubset(markouts.columns):
        for horizon, markout in markouts.select(
            "horizon_ns", "markout_ticks"
        ).iter_rows():
            horizon_value = _number(horizon)
            markout_value = _number(markout)
            if horizon_value is not None and markout_value is not None:
                buckets[int(horizon_value)].append(markout_value)
    if buckets:
        horizons = sorted(buckets)
        labels = [_format_horizon(value) for value in horizons]
        means = [sum(buckets[value]) / len(buckets[value]) for value in horizons]
        axis.bar(labels, means, color="tab:purple")
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("Forward horizon")
        axis.set_ylabel("Mean side-adjusted markout (ticks)")
    else:
        _no_data(axis, "No finite markouts by horizon are available for this run.")
    return _save(figure, path)


def _format_horizon(horizon_ns: int) -> str:
    if horizon_ns % 1_000_000_000 == 0:
        return f"{horizon_ns // 1_000_000_000}s"
    if horizon_ns % 1_000_000 == 0:
        return f"{horizon_ns // 1_000_000}ms"
    return f"{horizon_ns}ns"


def _pnl_by_regime(
    pnl: pl.DataFrame,
    market: pl.DataFrame,
) -> dict[str, dict[str, float]]:
    if "timestamp_ns" not in pnl.columns or "net_pnl" not in pnl.columns:
        return {}
    regime_columns = [
        column
        for column in ("spread_regime", "volatility_regime", "liquidity_regime")
        if column in market.columns
    ]
    if "timestamp_ns" not in market.columns or not regime_columns:
        return {}

    market_rows = sorted(
        [
            (
                int(timestamp),
                {column: row[index + 1] for index, column in enumerate(regime_columns)},
            )
            for row in market.select("timestamp_ns", *regime_columns).iter_rows()
            if (timestamp := row[0]) is not None
        ],
        key=lambda item: item[0],
    )
    market_timestamps = [timestamp for timestamp, _ in market_rows]
    totals: dict[str, defaultdict[str, float]] = {
        column: defaultdict(float) for column in regime_columns
    }
    previous: float | None = None
    pnl_rows = sorted(
        (
            int(timestamp),
            value,
        )
        for timestamp, raw_value in pnl.select("timestamp_ns", "net_pnl").iter_rows()
        if timestamp is not None and (value := _number(raw_value)) is not None
    )
    for timestamp, value in pnl_rows:
        change = 0.0 if previous is None else value - previous
        previous = value
        index = bisect.bisect_right(market_timestamps, timestamp) - 1
        if index < 0:
            continue
        regimes = market_rows[index][1]
        for column in regime_columns:
            regime = regimes.get(column)
            if regime is not None:
                totals[column][str(regime)] += change
    return {
        column: dict(sorted(values.items()))
        for column, values in totals.items()
        if values
    }


def _plot_pnl_by_regime(artifacts: RunArtifacts, path: Path) -> Path:
    grouped = _pnl_by_regime(
        artifacts.table("pnl"),
        artifacts.table("market"),
    )
    if not grouped:
        figure, axis = _figure("Net P&L change by recorded market regime")
        _no_data(
            axis,
            "No classified market-state and P&L series overlap for this run.",
        )
        return _save(figure, path)

    figure, axes = plt.subplots(
        1,
        len(grouped),
        figsize=(5 * len(grouped), 5),
        dpi=120,
        squeeze=False,
    )
    figure.suptitle("Net P&L change by recorded market regime")
    for axis, (column, values) in zip(axes[0], grouped.items(), strict=True):
        axis.bar(list(values), list(values.values()), color="tab:green")
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title(column.replace("_", " ").title())
        axis.set_ylabel("Net P&L change")
        axis.tick_params(axis="x", rotation=25)
        axis.grid(axis="y", alpha=0.25)
    return _save(figure, path)


def _plot_inventory_histogram(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Inventory distribution")
    values = _column_numbers(artifacts.table("inventory"), ("inventory",))
    if values:
        minimum = math.floor(min(values))
        maximum = math.ceil(max(values))
        bins = max(1, min(40, maximum - minimum + 1))
        axis.hist(values, bins=bins, color="tab:blue", alpha=0.75)
        axis.axvline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("Signed inventory")
        axis.set_ylabel("Observations")
    else:
        _no_data(axis, "No inventory observations are available for this run.")
    return _save(figure, path)


def _plot_queue_ahead(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Estimated queue-ahead distribution")
    values: list[float] = []
    for table_name in ("orders", "fills"):
        values = _column_numbers(
            artifacts.table(table_name),
            ("queue_ahead_estimate", "queue_ahead", "external_ahead"),
        )
        if values:
            break
    if values:
        bins = min(30, max(5, round(math.sqrt(len(values)))))
        axis.hist(values, bins=bins, color="tab:orange", alpha=0.75)
        axis.set_xlabel("Estimated quantity ahead")
        axis.set_ylabel("Observations")
    else:
        _no_data(axis, "No queue-ahead estimates are available for this run.")
    return _save(figure, path)


def _plot_latency_sensitivity(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Latency sensitivity")
    table = artifacts.table("latency_sensitivity")
    x_column = _first_column(
        table,
        ("latency_ns", "order_entry_latency_ns", "market_data_latency_ns"),
    )
    y_column = _first_column(
        table,
        (
            "mean_absolute_inventory",
            "fill_rate",
            "net_pnl",
            "markout_ticks",
        ),
    )
    grouped: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    if x_column is not None and y_column is not None:
        group_columns = [
            column
            for column in (
                "strategy",
                "queue_allocation",
                "ablation",
                "fee_multiplier",
            )
            if column in table.columns
        ]
        for row in table.iter_rows(named=True):
            x = _number(row.get(x_column))
            y = _number(row.get(y_column))
            if x is None or y is None:
                continue
            label = " · ".join(str(row.get(column)) for column in group_columns)
            grouped[label or "configured case"].append((x, y))
    if grouped:
        assert y_column is not None
        for label, pairs in sorted(grouped.items()):
            pairs.sort()
            axis.plot(
                [x / 1_000.0 for x, _ in pairs],
                [y for _, y in pairs],
                marker="o",
                linewidth=1.2,
                label=label,
            )
        axis.set_xlabel("Configured latency (microseconds)")
        axis.set_ylabel(y_column.replace("_", " ").title())
        if len(grouped) > 1:
            axis.legend(fontsize=7)
    else:
        _no_data(
            axis,
            "No latency sweep artifact is present. A single configured-latency "
            "run is not a sensitivity analysis.",
        )
    return _save(figure, path)


def _plot_single_run_comparison(artifacts: RunArtifacts, path: Path) -> Path:
    figure, axis = _figure("Strategy comparison")
    _no_data(
        axis,
        f"Run '{artifacts.run_name}' contains one strategy. Use compare_runs "
        "with multiple completed runs for a strategy comparison.",
    )
    return _save(figure, path)


def generate_report(run_directory: str | Path) -> ReportResult:
    """Generate every required plot for one completed run."""

    artifacts = load_run_artifacts(run_directory)
    plots_directory = artifacts.run_directory / "plots"
    plots_directory.mkdir(parents=True, exist_ok=True)
    plotters = (
        ("midpoint_and_quotes.png", _plot_midpoint_and_quotes),
        ("inventory_over_time.png", _plot_inventory),
        ("gross_and_net_pnl.png", _plot_pnl),
        ("drawdown.png", _plot_drawdown),
        ("fill_locations.png", _plot_fill_locations),
        ("markout_distribution.png", _plot_markout_distribution),
        ("markout_by_horizon.png", _plot_markout_by_horizon),
        ("pnl_by_market_regime.png", _plot_pnl_by_regime),
        ("inventory_histogram.png", _plot_inventory_histogram),
        ("queue_ahead_distribution.png", _plot_queue_ahead),
        ("latency_sensitivity.png", _plot_latency_sensitivity),
        ("strategy_comparison.png", _plot_single_run_comparison),
    )
    paths = {
        filename: plotter(artifacts, plots_directory / filename)
        for filename, plotter in plotters
    }
    summary_text = human_readable_summary(artifacts)
    try:
        (artifacts.run_directory / "report_summary.txt").write_text(
            summary_text + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReportError(
            f"cannot write report summary in {artifacts.run_directory}: {exc}"
        ) from exc
    return ReportResult(
        run_directory=artifacts.run_directory,
        plots_directory=plots_directory,
        plot_paths=MappingProxyType(paths),
        summary_text=summary_text,
    )


def _comparison_row(artifacts: RunArtifacts) -> dict[str, Any]:
    metrics = artifacts.metrics
    events_processed = _integer(_nested(metrics, "engineering", "events_processed"))
    scheduler_events = _integer(artifacts.diagnostics.get("scheduler_events_processed"))
    return {
        "run_name": artifacts.run_name,
        "strategy": artifacts.strategy,
        "symbol": artifacts.symbol,
        "events_processed": events_processed,
        "fill_events": _integer(_nested(metrics, "trading_activity", "fill_events")),
        "fill_rate": _number(_nested(metrics, "trading_activity", "fill_rate")),
        "turnover": _number(_nested(metrics, "trading_activity", "turnover")),
        "gross_pnl": _number(_nested(metrics, "pnl", "gross_pnl")),
        "fees": _number(_nested(metrics, "pnl", "fees")),
        "rebates": _number(_nested(metrics, "pnl", "rebates")),
        "net_pnl": _number(_nested(metrics, "pnl", "net_pnl")),
        "maximum_drawdown": _number(_nested(metrics, "pnl", "maximum_drawdown")),
        "end_inventory": _integer(_nested(metrics, "inventory", "end_of_session")),
        "mean_absolute_inventory": _number(
            _nested(metrics, "inventory", "mean_absolute")
        ),
        "time_near_limits_fraction": _number(
            _nested(metrics, "inventory", "time_near_limits_fraction")
        ),
        "risk_event_count": _integer(_nested(metrics, "risk", "event_count")),
        "scheduler_events_per_market_event": (
            scheduler_events / events_processed
            if scheduler_events is not None
            and events_processed is not None
            and events_processed > 0
            else None
        ),
        "realized_spread_ticks": _number(
            _nested(metrics, "execution_quality", "realized_spread_ticks")
        ),
    }


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _comparison_plot(table: pl.DataFrame, path: Path) -> Path:
    figure, axes = plt.subplots(1, 3, figsize=(14, 4.5), dpi=120)
    figure.suptitle("Strategy mechanics comparison (not a profitability ranking)")
    labels = table["strategy"].to_list() if table.height else []
    panels = (
        ("fill_rate", "Fill rate", "tab:blue"),
        ("mean_absolute_inventory", "Mean absolute inventory", "tab:orange"),
        ("maximum_drawdown", "Maximum drawdown", "tab:red"),
    )
    for axis, (column, title, color) in zip(axes, panels, strict=True):
        values = (
            [
                float(value) if value is not None else math.nan
                for value in table[column].to_list()
            ]
            if table.height and column in table.columns
            else []
        )
        if values and any(math.isfinite(value) for value in values):
            axis.bar(labels, values, color=color)
            axis.tick_params(axis="x", rotation=25)
            axis.set_title(title)
            axis.grid(axis="y", alpha=0.25)
        else:
            _no_data(axis, f"No {title.lower()} data are available.")
    figure.tight_layout()
    return _save(figure, path)


def compare_runs(
    run_directories: Iterable[str | Path],
    output_directory: str | Path | None = None,
    *,
    output_dir: str | Path | None = None,
) -> pl.DataFrame:
    """Compare completed runs and write deterministic CSV/JSON/plot outputs."""

    if output_directory is not None and output_dir is not None:
        raise ReportError("specify only one of output_directory or output_dir")
    directories = [Path(path).resolve() for path in run_directories]
    if not directories:
        raise ReportError("compare_runs requires at least one run directory")
    artifacts = [load_run_artifacts(path) for path in directories]
    _validate_comparison_identity(artifacts)
    rows = [_comparison_row(run) for run in artifacts]
    table = pl.DataFrame(rows, schema=_COMPARISON_SCHEMA, strict=False).sort("run_name")

    requested_output = output_directory if output_directory is not None else output_dir
    destination = (
        Path(requested_output).resolve()
        if requested_output is not None
        else directories[0].parent / "comparison"
    )
    try:
        destination.mkdir(parents=True, exist_ok=True)
        table.write_csv(destination / "comparison.csv")
        (destination / "comparison.json").write_text(
            json.dumps(table.to_dicts(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _comparison_plot(table, destination / "strategy_comparison.png")
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise ReportError(
            f"cannot write comparison outputs in {destination}: {exc}"
        ) from exc
    return table


def _validate_comparison_identity(artifacts: list[RunArtifacts]) -> None:
    if len(artifacts) < 2:
        return
    shutdown_identities = {
        (
            run.config.get("backtest", {}).get("shutdown_policy", "forced_expiry"),
            run.config.get("backtest", {}).get("client_stop_timestamp_ns"),
            run.config.get("backtest", {}).get("observation_end_timestamp_ns"),
        )
        for run in artifacts
    }
    if len(shutdown_identities) != 1:
        raise ReportError(
            "comparison runs must use the same shutdown policy, stop time and observation horizon"
        )
    hashes = {
        str(value)
        for run in artifacts
        if (
            value := run.summary.get("event_stream_sha256")
            or run.diagnostics.get("event_stream_sha256")
        )
    }
    if len(hashes) != 1 or any(
        not (
            run.summary.get("event_stream_sha256")
            or run.diagnostics.get("event_stream_sha256")
        )
        for run in artifacts
    ):
        raise ReportError("comparison runs must record the same event-stream SHA-256")

    symbols = {run.symbol for run in artifacts}
    if len(symbols) != 1:
        raise ReportError("comparison runs must use the same instrument symbol")

    event_count_values = [
        run.summary.get("event_count")
        or _nested(run.metrics, "engineering", "events_processed")
        for run in artifacts
    ]
    if (
        any(value is None for value in event_count_values)
        or len(set(event_count_values)) != 1
    ):
        raise ReportError("comparison runs must process the same event count")

    provenance_values = [run.summary.get("dataset_provenance") for run in artifacts]
    if any(not isinstance(value, Mapping) for value in provenance_values):
        raise ReportError("comparison runs must record dataset provenance")
    provenances = {
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        for value in provenance_values
    }
    if len(provenances) != 1:
        raise ReportError("comparison runs must have matching dataset provenance")
