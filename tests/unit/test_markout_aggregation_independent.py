"""Frozen gross forward-markout aggregation oracle and compatibility checks.

No case is evaluated on import. Explicit build_case calls the unchanged raw
producer only when the archive runner or pytest invokes it. Weighted sums use
independent rational arithmetic on supplied outcomes, never candidate helpers.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from statistics import fmean
from typing import Any

import pytest

from lobmm.metrics import build_metrics, compute_markouts

FIXTURES_SHA256 = "06999812efaab9357948bf3dd1de463f3dc2166b6d4786115f9d578a9446c5a5"
CASE_NAMES = (
    "single_horizon",
    "mixed_horizons",
    "split_single",
    "split_mixed",
    "ask_mirror",
    "mixed_sides",
    "censored_late",
    "horizon_censoring",
    "all_missing",
    "duplicate_horizon",
    "empty",
)
INTEGER_FIELDS = {
    "total_fill_quantity",
    "observed_quantity",
    "missing_quantity",
    "total_fill_events",
    "observed_fill_events",
    "missing_fill_events",
}
VALUE_FIELDS = {
    "quantity_coverage",
    "quantity_weighted_markout_ticks",
    "quantity_weighted_markout_value",
    "observed_markout_value",
}
OLD_ATTRIBUTION_NOTE = (
    "Attribution is an analytical approximation; inventory "
    "mark-to-market is the reconciliation residual."
)


def _document(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(fixtures_path)
        if fixtures_path is not None
        else (
            Path(__file__).resolve().parents[2]
            / "docs/evidence/markout-aggregation-20261108/fixtures.json"
        )
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURES_SHA256:
        raise ValueError("frozen markout fixture hash changed")
    return json.loads(path.read_text())


def load_cases(fixtures_path: str | Path | None = None) -> dict[str, Any]:
    """Return unevaluated frozen case specifications."""
    return deepcopy(_document(fixtures_path)["cases"])


def build_case(
    case_name: str, fixtures_path: str | Path | None = None
) -> dict[str, Any]:
    """Explicitly generate raw markout rows; never called merely by import."""
    document = _document(fixtures_path)
    specification = deepcopy(document["cases"][case_name])
    fills = specification["fills"]
    market = specification["market_states"]
    horizons = specification["horizons_ns"]
    tick = Decimal(specification["tick_size"])
    markouts = compute_markouts(fills, market, horizons, tick_size=tick)
    arguments = deepcopy(document["common_build_metrics"])
    arguments.update(
        fills=fills, market_states=market, markouts=markouts, tick_size=tick
    )
    return {
        "name": case_name,
        "fills": fills,
        "market_states": market,
        "horizons_ns": horizons,
        "tick_size": tick,
        "markouts": markouts,
        "build_args": arguments,
    }


def normalize_evidence(value: Any) -> Any:
    """Preserve nonfinite mutation provenance without nonstandard JSON floats."""
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "nonfinite": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"
        }
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, Mapping):
        return {str(key): normalize_evidence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_evidence(item) for item in value]
    return value


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _rational(value: float) -> Fraction:
    # Interpret the actual supplied finite binary64 observation exactly.
    return Fraction.from_float(value)


def _duplicate_signature(row: Mapping[str, Any], fill: Mapping[str, Any]) -> Any:
    # Normalize each outcome independently. A finite counterpart remains part
    # of the identity even when the pair cannot qualify as observed.
    return normalize_evidence(
        {
            "side": int(row["side"]),
            "fill_timestamp_ns": row.get(
                "fill_timestamp_ns", fill.get("exchange_fill_timestamp_ns")
            ),
            "fill_price_ticks": float(
                row.get("fill_price_ticks", fill.get("price_ticks"))
            ),
            "markout_ticks": _finite(row.get("markout_ticks")),
            "markout_value": _finite(row.get("markout_value")),
            "target_timestamp_ns": row.get("target_timestamp_ns"),
            "future_timestamp_ns": row.get("future_timestamp_ns"),
            "future_midpoint_ticks": row.get("future_midpoint_ticks"),
            "markout_bps": row.get("markout_bps"),
            "asof_convention": row.get("asof_convention"),
        }
    )


def independent_quantity_summary(
    fills: Sequence[Mapping[str, Any]], markouts: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Resolve unique outcomes, then sum each horizon/group independently."""
    if not markouts:
        return {}
    catalog: dict[str, Mapping[str, Any]] = {}
    for fill in fills:
        identifier = fill.get("fill_id")
        if not isinstance(identifier, str) or not identifier or identifier in catalog:
            raise ValueError("authoritative fill IDs must be unique and nonempty")
        if type(fill.get("quantity")) is not int or fill["quantity"] <= 0:
            raise ValueError(
                "authoritative quantity must be a positive builtin integer"
            )
        if isinstance(fill.get("side"), bool) or fill.get("side") not in (1, -1):
            raise ValueError("authoritative side must be bid or ask")
        catalog[identifier] = fill
    outcomes: dict[tuple[str, int], Mapping[str, Any]] = {}
    signatures: dict[tuple[str, int], Any] = {}
    for row in markouts:
        identifier = row.get("fill_id")
        if identifier not in catalog:
            raise ValueError("markout fill ID is unknown")
        fill = catalog[identifier]
        horizon = row.get("horizon_ns")
        if type(horizon) is not int or horizon <= 0:
            raise ValueError("horizon must be a positive builtin integer")
        if isinstance(row.get("side"), bool) or row.get("side") != fill["side"]:
            raise ValueError("markout side differs from authoritative fill")
        if (
            "fill_timestamp_ns" in row
            and row["fill_timestamp_ns"] != fill["exchange_fill_timestamp_ns"]
        ):
            raise ValueError("markout fill timestamp differs")
        # The existing producer stores float(price), so the join must retain
        # that existing representation even for integer ticks above 2**53.
        if "fill_price_ticks" in row and row["fill_price_ticks"] != float(
            fill["price_ticks"]
        ):
            raise ValueError("markout fill price differs")
        key = (identifier, horizon)
        signature = _duplicate_signature(row, fill)
        if key in signatures and signatures[key] != signature:
            raise ValueError("duplicate fill/horizon outcomes conflict")
        signatures[key] = signature
        outcomes[key] = row
    result = {}
    for horizon in sorted({h for _, h in outcomes}):
        groups = {}
        for name, side in (("all", None), ("bid", 1), ("ask", -1)):
            universe = [
                fill
                for fill in catalog.values()
                if side is None or fill["side"] == side
            ]
            observed = []
            for fill in universe:
                row = outcomes.get((fill["fill_id"], horizon))
                if row is None:
                    continue
                ticks, value = (
                    _finite(row.get("markout_ticks")),
                    _finite(row.get("markout_value")),
                )
                if ticks is not None and value is not None:
                    observed.append((fill["quantity"], ticks, value))
            total = sum(fill["quantity"] for fill in universe)
            covered = sum(quantity for quantity, _, _ in observed)
            ticks_total = sum(
                (quantity * _rational(ticks) for quantity, ticks, _ in observed),
                Fraction(),
            )
            value_total = sum(
                (quantity * _rational(value) for quantity, _, value in observed),
                Fraction(),
            )
            groups[name] = {
                "total_fill_quantity": total,
                "observed_quantity": covered,
                "missing_quantity": total - covered,
                "quantity_coverage": float(Fraction(covered, total)) if total else None,
                "total_fill_events": len(universe),
                "observed_fill_events": len(observed),
                "missing_fill_events": len(universe) - len(observed),
                "quantity_weighted_markout_ticks": float(ticks_total / covered)
                if covered
                else None,
                "quantity_weighted_markout_value": float(value_total / covered)
                if covered
                else None,
                "observed_markout_value": float(value_total) if covered else None,
            }
        result[str(horizon)] = groups
    return result


def expected_legacy_proxy(
    fills: Sequence[Mapping[str, Any]], markouts: Sequence[Mapping[str, Any]]
) -> float:
    values = [
        number
        for row in markouts
        if (number := _finite(row.get("markout_value"))) is not None
    ]
    mean = fmean(values) if values else None
    return (
        (mean or 0.0) * sum(int(fill["quantity"]) for fill in fills)
        if markouts
        else 0.0
    )


def expected_event_means(markouts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def mean(rows: Sequence[Mapping[str, Any]]) -> float | None:
        values = [
            number
            for row in rows
            if (number := _finite(row.get("markout_ticks"))) is not None
        ]
        return fmean(values) if values else None

    return {
        "markout_ticks_by_horizon_ns": {
            str(horizon): mean(
                [row for row in markouts if int(row["horizon_ns"]) == horizon]
            )
            for horizon in sorted({int(row["horizon_ns"]) for row in markouts})
        },
        "markout_ticks_by_side": {
            name: mean([row for row in markouts if int(row["side"]) == side])
            for name, side in (("bid", 1), ("ask", -1))
        },
    }


def assert_metrics_match(
    fills: Sequence[Mapping[str, Any]],
    markouts: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    *,
    abs_tol: float = 1e-9,
    rel_tol: float = 1e-12,
) -> None:
    expected = independent_quantity_summary(fills, markouts)
    actual = metrics["execution_quality"]["markout_summary_by_horizon_ns"]
    assert actual.keys() == expected.keys()
    for horizon, groups in expected.items():
        assert actual[horizon].keys() == groups.keys()
        for name, record in groups.items():
            assert actual[horizon][name].keys() == record.keys()
            for field, wanted in record.items():
                found = actual[horizon][name][field]
                if field in INTEGER_FIELDS:
                    assert type(found) is int and found == wanted
                elif wanted is None:
                    assert found is None
                else:
                    assert found is not None and math.isfinite(found)
                    assert math.isclose(found, wanted, abs_tol=abs_tol, rel_tol=rel_tol)
    for field, values in expected_event_means(markouts).items():
        assert metrics["execution_quality"][field] == values
    assert metrics["attribution"]["adverse_selection_markout"] is None
    assert metrics["attribution"][
        "legacy_pooled_markout_proxy"
    ] == expected_legacy_proxy(fills, markouts)
    assert metrics["attribution"]["note"] == OLD_ATTRIBUTION_NOTE
    assert isinstance(metrics["execution_quality"]["markout_aggregation_note"], str)
    assert metrics["execution_quality"]["markout_aggregation_note"].strip()
    assert isinstance(metrics["attribution"]["adverse_selection_markout_note"], str)
    assert metrics["attribution"]["adverse_selection_markout_note"].strip()


def _metrics(case: Mapping[str, Any]) -> dict[str, Any]:
    arguments = deepcopy(case["build_args"])
    arguments.update(fills=case["fills"], markouts=case["markouts"])
    return build_metrics(**arguments)


def _assert_case(case: dict[str, Any], *, exact: bool = True) -> dict[str, Any]:
    before = normalize_evidence(deepcopy(case))
    metrics = _metrics(case)
    assert_metrics_match(case["fills"], case["markouts"], metrics)
    if exact:
        assert metrics["execution_quality"][
            "markout_summary_by_horizon_ns"
        ] == independent_quantity_summary(case["fills"], case["markouts"])
    assert normalize_evidence(case) == before
    return metrics


@pytest.mark.parametrize("name", CASE_NAMES)
def test_frozen_cases_match_every_horizon_side_and_legacy_field(name: str) -> None:
    _assert_case(build_case(name))


def test_hand_expected_single_horizon_and_split_expose_proxy_instability() -> None:
    base, split = build_case("single_horizon"), build_case("split_single")
    original, children = _assert_case(base), _assert_case(split)
    a = original["execution_quality"]["markout_summary_by_horizon_ns"]["10"]["all"]
    b = children["execution_quality"]["markout_summary_by_horizon_ns"]["10"]["all"]
    assert a["observed_markout_value"] == b["observed_markout_value"] == -2.0
    assert (
        a["quantity_weighted_markout_ticks"]
        == b["quantity_weighted_markout_ticks"]
        == -0.8
    )
    assert a["total_fill_quantity"] == b["total_fill_quantity"] == 10
    assert a["observed_fill_events"] == 2 and b["observed_fill_events"] == 10
    assert original["attribution"]["legacy_pooled_markout_proxy"] == 10.0
    assert children["attribution"]["legacy_pooled_markout_proxy"] == -2.0


@pytest.mark.parametrize(
    ("base_name", "split_name"),
    [("single_horizon", "split_single"), ("mixed_horizons", "split_mixed")],
)
def test_identical_fill_splitting_preserves_quantity_estimands_not_event_counts(
    base_name: str, split_name: str
) -> None:
    base, split = build_case(base_name), build_case(split_name)
    a, b = _assert_case(base), _assert_case(split)
    left, right = (
        m["execution_quality"]["markout_summary_by_horizon_ns"] for m in (a, b)
    )
    for horizon in left:
        for side in left[horizon]:
            for field in VALUE_FIELDS | {
                "total_fill_quantity",
                "observed_quantity",
                "missing_quantity",
            }:
                assert left[horizon][side][field] == right[horizon][side][field]


def test_horizon_addition_and_duplicate_requests_leave_each_identified_summary_unchanged() -> (
    None
):
    single, mixed, duplicate = [
        build_case(name)
        for name in ("single_horizon", "mixed_horizons", "duplicate_horizon")
    ]
    results = [_assert_case(case) for case in (single, mixed, duplicate)]
    tables = [m["execution_quality"]["markout_summary_by_horizon_ns"] for m in results]
    assert tables[0]["10"] == tables[1]["10"] == tables[2]["10"]
    assert tables[1]["30"] == tables[2]["30"]
    assert len(duplicate["markouts"]) == 6 and len(mixed["markouts"]) == 4
    assert [m["attribution"]["legacy_pooled_markout_proxy"] for m in results] == [
        10.0,
        6.25,
        5.0,
    ]


def test_mirrored_sell_signs_match_buy_estimands_in_the_corresponding_side() -> None:
    bid, ask = [
        _assert_case(build_case(name))["execution_quality"][
            "markout_summary_by_horizon_ns"
        ]
        for name in ("mixed_horizons", "ask_mirror")
    ]
    for horizon in bid:
        assert bid[horizon]["all"] == ask[horizon]["all"]
        assert bid[horizon]["bid"] == ask[horizon]["ask"]
        assert bid[horizon]["ask"] == ask[horizon]["bid"]


def test_censored_quantity_lowers_coverage_without_extrapolating_observed_amount() -> (
    None
):
    mixed, censored = [
        _assert_case(build_case(name)) for name in ("mixed_horizons", "censored_late")
    ]
    for horizon in ("10", "30"):
        a, b = [
            m["execution_quality"]["markout_summary_by_horizon_ns"][horizon]["all"]
            for m in (mixed, censored)
        ]
        assert a["observed_markout_value"] == b["observed_markout_value"]
        assert b["total_fill_quantity"] == 110 and b["observed_quantity"] == 10
        assert b["quantity_coverage"] == float(Fraction(1, 11))
    assert censored["attribution"]["legacy_pooled_markout_proxy"] == 68.75


def test_horizons_report_different_observation_support_without_pooled_amount() -> None:
    summary = _assert_case(build_case("horizon_censoring"))["execution_quality"][
        "markout_summary_by_horizon_ns"
    ]
    assert summary["10"]["all"]["observed_quantity"] == 14
    assert summary["30"]["all"]["observed_quantity"] == 10
    assert summary["10"]["all"]["observed_markout_value"] == 2.0
    assert summary["30"]["all"]["observed_markout_value"] == 8.5


def test_zero_observed_amount_is_distinct_from_all_missing_and_empty_group() -> None:
    zero = _assert_case(build_case("mixed_sides"))["execution_quality"][
        "markout_summary_by_horizon_ns"
    ]["10"]["all"]
    assert (
        zero["observed_markout_value"] == zero["quantity_weighted_markout_value"] == 0.0
    )
    missing = _assert_case(build_case("all_missing"))["execution_quality"][
        "markout_summary_by_horizon_ns"
    ]["1000"]
    assert missing["all"]["observed_markout_value"] is None
    assert missing["all"]["quantity_coverage"] == 0.0
    assert missing["ask"]["total_fill_quantity"] == 0
    assert missing["ask"]["quantity_coverage"] is None


def test_missing_markout_row_is_unobserved_within_an_identified_horizon() -> None:
    case = build_case("mixed_horizons")
    case["markouts"] = [
        row
        for row in case["markouts"]
        if not (row["fill_id"] == "B" and row["horizon_ns"] == 30)
    ]
    summary = _assert_case(case)["execution_quality"]["markout_summary_by_horizon_ns"][
        "30"
    ]["all"]
    assert summary["observed_quantity"] == 1 and summary["missing_quantity"] == 9
    assert (
        summary["observed_markout_value"] == -0.5
        and summary["quantity_coverage"] == 0.1
    )


@pytest.mark.parametrize("kind", ["nan", "inf", "none"])
def test_normalized_nonfinite_paired_outcome_is_missing_not_imputed(kind: str) -> None:
    case = build_case("single_horizon")
    value = None if kind == "none" else float(kind)
    case["markouts"][0].update(markout_ticks=value, markout_value=value)
    summary = _assert_case(case)["execution_quality"]["markout_summary_by_horizon_ns"][
        "10"
    ]["all"]
    assert (
        summary["observed_quantity"] == 9 and summary["observed_markout_value"] == -4.5
    )
    assert summary["quantity_coverage"] == 0.9


@pytest.mark.parametrize("field", ["markout_ticks", "markout_value"])
def test_asymmetric_missingness_requires_both_fields_but_preserves_legacy_masks(
    field: str,
) -> None:
    case = build_case("single_horizon")
    case["markouts"][0][field] = None
    summary = _assert_case(case)["execution_quality"]["markout_summary_by_horizon_ns"][
        "10"
    ]["all"]
    assert (
        summary["observed_quantity"] == 9 and summary["observed_markout_value"] == -4.5
    )


def test_provided_per_unit_currency_is_not_recomputed_from_tick_outcome() -> None:
    case = build_case("single_horizon")
    case["markouts"][0]["markout_value"] = 3.0
    summary = _assert_case(case)["execution_quality"]["markout_summary_by_horizon_ns"][
        "10"
    ]["all"]
    assert summary["quantity_weighted_markout_ticks"] == -0.8
    assert summary["observed_markout_value"] == -1.5


def test_consistent_normalized_missing_duplicate_requests_are_accepted() -> None:
    case = build_case("single_horizon")
    case["markouts"][0].update(markout_ticks=float("nan"), markout_value=float("inf"))
    duplicate = deepcopy(case["markouts"][0])
    duplicate.update(markout_ticks=None, markout_value=None)
    case["markouts"].append(duplicate)
    summary = _assert_case(case)["execution_quality"]["markout_summary_by_horizon_ns"][
        "10"
    ]["all"]
    assert summary["observed_fill_events"] == 1 and summary["observed_quantity"] == 9


@pytest.mark.parametrize(
    "mutation", ["outcome", "future-reference", "finite-counterpart"]
)
def test_conflicting_duplicate_outcome_or_reference_is_rejected(mutation: str) -> None:
    case = build_case("single_horizon")
    duplicate = deepcopy(case["markouts"][1])
    if mutation == "outcome":
        duplicate["markout_ticks"] += 1
        duplicate["markout_value"] += float(case["tick_size"])
    elif mutation == "future-reference":
        duplicate["future_timestamp_ns"] += 1
    else:
        case["markouts"][1].update(markout_ticks=1.0, markout_value=None)
        duplicate.update(markout_ticks=2.0, markout_value=None)
    case["markouts"].append(duplicate)
    before = normalize_evidence(deepcopy(case))
    with pytest.raises(ValueError):
        _metrics(case)
    assert normalize_evidence(case) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-fill",
        "unknown-fill",
        "side",
        "fill-time",
        "fill-price",
        "zero-quantity",
        "negative-quantity",
        "boolean-quantity",
        "zero-horizon",
        "negative-horizon",
        "boolean-horizon",
    ],
)
def test_predefined_invalid_authoritative_join_is_rejected(mutation: str) -> None:
    case = build_case("single_horizon")
    if mutation == "duplicate-fill":
        case["fills"].append(deepcopy(case["fills"][0]))
    elif mutation == "unknown-fill":
        case["markouts"][0]["fill_id"] = "unknown"
    elif mutation == "side":
        case["markouts"][0]["side"] = -1
    elif mutation == "fill-time":
        case["markouts"][0]["fill_timestamp_ns"] += 1
    elif mutation == "fill-price":
        case["markouts"][0]["fill_price_ticks"] += 1
    elif mutation.endswith("quantity"):
        case["fills"][0]["quantity"] = {
            "zero-quantity": 0,
            "negative-quantity": -1,
            "boolean-quantity": True,
        }[mutation]
    else:
        case["markouts"][0]["horizon_ns"] = {
            "zero-horizon": 0,
            "negative-horizon": -1,
            "boolean-horizon": True,
        }[mutation]
    before = normalize_evidence(deepcopy(case))
    with pytest.raises(ValueError):
        _metrics(case)
    assert normalize_evidence(case) == before


def test_unknown_horizon_does_not_add_fill_join_restrictions_to_legacy_metrics() -> (
    None
):
    case = build_case("single_horizon")
    case["markouts"] = []
    for fill in case["fills"]:
        del fill["fill_id"]
    metrics = _assert_case(case)
    assert metrics["execution_quality"]["markout_summary_by_horizon_ns"] == {}


def test_large_integer_fill_price_uses_the_existing_raw_float_identity() -> None:
    case = build_case("single_horizon")
    price = 2**53 + 1
    case["fills"] = [
        {
            "fill_id": "large",
            "side": 1,
            "quantity": 1,
            "price_ticks": price,
            "exchange_fill_timestamp_ns": 0,
            "liquidity_role": "maker",
        }
    ]
    case["market_states"] = [
        {
            "timestamp_ns": 10,
            "midpoint_ticks": float(price + 3),
            "spread_ticks": 2,
            "best_bid_quantity": 1,
            "best_ask_quantity": 1,
        }
    ]
    case["markouts"] = compute_markouts(
        case["fills"], case["market_states"], [10], tick_size=case["tick_size"]
    )
    case["build_args"]["market_states"] = case["market_states"]
    assert case["markouts"][0]["fill_price_ticks"] == float(price)
    _assert_case(case)
