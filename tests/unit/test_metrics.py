from __future__ import annotations

from decimal import Decimal

from lobmm.metrics import aggregate_by, build_metrics, compute_markouts


def fills() -> list[dict[str, object]]:
    return [
        {
            "fill_id": "B",
            "side": 1,
            "quantity": 2,
            "price_ticks": 100,
            "exchange_fill_timestamp_ns": 0,
            "liquidity_role": "maker",
        },
        {
            "fill_id": "S",
            "side": -1,
            "quantity": 1,
            "price_ticks": 102,
            "exchange_fill_timestamp_ns": 0,
            "liquidity_role": "taker",
        },
    ]


def test_markout_side_sign_and_asof_convention() -> None:
    market = [
        {"timestamp_ns": 100, "midpoint_ticks": 101.0},
        {"timestamp_ns": 110, "midpoint_ticks": 103.0},
    ]
    rows = compute_markouts(fills(), market, [105], tick_size=Decimal("0.01"))
    buy, sell = rows
    assert buy["future_timestamp_ns"] == 110
    assert buy["markout_ticks"] == 3
    assert sell["markout_ticks"] == -1
    assert buy["markout_value"] == 0.03


def test_markout_is_missing_without_future_midpoint() -> None:
    rows = compute_markouts(
        fills()[:1],
        [{"timestamp_ns": 1, "midpoint_ticks": 101.0}],
        [10],
        tick_size=0.01,
    )
    assert rows[0]["future_timestamp_ns"] is None
    assert rows[0]["markout_ticks"] is None


def test_build_metrics_reconciles_cost_signs() -> None:
    orders = [
        {
            "order_id": "O1",
            "previous_status": None,
            "new_status": "filled",
        }
    ]
    pnl = [
        {
            "gross_pnl": 1.0,
            "net_pnl": 0.9,
            "fees": 0.2,
            "rebates": 0.1,
            "realized_pnl": 0.5,
            "unrealized_pnl": 0.5,
        }
    ]
    metrics = build_metrics(
        orders=orders,
        fills=fills()[:1],
        inventory=[{"inventory": 2}],
        pnl=pnl,
        market_states=[
            {
                "timestamp_ns": 0,
                "midpoint_ticks": 101,
                "spread_ticks": 2,
                "best_bid_quantity": 10,
                "best_ask_quantity": 10,
            }
        ],
        markouts=[],
        risk_events=[],
        quote_messages=[],
        diagnostics={"events_processed": 1, "max_abs_inventory": 10},
        tick_size=Decimal("0.01"),
    )
    assert metrics["pnl"]["net_pnl"] == 0.9
    assert metrics["pnl"]["fees"] == 0.2
    assert metrics["pnl"]["rebates"] == 0.1
    assert metrics["trading_activity"]["maker_volume"] == 2


def test_near_limit_metric_uses_configured_fraction() -> None:
    common = {
        "orders": [],
        "fills": [],
        "inventory": [{"inventory": 6}, {"inventory": 9}],
        "pnl": [],
        "market_states": [],
        "markouts": [],
        "risk_events": [],
        "quote_messages": [],
        "diagnostics": {"max_abs_inventory": 10},
        "tick_size": Decimal("0.01"),
    }
    half = build_metrics(**common, near_limit_fraction=0.5)
    high = build_metrics(**common, near_limit_fraction=0.8)

    assert half["inventory"]["time_near_limits_fraction"] == 1.0
    assert high["inventory"]["time_near_limits_fraction"] == 0.5


def test_aggregate_by_excludes_missing_values() -> None:
    assert aggregate_by(
        [
            {"bucket": "a", "value": 1},
            {"bucket": "a", "value": 3},
            {"bucket": "b", "value": None},
        ],
        key="bucket",
        value="value",
    ) == {"a": 2.0}
