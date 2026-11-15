"""One preregistered exact-quantity representation counterexample.

The finite per-unit outcomes remain the raw producer's existing binary64
values. Only their integer quantity weighting and final division are exact.
This completed-row fixture is not a physical tape or market advantage claim.
"""

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from statistics import fmean
from typing import Any

from lobmm.metrics import build_metrics, compute_markouts


def _completed_case(split: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    big = 2**53
    original = {
        "fill_id": "A",
        "side": 1,
        "quantity": big + 1,
        "price_ticks": 100,
        "exchange_fill_timestamp_ns": 0,
        "liquidity_role": "maker",
    }
    fills = (
        [
            dict(original, fill_id="A-big", quantity=big),
            dict(original, fill_id="A-unit", quantity=1),
        ]
        if split
        else [original]
    )
    fills.append(
        {
            "fill_id": "B",
            "side": 1,
            "quantity": big,
            "price_ticks": 100,
            "exchange_fill_timestamp_ns": 20,
            "liquidity_role": "maker",
        }
    )
    market = [
        {"timestamp_ns": 0, "midpoint_ticks": 100},
        {"timestamp_ns": 10, "midpoint_ticks": 101},
        {"timestamp_ns": 20, "midpoint_ticks": 100},
        {"timestamp_ns": 30, "midpoint_ticks": 99},
    ]
    tick = Decimal("0.25")
    markouts = compute_markouts(fills, market, [10], tick_size=tick)
    arguments = {
        "orders": [],
        "fills": fills,
        "inventory": [],
        "pnl": [],
        "market_states": market,
        "markouts": markouts,
        "risk_events": [],
        "quote_messages": [],
        "diagnostics": {},
        "tick_size": tick,
    }
    before = deepcopy(arguments)
    metrics = build_metrics(**arguments)
    assert arguments == before
    return metrics, markouts


def test_large_quantity_split_preserves_exact_observed_markout_amount_and_means() -> (
    None
):
    # Compute both representations before checking the introduced defect, so
    # a failing baseline log retains its full [unsplit, split] amount pair.
    results = [_completed_case(split) for split in (False, True)]
    summaries = [
        metrics["execution_quality"]["markout_summary_by_horizon_ns"]["10"]
        for metrics, _ in results
    ]
    amounts = [summary["all"]["observed_markout_value"] for summary in summaries]
    assert amounts == [0.25, 0.25]

    total = 2**54 + 1
    expected_ticks = float(Fraction(1, total))
    expected_value = float(Fraction(1, 4 * total))
    for event_count, ((metrics, markouts), summary) in zip(
        (2, 3), zip(results, summaries, strict=True), strict=True
    ):
        assert [row["markout_ticks"] for row in markouts] == (
            [1.0, -1.0] if event_count == 2 else [1.0, 1.0, -1.0]
        )
        assert [row["markout_value"] for row in markouts] == (
            [0.25, -0.25] if event_count == 2 else [0.25, 0.25, -0.25]
        )
        for group in ("all", "bid"):
            record = summary[group]
            for field, expected in {
                "total_fill_quantity": total,
                "observed_quantity": total,
                "missing_quantity": 0,
                "total_fill_events": event_count,
                "observed_fill_events": event_count,
                "missing_fill_events": 0,
            }.items():
                assert type(record[field]) is int and record[field] == expected
            assert record["quantity_coverage"] == 1.0
            assert record["observed_markout_value"] == 0.25
            # Exact binary64 expected values: a loose absolute tolerance would
            # incorrectly accept a zero numerator for these small means.
            assert record["quantity_weighted_markout_ticks"] == expected_ticks
            assert record["quantity_weighted_markout_value"] == expected_value
        empty = summary["ask"]
        for field in (
            "total_fill_quantity",
            "observed_quantity",
            "missing_quantity",
            "total_fill_events",
            "observed_fill_events",
            "missing_fill_events",
        ):
            assert type(empty[field]) is int and empty[field] == 0
        for field in (
            "quantity_coverage",
            "quantity_weighted_markout_ticks",
            "quantity_weighted_markout_value",
            "observed_markout_value",
        ):
            assert empty[field] is None
        tick_mean = fmean(row["markout_ticks"] for row in markouts)
        value_mean = fmean(row["markout_value"] for row in markouts)
        assert metrics["execution_quality"]["markout_ticks_by_horizon_ns"] == {
            "10": tick_mean
        }
        assert metrics["execution_quality"]["markout_ticks_by_side"] == {
            "bid": tick_mean,
            "ask": None,
        }
        assert (
            metrics["attribution"]["legacy_pooled_markout_proxy"]
            == (value_mean or 0.0) * total
        )
        assert metrics["attribution"]["adverse_selection_markout"] is None
