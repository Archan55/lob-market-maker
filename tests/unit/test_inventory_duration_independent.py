"""Frozen inventory-duration fixtures and an independent integer-time oracle.

The oracle partitions the explicit window, then performs a separate as-of scan
for each interval. It neither imports a duration helper nor folds production
state transitions. All examples are engineering fixtures, not market evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from lobmm.case_study import _case_study_markdown
from lobmm.metrics import build_metrics
from lobmm.report import RunArtifacts

_START = "session_start_timestamp_ns"
_END = "session_end_timestamp_ns"
_TIME_FIELDS = {
    "time_near_limits_fraction",
    "near_limit_duration_ns",
    "observation_duration_ns",
    "time_window_start_ns",
    "time_window_end_ns",
}


@dataclass(frozen=True)
class DurationCase:
    name: str
    start: int
    end: int
    states: tuple[tuple[int, int], ...]
    near: int


_CORE = (
    DurationCase("short-near", 100, 110, ((100, 5), (101, 0)), 1),
    DurationCase("long-near", 100, 110, ((100, 5), (109, 0)), 9),
    DurationCase("duplicate-near", 100, 110, ((100, 5), (100, 5), (101, 0)), 1),
    DurationCase("same-time-final-far", 100, 110, ((100, 5), (100, 0), (109, -5)), 1),
    DurationCase("delayed-first", 100, 110, ((103, -5), (108, 0)), 5),
    DurationCase("pre-window-carry", 100, 110, ((90, 5), (105, 0)), 5),
    DurationCase("endpoint-postend", 100, 110, ((100, 5), (110, 0), (112, 5)), 10),
    DurationCase("carry-no-final", 100, 110, ((102, 5),), 8),
    DurationCase("empty", 100, 110, (), 0),
    DurationCase(
        "large-epoch",
        1_000_000_000_000_000_100,
        1_000_000_000_000_000_110,
        ((1_000_000_000_000_000_100, 5), (1_000_000_000_000_000_101, 0)),
        1,
    ),
)


def _rows(states: tuple[tuple[int, int], ...]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp_ns": timestamp,
            "inventory": quantity,
            "review": {"position": index},
        }
        for index, (timestamp, quantity) in enumerate(states)
    ]


def partition_duration_oracle(
    rows: Sequence[Mapping[str, Any]],
    start: int,
    end: int,
    *,
    maximum_limit: int = 10,
    near_limit_fraction: float = 0.5,
) -> tuple[int | None, int, Fraction | None]:
    """Independent valid-window oracle, reusable on preserved inventory rows."""

    duration = end - start
    if maximum_limit <= 0:
        return None, duration, None
    boundaries = sorted(
        {start, end}
        | {row["timestamp_ns"] for row in rows if start < row["timestamp_ns"] < end}
    )
    total = 0
    for left, right in pairwise(boundaries):
        previous = [row for row in rows if row["timestamp_ns"] <= left]
        # Scanning original order preserves the final state at equal times.
        quantity = int(previous[-1].get("inventory", 0)) if previous else 0
        if abs(quantity) >= near_limit_fraction * maximum_limit:
            total += right - left
    return total, duration, Fraction(total, duration) if duration else None


def _legacy_observations(rows: list[dict[str, Any]], maximum_limit: int) -> float:
    if not rows or not maximum_limit:
        return 0.0
    return sum(
        abs(int(row.get("inventory", 0))) >= 0.5 * maximum_limit for row in rows
    ) / len(rows)


def _build(
    rows: list[dict[str, Any]],
    *,
    bounds: dict[str, object] | None = None,
    limit: int | None = 10,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    if limit is not None:
        diagnostics["max_abs_inventory"] = limit
    if bounds is not None:
        diagnostics.update(bounds)
    return build_metrics(
        orders=[],
        fills=[],
        inventory=rows,
        pnl=[],
        market_states=[],
        markouts=[],
        risk_events=[],
        quote_messages=[],
        diagnostics=diagnostics,
        tick_size=Decimal("0.01"),
        near_limit_fraction=0.5,
    )


def _without_time(metrics: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(metrics)
    for name in _TIME_FIELDS:
        result["inventory"].pop(name)
    return result


@pytest.mark.parametrize("case", _CORE, ids=lambda case: case.name)
def test_frozen_duration_fixtures_match_independent_partition_oracle(
    case: DurationCase,
) -> None:
    rows = _rows(case.states)
    before = deepcopy(rows)
    expected, duration, fraction = partition_duration_oracle(rows, case.start, case.end)
    assert expected == case.near
    metrics = _build(rows, bounds={_START: case.start, _END: case.end})
    inventory = metrics["inventory"]
    assert type(inventory["near_limit_duration_ns"]) is int
    assert type(inventory["observation_duration_ns"]) is int
    assert inventory["near_limit_duration_ns"] == case.near
    assert inventory["observation_duration_ns"] == duration == case.end - case.start
    assert inventory["time_window_start_ns"] == case.start
    assert inventory["time_window_end_ns"] == case.end
    assert fraction is not None
    assert inventory["time_near_limits_fraction"] == float(fraction)
    assert inventory["observations_near_limits_fraction"] == _legacy_observations(
        rows, 10
    )
    missing_window = _build(rows)
    assert _without_time(metrics) == _without_time(missing_window)
    assert rows == before


def test_spacing_contrast_and_same_timestamp_duplicate_invariance() -> None:
    short = _rows(_CORE[0].states)
    long = _rows(_CORE[1].states)
    duplicate = _rows(_CORE[2].states)
    results = [
        _build(rows, bounds={_START: 100, _END: 110})["inventory"]
        for rows in (short, long, duplicate)
    ]
    assert [result["time_near_limits_fraction"] for result in results] == [
        0.1,
        0.9,
        0.1,
    ]
    assert [result["observations_near_limits_fraction"] for result in results] == [
        0.5,
        0.5,
        2 / 3,
    ]
    for result in results[:2]:
        # These established inventory statistics deliberately remain row based.
        assert result["mean"] == result["mean_absolute"] == 2.5
        assert result["standard_deviation"] == 2.5
        assert result["maximum_long"] == 5
        assert result["maximum_short"] == result["end_of_session"] == 0


def test_both_signs_are_included_at_the_unchanged_near_limit_boundary() -> None:
    rows = _rows(((100, -5), (104, 4), (106, 5), (110, -6)))
    result = _build(rows, bounds={_START: 100, _END: 110})["inventory"]
    assert partition_duration_oracle(rows, 100, 110) == (8, 10, Fraction(4, 5))
    assert result["near_limit_duration_ns"] == 8
    assert result["time_near_limits_fraction"] == 0.8
    assert result["observations_near_limits_fraction"] == 0.75


@pytest.mark.parametrize("bounds", [None, {_START: None, _END: None}])
def test_missing_window_has_no_duration_metadata_or_silent_row_fallback(
    bounds: dict[str, object] | None,
) -> None:
    # Untimestamped legacy rows remain usable for observation statistics when
    # no duration window has been supplied.
    rows = [{"inventory": 5}, {"inventory": 0}]
    before = deepcopy(rows)
    result = _build(rows, bounds=bounds)["inventory"]
    assert {name: result[name] for name in _TIME_FIELDS} == dict.fromkeys(_TIME_FIELDS)
    assert result["observations_near_limits_fraction"] == 0.5
    assert rows == before


def test_zero_duration_has_zero_integer_durations_and_undefined_fraction() -> None:
    rows = _rows(((99, 5), (100, 0), (101, 5)))
    result = _build(rows, bounds={_START: 100, _END: 100})["inventory"]
    assert result["observation_duration_ns"] == 0
    assert result["near_limit_duration_ns"] == 0
    assert result["time_near_limits_fraction"] is None
    assert result["time_window_start_ns"] == result["time_window_end_ns"] == 100
    assert result["observations_near_limits_fraction"] == 2 / 3


@pytest.mark.parametrize("limit", [None, 0, -1], ids=["missing", "zero", "negative"])
def test_unknown_position_limit_retains_window_but_has_no_near_duration(
    limit: int | None,
) -> None:
    rows = _rows(((100, 5), (101, 0)))
    result = _build(rows, bounds={_START: 100, _END: 110}, limit=limit)["inventory"]
    assert result["observation_duration_ns"] == 10
    assert result["near_limit_duration_ns"] is None
    assert result["time_near_limits_fraction"] is None
    assert result["time_window_start_ns"] == 100
    assert result["time_window_end_ns"] == 110
    # Preserve even the old negative-limit observation-estimator behavior.
    assert result["observations_near_limits_fraction"] == _legacy_observations(
        rows, limit or 0
    )


@pytest.mark.parametrize(
    "bounds",
    [
        {_START: 100},
        {_END: 110},
        {_START: None, _END: 110},
        {_START: True, _END: 110},
        {_START: 100, _END: False},
        {_START: 100.0, _END: 110},
        {_START: 100, _END: "110"},
        {_START: -1, _END: 110},
        {_START: 100, _END: 99},
    ],
    ids=[
        "missing-end",
        "missing-start",
        "none-start",
        "boolean-start",
        "boolean-end",
        "float-start",
        "string-end",
        "negative-start",
        "reversed",
    ],
)
def test_malformed_window_is_rejected_without_mutating_input(
    bounds: dict[str, object],
) -> None:
    rows = _rows(((100, 5),))
    before = deepcopy(rows)
    with pytest.raises(ValueError):
        _build(rows, bounds=bounds)
    assert rows == before


@pytest.mark.parametrize(
    "rows",
    [
        [{"inventory": 5}],
        [{"timestamp_ns": None, "inventory": 5}],
        [{"timestamp_ns": True, "inventory": 5}],
        [{"timestamp_ns": 100.0, "inventory": 5}],
        [{"timestamp_ns": "100", "inventory": 5}],
        [{"timestamp_ns": -1, "inventory": 5}],
        [{"timestamp_ns": 101, "inventory": 0}, {"timestamp_ns": 100, "inventory": 5}],
        [
            {"timestamp_ns": 100, "inventory": 0},
            {"timestamp_ns": "112", "inventory": 5},
        ],
    ],
    ids=[
        "missing",
        "none",
        "boolean",
        "float",
        "string",
        "negative",
        "decreasing",
        "invalid-postend",
    ],
)
def test_explicit_window_requires_all_timestamps_to_be_valid_and_ordered(
    rows: list[dict[str, Any]],
) -> None:
    before = deepcopy(rows)
    with pytest.raises(ValueError):
        _build(rows, bounds={_START: 100, _END: 110})
    assert rows == before


@pytest.mark.parametrize(
    ("end", "limit"), [(100, 10), (110, 0)], ids=["zero-duration", "unknown-limit"]
)
def test_undefined_fraction_does_not_skip_explicit_timestamp_validation(
    end: int, limit: int
) -> None:
    rows = [{"inventory": 5}]
    before = deepcopy(rows)
    with pytest.raises(ValueError):
        _build(rows, bounds={_START: 100, _END: end}, limit=limit)
    assert rows == before


def test_malformed_window_is_reported_before_invalid_row_timestamps() -> None:
    rows = [{"timestamp_ns": "invalid", "inventory": 5}]
    before = deepcopy(rows)
    with pytest.raises(ValueError, match="(?i)window|session|bound|start|end"):
        _build(rows, bounds={_START: 110, _END: 100})
    assert rows == before


@pytest.mark.parametrize(
    "known_window", [False, True], ids=["undefined-n-a", "true-zero-percent"]
)
def test_case_table_distinguishes_missing_time_from_zero(known_window: bool) -> None:
    rows = _rows(((100, 0), (101, 0)))
    metrics = _build(rows, bounds={_START: 100, _END: 110} if known_window else None)
    run = RunArtifacts(
        run_directory=Path("in-memory-duration-review"),
        metrics=metrics,
        summary={"strategy": "fixed_spread"},
        diagnostics={},
        config={},
        tables={},
    )
    text = _case_study_markdown(
        [run],
        synthetic=True,
        assets_relative=Path("assets"),
        experiment_manifest={},
        event_stream_hash="a" * 64,
        sensitivity_interpretation="",
    )
    line = next(
        line for line in text.splitlines() if line.startswith("| fixed_spread |")
    )
    cells = [cell.strip() for cell in line.split("|")[1:-1]]
    assert cells[4].lower() == ("0.0%" if known_window else "n/a")
