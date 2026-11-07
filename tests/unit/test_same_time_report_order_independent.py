"""Frozen report-order witnesses and independent audit-series oracles.

These original fictional inputs establish reporting mechanics only. The five
plot vectors and regime witness were fixed before outcomes in the November 7
protocol. Numeric assertions inspect actual Matplotlib artists; the original
save function still writes each PNG and closes its figure.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

import lobmm.report as report_module

_EPOCH = 1_700_000_000_000_000_000
_PLOT_CASES = {
    "descending_tie": (
        ((0, 0), (10, 2), (10, 1), (20, 2)),
        (0.0, 1e-8, 1e-8, 2e-8),
        (0.0, 0.0, 1.0, 0.0),
    ),
    "rising_tie": (
        ((0, 0), (10, 1), (10, 2), (20, 2)),
        (0.0, 1e-8, 1e-8, 2e-8),
        (0.0, 0.0, 0.0, 0.0),
    ),
    "unique_time": (
        ((0, 0), (10, 2), (20, 1), (30, 2)),
        (0.0, 1e-8, 2e-8, 3e-8),
        (0.0, 0.0, 1.0, 0.0),
    ),
    "flat_duplicate": (
        ((0, 0), (10, 2), (10, 2), (20, 1)),
        (0.0, 1e-8, 1e-8, 2e-8),
        (0.0, 0.0, 0.0, 1.0),
    ),
    "epoch_distinct": (
        ((_EPOCH, 0), (_EPOCH + 1, 2), (_EPOCH + 2, 1), (_EPOCH + 3, 2)),
        (0.0, 1e-9, 2e-9, 3e-9),
        (0.0, 0.0, 1.0, 0.0),
    ),
}


def _finite_pairs(pairs: Sequence[Sequence[Any]]) -> list[tuple[int | float, float]]:
    result: list[tuple[int | float, float]] = []
    for timestamp, raw_value in pairs:
        try:
            numeric_time, value = float(timestamp), float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_time) and math.isfinite(value):
            result.append(
                (timestamp if type(timestamp) is int else numeric_time, value)
            )
    return result


def ordered_drawdown_oracle(pairs: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """Observed-peak drawdown in stable time order; integer origins stay exact."""

    ordered = sorted(_finite_pairs(pairs), key=lambda pair: pair[0])
    timestamps = [timestamp for timestamp, _ in ordered]
    net_pnl = [value for _, value in ordered]
    high: float | None = None
    drawdowns: list[float] = []
    for value in net_pnl:
        if high is None or value > high:
            high = value
        drawdowns.append(high - value)
    origin = timestamps[0] if timestamps else 0
    return {
        "timestamps_ns": timestamps,
        "net_pnl": net_pnl,
        "origin_ns": origin,
        "x": [(timestamp - origin) / 1_000_000_000 for timestamp in timestamps],
        "y": drawdowns,
        "row_count": len(ordered),
        "maximum": max(drawdowns, default=0.0),
        "terminal": drawdowns[-1] if drawdowns else None,
    }


def regime_increment_oracle(
    pnl: pl.DataFrame, market: pl.DataFrame
) -> dict[str, dict[str, float]]:
    """Canonical audit increments with the existing last-state-at-or-before rule.

    A forward market cursor supplies an independent as-of implementation. This
    retains the report's timestamp convention for integer audit timestamps; it
    does not infer causal labels or normalize arbitrary malformed artifacts.
    """

    if not {"timestamp_ns", "net_pnl"}.issubset(pnl.columns):
        return {}
    columns = [
        name
        for name in ("spread_regime", "volatility_regime", "liquidity_regime")
        if name in market.columns
    ]
    if "timestamp_ns" not in market.columns or not columns:
        return {}
    market_states = sorted(
        (
            (int(row[0]), dict(zip(columns, row[1:], strict=True)))
            for row in market.select("timestamp_ns", *columns).iter_rows()
            if row[0] is not None
        ),
        key=lambda state: state[0],
    )
    observations = sorted(
        (
            (int(timestamp), value)
            for timestamp, value in _finite_pairs(
                pnl.select("timestamp_ns", "net_pnl").rows()
            )
        ),
        key=lambda observation: observation[0],
    )
    totals: dict[str, dict[str, float]] = {column: {} for column in columns}
    previous: float | None = None
    market_index = 0
    current: dict[str, Any] | None = None
    for timestamp, value in observations:
        change = 0.0 if previous is None else value - previous
        previous = value
        while (
            market_index < len(market_states)
            and market_states[market_index][0] <= timestamp
        ):
            current = market_states[market_index][1]
            market_index += 1
        if current is None:
            continue
        for column in columns:
            label = current[column]
            if label is not None:
                key = str(label)
                totals[column][key] = totals[column].get(key, 0.0) + change
    return {
        column: dict(sorted(values.items()))
        for column, values in totals.items()
        if values
    }


def _capture_plot(
    plotter: Callable[[report_module.RunArtifacts, Path], Path],
    artifacts: report_module.RunArtifacts,
    png_path: Path,
) -> dict[str, Any]:
    original_save = report_module._save
    saved: list[dict[str, Any]] = []

    def save(figure: Any, path: Path) -> Path:
        record = {
            "axes": [
                {
                    "title": axis.get_title(),
                    "lines": [
                        {
                            "label": line.get_label(),
                            "x": [float(value) for value in line.get_xdata()],
                            "y": [float(value) for value in line.get_ydata()],
                        }
                        for line in axis.get_lines()
                    ],
                    "bars": [
                        {
                            "x": float(bar.get_x()),
                            "width": float(bar.get_width()),
                            "height": float(bar.get_height()),
                        }
                        for bar in axis.patches
                    ],
                    "x_tick_labels": [
                        label.get_text() for label in axis.get_xticklabels()
                    ],
                }
                for axis in figure.axes
            ],
            "figure_closed": False,
        }
        number = figure.number
        result = original_save(figure, path)
        record["figure_closed"] = not report_module.plt.fignum_exists(number)
        saved.append(record)
        return result

    with patch.object(report_module, "_save", save):
        returned_path = plotter(artifacts, png_path)
    assert returned_path == png_path
    assert len(saved) == 1
    assert saved[0]["figure_closed"] is True
    image = png_path.read_bytes()
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 8
    return {
        **saved[0],
        "png_path": str(png_path),
        "png_sha256": hashlib.sha256(image).hexdigest(),
        "png_bytes": len(image),
    }


def capture_drawdown_plot(
    artifacts: report_module.RunArtifacts, png_path: Path
) -> dict[str, Any]:
    """Capture every actual drawdown Line2D and save the original renderer PNG."""

    return _capture_plot(report_module._plot_drawdown, artifacts, png_path)


def capture_regime_plot(
    artifacts: report_module.RunArtifacts, png_path: Path
) -> dict[str, Any]:
    """Capture actual regime bar heights/labels and every accompanying Line2D."""

    return _capture_plot(report_module._plot_pnl_by_regime, artifacts, png_path)


def _artifacts(
    pnl: pl.DataFrame, market: pl.DataFrame | None = None
) -> report_module.RunArtifacts:
    tables = {"pnl": pnl}
    if market is not None:
        tables["market"] = market
    return report_module.RunArtifacts(
        run_directory=Path("."),
        metrics={},
        summary={},
        diagnostics={},
        config={},
        tables=tables,
    )


def _assert_drawdown_capture(captured: dict[str, Any], oracle: dict[str, Any]) -> None:
    assert len(captured["axes"]) == 1
    lines = captured["axes"][0]["lines"]
    assert len(lines) == 1
    assert lines[0]["x"] == oracle["x"]
    assert lines[0]["y"] == oracle["y"]
    assert len(lines[0]["x"]) == len(lines[0]["y"]) == oracle["row_count"]
    assert max(lines[0]["y"], default=0.0) == oracle["maximum"]
    assert (lines[0]["y"][-1] if lines[0]["y"] else None) == oracle["terminal"]


@pytest.mark.parametrize("case_name", tuple(_PLOT_CASES))
def test_frozen_drawdown_artist_keeps_audit_order_and_relative_time(
    case_name: str, tmp_path: Path
) -> None:
    rows, expected_x, expected_y = _PLOT_CASES[case_name]
    oracle = ordered_drawdown_oracle(rows)
    assert oracle["x"] == list(expected_x)
    assert oracle["y"] == list(expected_y)
    pnl = pl.DataFrame(rows, schema=["timestamp_ns", "net_pnl"], orient="row")
    original_rows = pnl.rows()
    captured = capture_drawdown_plot(_artifacts(pnl), tmp_path / f"{case_name}.png")
    _assert_drawdown_capture(captured, oracle)
    assert pnl.rows() == original_rows


def test_frozen_regime_increments_preserve_tied_audit_order(tmp_path: Path) -> None:
    pnl = pl.DataFrame(
        [(0, 0), (10, 2), (10, 1), (20, 2)],
        schema=["timestamp_ns", "net_pnl"],
        orient="row",
    )
    market = pl.DataFrame(
        [(0, "initial"), (10, "A"), (20, "B")],
        schema=["timestamp_ns", "spread_regime"],
        orient="row",
    )
    expected = {"spread_regime": {"A": 1.0, "B": 1.0, "initial": 0.0}}
    assert regime_increment_oracle(pnl, market) == expected
    actual = report_module._pnl_by_regime(pnl, market)
    captured = capture_regime_plot(_artifacts(pnl, market), tmp_path / "regime.png")
    assert actual == expected
    assert len(captured["axes"]) == 1
    axis = captured["axes"][0]
    assert axis["x_tick_labels"] == ["A", "B", "initial"]
    assert [bar["height"] for bar in axis["bars"]] == [1.0, 1.0, 0.0]
    assert sum(bar["height"] for bar in axis["bars"]) == 2.0
    assert len(axis["lines"]) == 1
    assert axis["lines"][0]["y"] == [0.0, 0.0]


def test_default_numeric_pairs_and_finite_filtering_stay_unchanged(
    tmp_path: Path,
) -> None:
    rows = [
        (_EPOCH, 1),
        (2.5, 3.0),
        ("7", "4"),
        (True, 5.0),
        (None, 6),
        (8, None),
        (math.nan, 7),
        (math.inf, 8),
        (9, math.inf),
        ("invalid", 9),
        (10, "invalid"),
    ]
    frame = pl.DataFrame(
        {
            "timestamp_ns": pl.Series([row[0] for row in rows], dtype=pl.Object),
            "net_pnl": pl.Series([row[1] for row in rows], dtype=pl.Object),
        }
    )
    actual = report_module._numeric_pairs(
        frame, x_columns=("timestamp_ns",), y_columns=("net_pnl",)
    )
    assert actual == [(float(_EPOCH), 1.0), (2.5, 3.0), (7.0, 4.0), (1.0, 5.0)]
    assert all(
        type(timestamp) is float and type(value) is float for timestamp, value in actual
    )
    # The actual drawdown path applies the same finite filtering; only genuine
    # integer timestamps are eligible for integer preservation.
    oracle = ordered_drawdown_oracle(rows)
    captured = capture_drawdown_plot(_artifacts(frame), tmp_path / "finite-filter.png")
    _assert_drawdown_capture(captured, oracle)
    assert oracle["row_count"] == 4
