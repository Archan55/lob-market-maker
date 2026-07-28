"""Post-run markouts, metrics, and research attribution."""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any

from lobmm.enums import LiquidityRole, Side

type Row = Mapping[str, Any]


def _finite_mean(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return fmean(clean) if clean else None


def compute_markouts(
    fills: Sequence[Row],
    market_states: Sequence[Row],
    horizons_ns: Sequence[int],
    *,
    tick_size: Decimal | float,
) -> list[dict[str, Any]]:
    """Calculate side-adjusted forward markouts using a true-state as-of rule.

    For each horizon the first true midpoint at or after
    ``fill_timestamp + horizon`` is used. Missing future observations remain
    missing rather than being extrapolated.
    """

    if any(horizon <= 0 for horizon in horizons_ns):
        raise ValueError("markout horizons must be positive")
    ordered_market = sorted(
        (
            (int(row["timestamp_ns"]), float(row["midpoint_ticks"]))
            for row in market_states
            if row.get("midpoint_ticks") is not None
        ),
        key=lambda item: item[0],
    )
    timestamps = [item[0] for item in ordered_market]
    tick = float(tick_size)
    output: list[dict[str, Any]] = []
    for fill in fills:
        side = Side(int(fill["side"]))
        fill_price = float(fill["price_ticks"])
        fill_timestamp = int(fill["exchange_fill_timestamp_ns"])
        for horizon in horizons_ns:
            target = fill_timestamp + int(horizon)
            index = bisect.bisect_left(timestamps, target)
            if index >= len(ordered_market):
                future_timestamp: int | None = None
                future_midpoint: float | None = None
                markout_ticks: float | None = None
                markout_bps: float | None = None
                markout_value: float | None = None
            else:
                future_timestamp, future_midpoint = ordered_market[index]
                if side is Side.BID:
                    markout_ticks = future_midpoint - fill_price
                else:
                    markout_ticks = fill_price - future_midpoint
                markout_value = markout_ticks * tick
                markout_bps = (
                    markout_ticks / fill_price * 10_000.0 if fill_price else None
                )
            output.append(
                {
                    "fill_id": fill["fill_id"],
                    "side": int(side),
                    "fill_timestamp_ns": fill_timestamp,
                    "fill_price_ticks": fill_price,
                    "horizon_ns": int(horizon),
                    "target_timestamp_ns": target,
                    "future_timestamp_ns": future_timestamp,
                    "future_midpoint_ticks": future_midpoint,
                    "markout_ticks": markout_ticks,
                    "markout_value": markout_value,
                    "markout_bps": markout_bps,
                    "asof_convention": "first_true_midpoint_at_or_after_horizon",
                }
            )
    return output


def classify_regimes(market_states: list[dict[str, Any]]) -> None:
    """Attach simple ex-post spread, volatility, and liquidity buckets in place."""

    if not market_states:
        return
    spreads = [
        float(row["spread_ticks"])
        for row in market_states
        if row.get("spread_ticks") is not None
    ]
    depths = [
        float(row.get("best_bid_quantity", 0)) + float(row.get("best_ask_quantity", 0))
        for row in market_states
    ]
    midpoint_changes: list[float] = []
    previous: float | None = None
    for row in market_states:
        current_raw = row.get("midpoint_ticks")
        current = float(current_raw) if current_raw is not None else None
        change = 0.0 if previous is None or current is None else abs(current - previous)
        midpoint_changes.append(change)
        if current is not None:
            previous = current
    spread_cut = _finite_mean(spreads) or 0.0
    depth_cut = _finite_mean(depths) or 0.0
    volatility_cut = _finite_mean(midpoint_changes) or 0.0
    for row, depth, change in zip(market_states, depths, midpoint_changes, strict=True):
        spread = row.get("spread_ticks")
        row["spread_regime"] = (
            "wide" if spread is not None and float(spread) > spread_cut else "tight"
        )
        row["liquidity_regime"] = "deep" if depth >= depth_cut else "thin"
        row["volatility_regime"] = "high" if change > volatility_cut else "low"


def build_metrics(
    *,
    orders: Sequence[Row],
    fills: Sequence[Row],
    inventory: Sequence[Row],
    pnl: Sequence[Row],
    market_states: Sequence[Row],
    markouts: Sequence[Row],
    risk_events: Sequence[Row],
    quote_messages: Sequence[Row],
    diagnostics: Mapping[str, Any],
    tick_size: Decimal | float,
    near_limit_fraction: float = 0.8,
) -> dict[str, Any]:
    """Build a JSON-safe metric document from completed audit rows."""

    if not 0 < near_limit_fraction <= 1:
        raise ValueError("near_limit_fraction must be in (0, 1]")
    submitted_orders = len(
        {
            str(row["order_id"])
            for row in orders
            if row.get("previous_status") in {None, ""}
        }
    )
    if submitted_orders == 0:
        submitted_orders = len({str(row["order_id"]) for row in orders})
    cancelled_orders = sum(1 for row in orders if row.get("new_status") == "cancelled")
    rejected_orders = sum(1 for row in orders if row.get("new_status") == "rejected")
    partial_fills = sum(
        1 for row in orders if row.get("new_status") == "partially_filled"
    )
    completed_fills = sum(1 for row in orders if row.get("new_status") == "filled")
    fill_count = len(fills)
    total_fill_quantity = sum(int(row["quantity"]) for row in fills)
    buy_volume = sum(
        int(row["quantity"]) for row in fills if int(row["side"]) == int(Side.BID)
    )
    sell_volume = sum(
        int(row["quantity"]) for row in fills if int(row["side"]) == int(Side.ASK)
    )
    maker_volume = sum(
        int(row["quantity"])
        for row in fills
        if row.get("liquidity_role") == LiquidityRole.MAKER.value
    )
    taker_volume = total_fill_quantity - maker_volume
    turnover = sum(
        float(row["quantity"]) * float(row["price_ticks"]) * float(tick_size)
        for row in fills
    )

    final_pnl = pnl[-1] if pnl else {}
    net_series = [float(row.get("net_pnl", 0.0)) for row in pnl]
    drawdowns: list[float] = []
    high = -math.inf
    for value in net_series:
        high = max(high, value)
        drawdowns.append(high - value)
    maximum_drawdown = max(drawdowns, default=0.0)

    inventory_values = [int(row.get("inventory", 0)) for row in inventory]
    maximum_limit = int(diagnostics.get("max_abs_inventory", 0) or 0)
    near_threshold = near_limit_fraction * maximum_limit
    near_fraction = (
        sum(abs(value) >= near_threshold for value in inventory_values)
        / len(inventory_values)
        if inventory_values and maximum_limit
        else 0.0
    )

    spreads = [
        float(row["spread_ticks"])
        for row in market_states
        if row.get("spread_ticks") is not None
    ]
    displayed_depth = [
        int(row.get("best_bid_quantity", 0)) + int(row.get("best_ask_quantity", 0))
        for row in market_states
    ]

    markout_by_horizon: dict[str, float | None] = {}
    for horizon in sorted({int(row["horizon_ns"]) for row in markouts}):
        markout_by_horizon[str(horizon)] = _finite_mean(
            float(row["markout_ticks"])
            for row in markouts
            if int(row["horizon_ns"]) == horizon
            and row.get("markout_ticks") is not None
        )
    markout_by_side: dict[str, float | None] = {}
    for side in Side:
        markout_by_side[side.name.lower()] = _finite_mean(
            float(row["markout_ticks"])
            for row in markouts
            if int(row["side"]) == int(side) and row.get("markout_ticks") is not None
        )

    fill_midpoints = _fill_midpoints(fills, market_states)
    signed_spreads: list[float] = []
    spread_capture_value = 0.0
    for fill, midpoint in zip(fills, fill_midpoints, strict=True):
        if midpoint is None:
            continue
        price = float(fill["price_ticks"])
        value = midpoint - price if int(fill["side"]) == 1 else price - midpoint
        signed_spreads.append(value)
        spread_capture_value += value * int(fill["quantity"]) * float(tick_size)

    fees = float(final_pnl.get("fees", 0.0))
    rebates = float(final_pnl.get("rebates", 0.0))
    gross_pnl = float(final_pnl.get("gross_pnl", 0.0))
    net_pnl = float(final_pnl.get("net_pnl", 0.0))
    realized_pnl = float(final_pnl.get("realized_pnl", 0.0))
    unrealized_pnl = float(final_pnl.get("unrealized_pnl", 0.0))
    attribution_residual = net_pnl - (spread_capture_value - fees + rebates)

    fill_rate = completed_fills / submitted_orders if submitted_orders else 0.0
    result: dict[str, Any] = {
        "research_disclaimer": (
            "Historical/synthetic simulation results are not evidence of "
            "future or real-world profitability."
        ),
        "trading_activity": {
            "submitted_orders": submitted_orders,
            "cancelled_orders": cancelled_orders,
            "rejected_orders": rejected_orders,
            "partial_fills": partial_fills,
            "completed_fills": completed_fills,
            "fill_events": fill_count,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "turnover": turnover,
            "maker_volume": maker_volume,
            "taker_volume": taker_volume,
            "order_to_fill_ratio": (
                submitted_orders / fill_count if fill_count else None
            ),
            "cancel_to_fill_ratio": (
                cancelled_orders / fill_count if fill_count else None
            ),
            "fill_rate": fill_rate,
            "quote_messages": len(quote_messages),
        },
        "pnl": {
            "gross_pnl": gross_pnl,
            "fees": fees,
            "rebates": rebates,
            "net_pnl": net_pnl,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "maximum_drawdown": maximum_drawdown,
            "pnl_per_traded_unit": (
                net_pnl / total_fill_quantity if total_fill_quantity else None
            ),
            "pnl_per_fill": net_pnl / fill_count if fill_count else None,
            "pnl_per_unit_turnover": net_pnl / turnover if turnover else None,
            "annualized_sharpe": None,
            "sharpe_note": (
                "Omitted: event-time demonstration samples do not support a "
                "defensible annualization."
            ),
        },
        "inventory": {
            "mean": _finite_mean(map(float, inventory_values)),
            "standard_deviation": (
                pstdev(inventory_values) if len(inventory_values) > 1 else 0.0
            ),
            "maximum_long": max(inventory_values, default=0),
            "maximum_short": min(inventory_values, default=0),
            "mean_absolute": _finite_mean(
                float(abs(value)) for value in inventory_values
            ),
            "time_near_limits_fraction": near_fraction,
            "end_of_session": inventory_values[-1] if inventory_values else 0,
        },
        "execution_quality": {
            "realized_spread_ticks": _finite_mean(signed_spreads),
            "effective_spread_ticks": (
                2.0 * (_finite_mean(signed_spreads) or 0.0) if signed_spreads else None
            ),
            "markout_ticks_by_horizon_ns": markout_by_horizon,
            "markout_ticks_by_side": markout_by_side,
        },
        "market_conditions": {
            "average_market_spread_ticks": _finite_mean(spreads),
            "average_displayed_top_depth": _finite_mean(map(float, displayed_depth)),
        },
        "risk": {
            "event_count": len(risk_events),
            "kill_switch_active": bool(diagnostics.get("kill_switch_active", False)),
        },
        "attribution": {
            "spread_capture": spread_capture_value,
            "inventory_mark_to_market": attribution_residual,
            "fees": -fees,
            "rebates": rebates,
            "adverse_selection_markout": (
                (
                    _finite_mean(
                        float(row["markout_value"])
                        for row in markouts
                        if row.get("markout_value") is not None
                    )
                    or 0.0
                )
                * total_fill_quantity
                if markouts
                else 0.0
            ),
            "reconciliation_residual": 0.0,
            "note": (
                "Attribution is an analytical approximation; inventory "
                "mark-to-market is the reconciliation residual."
            ),
        },
        "engineering": {
            "events_processed": int(diagnostics.get("events_processed", 0)),
            "scheduler_events_processed": int(
                diagnostics.get("scheduler_events_processed", 0)
            ),
            "wall_clock_seconds": float(diagnostics.get("wall_clock_seconds", 0.0)),
            "events_per_second": diagnostics.get("events_per_second"),
            "peak_memory_bytes": diagnostics.get("peak_memory_bytes"),
        },
    }
    return result


def _fill_midpoints(
    fills: Sequence[Row], market_states: Sequence[Row]
) -> list[float | None]:
    ordered = sorted(
        (
            (int(row["timestamp_ns"]), float(row["midpoint_ticks"]))
            for row in market_states
            if row.get("midpoint_ticks") is not None
        ),
        key=lambda item: item[0],
    )
    timestamps = [item[0] for item in ordered]
    output: list[float | None] = []
    for fill in fills:
        timestamp = int(fill["exchange_fill_timestamp_ns"])
        index = bisect.bisect_right(timestamps, timestamp) - 1
        output.append(ordered[index][1] if index >= 0 else None)
    return output


def aggregate_by(rows: Sequence[Row], *, key: str, value: str) -> dict[str, float]:
    """Mean numeric value by a stringable bucket, excluding missing values."""

    buckets: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(key) is None or row.get(value) is None:
            continue
        buckets[str(row[key])].append(float(row[value]))
    return {bucket: fmean(values) for bucket, values in sorted(buckets.items())}
