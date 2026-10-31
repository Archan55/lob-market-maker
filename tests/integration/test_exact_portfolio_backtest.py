"""Frozen full-loop ledger checks for a repeating average at two scales.

The hand-authored tape and expected fills were fixed before execution. Ordinary
fixed-spread quoting supplies all orders; matching, risk, channels, accounting,
and artifact writing are unmodified. The oracle uses integer cash and rational
averages, without importing an exchange or portfolio implementation.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent

pytestmark = pytest.mark.integration


def _config(quantity: int) -> AppConfig:
    base = AppConfig()
    config = base.model_copy(
        update={
            "latency": base.latency.model_copy(
                update={
                    "market_data_ns": 0,
                    "order_entry_ns": 0,
                    "cancellation_ns": 0,
                    "fill_report_ns": 0,
                }
            ),
            "strategy": base.strategy.model_copy(
                update={
                    "order_size": quantity,
                    "minimum_quote_lifetime_ns": 0,
                    "refresh_interval_ns": 1_000,
                    "stale_after_ns": 1_000,
                }
            ),
            "risk": base.risk.model_copy(
                update={
                    "max_order_size": quantity,
                    "max_abs_inventory": 10 * quantity,
                    "max_total_open_quantity": 4 * quantity,
                    "max_loss": Decimal("1e30"),
                    "max_drawdown": Decimal("1e30"),
                }
            ),
            "fees": base.fees.model_copy(
                update={
                    "maker_fee_per_unit": Decimal("0.001"),
                    "maker_rebate_per_unit": Decimal("0.0002"),
                    "taker_fee_per_unit": Decimal("0.003"),
                    "proportional_fee_rate": Decimal("0.0001"),
                }
            ),
            "backtest": base.backtest.model_copy(
                update={"warmup_events": 0, "timer_interval_ns": 1_000}
            ),
            "output": base.output.model_copy(update={"write_plots": False}),
        }
    )
    # Validate the actual nested values, including the large quantity limits.
    return AppConfig.model_validate(config.model_dump())


def _tape(quantity: int, reflected: bool) -> list[MarketEvent]:
    # Before each trade an own quote already rests at the new bid, with the
    # subsequently added historical quantity behind it. Distinct quote prices
    # isolate the fills from previous counterfactual queue displacement.
    rows = (
        (0, EventType.SNAPSHOT, Side.BID, 99),
        (0, EventType.SNAPSHOT, Side.ASK, 103),
        (10, EventType.ADD, Side.BID, 100),
        (20, EventType.TRADE, Side.BID, 100),
        (30, EventType.CANCEL, Side.ASK, 103),
        (30, EventType.ADD, Side.ASK, 104),
        (30, EventType.ADD, Side.BID, 100),
        (40, EventType.ADD, Side.BID, 101),
        (50, EventType.TRADE, Side.BID, 101),
        (60, EventType.CANCEL, Side.ASK, 104),
        (60, EventType.ADD, Side.ASK, 106),
        (60, EventType.ADD, Side.BID, 102),
        (70, EventType.ADD, Side.BID, 103),
        (80, EventType.TRADE, Side.BID, 103),
        (90, EventType.ADD, Side.ASK, 107),
    )
    return [
        MarketEvent(
            timestamp,
            sequence,
            kind,
            side.opposite if reflected else side,
            204 - price if reflected else price,
            quantity,
        )
        for sequence, (timestamp, kind, side, price) in enumerate(rows)
    ]


@pytest.mark.parametrize("quantity", [1, 10**15], ids=["unit", "large-int64"])
@pytest.mark.parametrize("reflected", [False, True], ids=["long", "short"])
def test_full_backtest_preserves_cash_identity_with_repeating_average(
    tmp_path: Path, quantity: int, reflected: bool
) -> None:
    # Fixed before execution: long fills 100/101/103, short 104/103/101.
    # At the third fill average cost is exactly 304/3 or 308/3 ticks;
    # net open inventory is +/-3q and gross marked P&L is exactly 8q ticks.
    prices = (104, 103, 101) if reflected else (100, 101, 103)
    marks = (103, 102, 100) if reflected else (101, 102, 104)
    side = -1 if reflected else 1
    timestamps = (20, 50, 80)
    expected_fills = [
        (side, quantity, price, "maker", timestamp)
        for price, timestamp in zip(prices, timestamps, strict=True)
    ]

    result = run_backtest(
        _config(quantity),
        _tape(quantity, reflected),
        run_name=f"repeating-average-{side}-{quantity}",
        output_root=tmp_path,
    )
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert (
        fills.select(
            "side",
            "quantity",
            "price_ticks",
            "liquidity_role",
            "exchange_fill_timestamp_ns",
        ).rows()
        == expected_fills
    )
    assert fills["fill_id"].n_unique() == 3
    assert fills["order_id"].n_unique() == 3

    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    for count, (timestamp, mark) in enumerate(
        zip(timestamps, marks, strict=True), start=1
    ):
        prefix_prices = prices[:count]
        inventory = side * count * quantity
        cash_ticks = -side * quantity * sum(prefix_prices)
        gross_ticks = cash_ticks + inventory * mark
        fees = quantity * sum(
            (Fraction(1, 1_000) + Fraction(price, 1_000_000)) for price in prefix_prices
        )
        rebates = Fraction(count * quantity, 5_000)
        net = Fraction(gross_ticks, 100) - fees + rebates
        row = pnl.filter(pl.col("timestamp_ns") == timestamp).row(0, named=True)
        assert row["fill_count"] == count
        assert row["inventory"] == inventory
        assert row["trade_cash_ticks"] == cash_ticks
        assert row["mark_ticks"] == mark
        assert row["average_cost_ticks"] == float(Fraction(sum(prefix_prices), count))
        assert row["gross_pnl_ticks"] == float(gross_ticks)
        assert row["realized_pnl_ticks"] == 0.0
        assert row["unrealized_pnl_ticks"] == float(gross_ticks)
        assert row["fees"] == float(fees)
        assert row["rebates"] == float(rebates)
        assert row["net_pnl"] == float(net)

    assert result.diagnostics["end_inventory"] == side * 3 * quantity
    assert result.diagnostics["open_orders_end"] == 0
    assert result.metrics["pnl"]["net_pnl"] == float(net)
