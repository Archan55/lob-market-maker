"""Full-replay regressions with independent cash and liquidation-side marks.

Matching, risk, strategy decisions and channel delivery use production code.
The oracles below use only the hand-specified fills and terminal visible prices;
they do not invoke Portfolio, Exchange, QueueModel or the marking selector.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig
from lobmm.enums import EventType, MarkPrice, SessionEndPolicy, Side
from lobmm.events import MarketEvent

pytestmark = pytest.mark.integration


def _config(*, quantity: int, policy: SessionEndPolicy) -> AppConfig:
    base = AppConfig()
    return base.model_copy(
        update={
            "latency": base.latency.model_copy(
                update={
                    "market_data_ns": 0,
                    "order_entry_ns": 1,
                    "cancellation_ns": 100,
                    "fill_report_ns": 20,
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
            "fees": base.fees.model_copy(
                update={
                    "maker_fee_per_unit": Decimal("0.001"),
                    "maker_rebate_per_unit": Decimal("0.0002"),
                    "taker_fee_per_unit": Decimal("0.003"),
                    "proportional_fee_rate": Decimal("0.0001"),
                }
            ),
            "backtest": base.backtest.model_copy(
                update={
                    "warmup_events": 0,
                    "timer_interval_ns": 1_000,
                    "mark_price": MarkPrice.CONSERVATIVE,
                    "session_end_policy": policy,
                }
            ),
            "output": base.output.model_copy(update={"write_plots": False}),
        }
    )


def _events(
    rows: list[tuple[int, str, int, int, int]], *, reflected: bool = False
) -> list[MarketEvent]:
    return [
        MarketEvent(
            timestamp,
            sequence,
            EventType(kind),
            Side(-side if reflected else side),
            202 - price if reflected else price,
            quantity,
        )
        for sequence, (timestamp, kind, side, price, quantity) in enumerate(rows)
    ]


def _assert_cash_identity(
    row: dict[str, object],
    *,
    signed_fills: tuple[tuple[int, int, int, str], ...],
    mark: int,
    realized_ticks: int,
) -> None:
    """Reconcile without the average-cost implementation or fee helper."""
    inventory = sum(side * quantity for side, quantity, _, _ in signed_fills)
    cash_ticks = -sum(
        side * quantity * price for side, quantity, price, _ in signed_fills
    )
    gross_ticks = cash_ticks + inventory * mark
    fees = sum(
        Decimal(quantity) * (Decimal("0.001") if role == "maker" else Decimal("0.003"))
        + Decimal(quantity * price) * Decimal("0.01") * Decimal("0.0001")
        for _, quantity, price, role in signed_fills
    )
    rebates = sum(
        Decimal(quantity) * Decimal("0.0002")
        for _, quantity, _, role in signed_fills
        if role == "maker"
    )
    assert row["inventory"] == inventory
    assert row["mark_ticks"] == mark
    assert row["trade_cash_ticks"] == cash_ticks
    assert row["gross_pnl_ticks"] == gross_ticks
    assert row["realized_pnl_ticks"] == realized_ticks
    assert row["unrealized_pnl_ticks"] == gross_ticks - realized_ticks
    assert row["fees"] == pytest.approx(float(fees), abs=1e-12)
    assert row["rebates"] == pytest.approx(float(rebates), abs=1e-12)
    assert row["cash"] == pytest.approx(
        float(Decimal(cash_ticks) * Decimal("0.01") - fees + rebates), abs=1e-12
    )
    assert row["net_pnl"] == pytest.approx(
        float(Decimal(gross_ticks) * Decimal("0.01") - fees + rebates), abs=1e-12
    )


@pytest.mark.parametrize("side", [Side.BID, Side.ASK])
def test_taker_open_uses_post_fill_conservative_inventory_side(
    tmp_path: Path, side: Side
) -> None:
    # The strategy observes 100/104 at t=3 and sends 101/103. The true ask
    # moves to 101 before t=8 entry (or the reflected bid moves to 103).
    # The resulting taker fill must use the newly opened inventory's side.
    config = _config(quantity=2, policy=SessionEndPolicy.MARK)
    config = config.model_copy(
        update={
            "latency": config.latency.model_copy(
                update={
                    "market_data_ns": 3,
                    "order_entry_ns": 5,
                    "cancellation_ns": 5,
                    "fill_report_ns": 1,
                }
            ),
            "strategy": config.strategy.model_copy(update={"post_only": False}),
            # Correct conservative net loss breaches 0.02 at the fill. The
            # old midpoint snapshot incorrectly postponed the kill to t=20.
            "risk": config.risk.model_copy(update={"max_loss": Decimal("0.02")}),
        }
    )
    reflected = side is Side.ASK
    # Reflection about 102 ticks preserves the 100/104 initial book.
    rows = [
        (0, "SNAPSHOT", 1, 100, 10),
        (0, "SNAPSHOT", -1, 104, 10),
        (4, "CANCEL", -1, 104, 10),
        (4, "ADD", -1, 101, 10),
        (20, "ADD", 1, 99, 1),
    ]
    events = _events(rows)
    if reflected:
        events = [
            MarketEvent(
                event.timestamp_ns,
                event.sequence_number,
                event.event_type,
                event.side.opposite if event.side is not None else None,
                204 - event.price_ticks,
                event.quantity,
            )
            for event in events
        ]
    result = run_backtest(
        config, events, run_name=f"taker-open-{side.name}", output_root=tmp_path
    )
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills.select("side", "quantity", "price_ticks").rows() == [
        (int(side), 2, 101 if side is Side.BID else 103)
    ]
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    fill_row = pnl.filter(pl.col("timestamp_ns") == 8).row(0, named=True)
    _assert_cash_identity(
        fill_row,
        signed_fills=((int(side), 2, 101 if side is Side.BID else 103, "taker"),),
        mark=100 if side is Side.BID else 104,
        realized_ticks=0,
    )
    risk_events = pl.read_parquet(result.run_directory / "risk_events.parquet")
    assert risk_events.filter(pl.col("reason") == "max_loss")[
        "timestamp_ns"
    ].to_list() == [8]


@pytest.mark.parametrize("reflected", [False, True])
def test_maker_flip_uses_post_fill_conservative_inventory_side(
    tmp_path: Path, reflected: bool
) -> None:
    # Both own quotes arrive inside 98/104 with no queue ahead. Later adds
    # join behind them. One buy then two sells flip +1 to -1 (and vice versa).
    events = _events(
        [
            (0, "SNAPSHOT", 1, 98, 20),
            (0, "SNAPSHOT", -1, 104, 20),
            (2, "ADD", 1, 100, 10),
            (3, "TRADE", 1, 100, 1),
            (4, "ADD", -1, 102, 10),
            (5, "TRADE", -1, 102, 2),
            (10, "ADD", 1, 97, 1),
        ],
        reflected=reflected,
    )
    result = run_backtest(
        _config(quantity=2, policy=SessionEndPolicy.MARK),
        events,
        run_name=f"maker-flip-{reflected}",
        output_root=tmp_path,
    )
    sign = -1 if reflected else 1
    first_price, second_price = (102, 100) if reflected else (100, 102)
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills.select("side", "quantity", "price_ticks").rows() == [
        (sign, 1, first_price),
        (-sign, 2, second_price),
    ]
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    # Inspect the immediate fill row, before the subsequent market re-mark.
    flip = pnl.filter(pl.col("timestamp_ns") == 5).row(0, named=True)
    _assert_cash_identity(
        flip,
        signed_fills=(
            (sign, 1, first_price, "maker"),
            (-sign, 2, second_price, "maker"),
        ),
        mark=100 if reflected else 102,
        realized_ticks=2,
    )


@pytest.mark.parametrize("reflected", [False, True])
@pytest.mark.parametrize("one_sided", [False, True])
def test_partial_session_liquidation_marks_remaining_liquidation_side_depth(
    tmp_path: Path, reflected: bool, one_sided: bool
) -> None:
    rows = [
        (0, "SNAPSHOT", 1, 98, 20),
        (0, "SNAPSHOT", -1, 104, 20),
        (2, "ADD", 1, 100, 20),
        (3, "TRADE", 1, 100, 5),
        (4, "CANCEL", 1, 100, 13),
        (10, "CANCEL", -1, 104, 20) if one_sided else (10, "ADD", -1, 105, 1),
    ]
    result = run_backtest(
        _config(quantity=5, policy=SessionEndPolicy.LIQUIDATE),
        _events(rows, reflected=reflected),
        run_name=f"partial-liquidation-{reflected}-{one_sided}",
        output_root=tmp_path,
    )
    sign = -1 if reflected else 1
    entry_price = 102 if reflected else 100
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills.select("side", "quantity", "price_ticks", "strategy_id").rows() == [
        (sign, 5, entry_price, "market-maker"),
        (-sign, 2, entry_price, "session-policy"),
    ]
    assert fills["exchange_fill_timestamp_ns"].to_list() == [3, 11]
    assert result.diagnostics["open_orders_end"] == 0
    assert result.diagnostics["end_inventory"] == sign * 3
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    terminal = pnl.row(-1, named=True)
    assert terminal["timestamp_ns"] == 11
    _assert_cash_identity(
        terminal,
        signed_fills=(
            (sign, 5, entry_price, "maker"),
            (-sign, 2, entry_price, "taker"),
        ),
        mark=104 if reflected else 98,
        realized_ticks=0,
    )
    assert result.metrics["pnl"]["net_pnl"] == terminal["net_pnl"]
    assert result.metrics["inventory"]["end_of_session"] == sign * 3


@pytest.mark.parametrize("reflected", [False, True])
def test_one_sided_terminal_market_marks_available_liquidation_side(
    tmp_path: Path, reflected: bool
) -> None:
    # After opposite-side removal, withdrawing the current liquidation-side
    # best quote exposes a worse executable mark even without another fill.
    result = run_backtest(
        _config(quantity=5, policy=SessionEndPolicy.MARK),
        _events(
            [
                (0, "SNAPSHOT", 1, 98, 20),
                (0, "SNAPSHOT", -1, 104, 20),
                (2, "ADD", 1, 100, 20),
                (3, "TRADE", 1, 100, 5),
                (10, "CANCEL", -1, 104, 20),
                (10, "CANCEL", 1, 100, 15),
            ],
            reflected=reflected,
        ),
        run_name=f"one-sided-terminal-{reflected}",
        output_root=tmp_path,
    )
    sign = -1 if reflected else 1
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    terminal = pnl.row(-1, named=True)
    assert terminal["timestamp_ns"] == 10
    _assert_cash_identity(
        terminal,
        signed_fills=((sign, 5, 102 if reflected else 100, "maker"),),
        mark=104 if reflected else 98,
        realized_ticks=0,
    )
