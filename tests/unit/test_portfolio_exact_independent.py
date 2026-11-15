"""Black-box accounting checks with explicit, independent rational checkpoints.

The reference stores signed remaining acquisition notional. Realized P&L is
cash plus that pool, rather than a sum of rounded average-price differences.
No production accounting helper supplies an expected value.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, getcontext, localcontext
from fractions import Fraction
from typing import Any

import pytest

from lobmm.config import FeeConfig
from lobmm.enums import LiquidityRole, Side
from lobmm.portfolio import (
    AccountingError,
    AccountingFill,
    Portfolio,
    PortfolioSnapshot,
)


def _terminates(value: Fraction) -> bool:
    denominator = value.denominator
    for divisor in (2, 5):
        while denominator % divisor == 0:
            denominator //= divisor
    return denominator == 1


def _assert_view(actual: Decimal, expected: Fraction) -> None:
    assert isinstance(actual, Decimal)
    assert actual.is_finite()
    if _terminates(expected):
        # Finite decimal ledger values must not lose digits to ambient precision.
        assert Fraction(actual) == expected
    else:
        # Only presentation may round; this division uses the caller's context.
        rounded = Decimal(expected.numerator) / Decimal(expected.denominator)
        assert actual == rounded


def _assert_snapshot(
    snapshot: PortfolioSnapshot,
    *,
    inventory: int,
    cash_ticks: int,
    signed_pool: Fraction,
    mark: Fraction,
    tick: Fraction,
    fees: Fraction = Fraction(0),
    rebates: Fraction = Fraction(0),
    fill_count: int,
) -> None:
    realized = Fraction(cash_ticks) + signed_pool
    unrealized = inventory * mark - signed_pool
    gross = Fraction(cash_ticks) + inventory * mark
    assert snapshot.inventory == inventory
    assert snapshot.trade_cash_ticks == cash_ticks
    assert snapshot.fill_count == fill_count
    assert realized + unrealized == gross
    _assert_view(snapshot.mark_ticks, mark)
    _assert_view(snapshot.tick_value, tick)
    if inventory:
        assert snapshot.average_cost_ticks is not None
        _assert_view(snapshot.average_cost_ticks, signed_pool / inventory)
    else:
        assert snapshot.average_cost_ticks is None
    for actual, expected in (
        (snapshot.trade_cash, cash_ticks * tick),
        (snapshot.cash, cash_ticks * tick - fees + rebates),
        (snapshot.realized_pnl_ticks, realized),
        (snapshot.unrealized_pnl_ticks, unrealized),
        (snapshot.gross_pnl_ticks, gross),
        (snapshot.realized_pnl, realized * tick),
        (snapshot.unrealized_pnl, unrealized * tick),
        (snapshot.gross_pnl, gross * tick),
        (snapshot.fees, fees),
        (snapshot.rebates, rebates),
        (snapshot.net_pnl, gross * tick - fees + rebates),
        (snapshot.current_gross_exposure, abs(inventory) * mark * tick),
    ):
        _assert_view(actual, expected)


@pytest.mark.parametrize("mirror", (1, -1), ids=("long-first", "short-first"))
def test_explicit_rational_checkpoints_through_closes_and_reversals(
    mirror: int,
) -> None:
    # direction, size, price, resulting inventory, cash, signed remaining pool.
    # These constants are worked out independently of Portfolio's update rule.
    checkpoints = (
        (1, 2, 100, 2, -200, Fraction(200)),
        (1, 1, 101, 3, -301, Fraction(301)),
        (-1, 1, 103, 2, -198, Fraction(602, 3)),
        (-1, 4, 99, -2, 198, Fraction(-198)),
        (1, 1, 97, -1, 101, Fraction(-99)),
        (1, 2, 102, 1, -103, Fraction(102)),
        (-1, 1, 104, 0, 1, Fraction(0)),
    )
    ledger = Portfolio(tick_value="0.01", identity_tolerance=0)
    fees = rebates = Fraction(0)
    for index, (direction, size, price, inventory, cash, pool) in enumerate(
        checkpoints, 1
    ):
        fee = Decimal("0.001") * index
        rebate = Decimal("0.00007") * (index % 2)
        fees += Fraction(fee)
        rebates += Fraction(rebate)
        fill = AccountingFill(
            side=Side(direction * mirror),
            quantity=size,
            price_ticks=price,
            exchange_fill_timestamp_ns=index,
            liquidity_role=LiquidityRole.MAKER if index % 2 else LiquidityRole.TAKER,
            fee=fee,
            rebate=rebate,
        )
        result = ledger.apply_fill(fill, mark_ticks="100.125")
        _assert_snapshot(
            result,
            inventory=inventory * mirror,
            cash_ticks=cash * mirror,
            signed_pool=pool * mirror,
            mark=Fraction(801, 8),
            tick=Fraction(1, 100),
            fees=fees,
            rebates=rebates,
            fill_count=index,
        )
        assert result.timestamp_ns == index
        ledger.assert_invariants()


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
@pytest.mark.parametrize(
    ("price", "tolerance"), ((600_000, "1e-18"), (600_000_000, "1e-12"))
)
def test_valid_int64_scaled_fills_do_not_fail_on_repeating_average(
    side: Side, price: int, tolerance: str
) -> None:
    ledger = Portfolio(tick_value="0.01", identity_tolerance=tolerance)
    quantities = (100_000_000, 200_000_000)
    notional = quantities[0] * price + quantities[1] * (price + 1)
    assert max(*quantities, notional, price + 1) < 2**63
    ledger.record_fill(
        side=side, quantity=quantities[0], price_ticks=price, mark_ticks=price + 1
    )
    result = ledger.record_fill(
        side=side, quantity=quantities[1], price_ticks=price + 1, mark_ticks=price + 1
    )
    _assert_snapshot(
        result,
        inventory=int(side) * sum(quantities),
        cash_ticks=-int(side) * notional,
        signed_pool=Fraction(int(side) * notional),
        mark=Fraction(price + 1),
        tick=Fraction(1, 100),
        fill_count=2,
    )


@pytest.mark.parametrize("precision", (6, 28, 50))
@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_context_changes_affect_presentation_without_losing_finite_ledger_values(
    precision: int, side: Side
) -> None:
    original = getcontext().copy()
    ledger = Portfolio(tick_value="0.000000000123456789012345678901")
    ledger.record_fill(side=side, quantity=2, price_ticks=600_000_000, fee=0, rebate=0)
    with localcontext() as context:
        context.prec = precision
        settings = (context.prec, context.rounding, context.Emin, context.Emax)
        fee = Decimal("0.00000000000000000000000000007")
        result = ledger.record_fill(
            side=side,
            quantity=1,
            price_ticks=600_000_001,
            fee=fee,
            rebate=0,
            mark_ticks="600000001.12345678901234567890123456789",
        )
        _assert_snapshot(
            result,
            inventory=int(side) * 3,
            cash_ticks=-int(side) * 1_800_000_001,
            signed_pool=Fraction(int(side) * 1_800_000_001),
            mark=Fraction("600000001.12345678901234567890123456789"),
            tick=Fraction("0.000000000123456789012345678901"),
            fees=Fraction(fee),
            fill_count=2,
        )
        assert (context.prec, context.rounding, context.Emin, context.Emax) == settings
    assert getcontext().prec == original.prec
    # Re-rendering after the caller changes context must not corrupt the ledger.
    ledger.assert_invariants()
    _assert_snapshot(
        ledger.snapshot(mark_ticks=600_000_001),
        inventory=int(side) * 3,
        cash_ticks=-int(side) * 1_800_000_001,
        signed_pool=Fraction(int(side) * 1_800_000_001),
        mark=Fraction(600_000_001),
        tick=Fraction("0.000000000123456789012345678901"),
        fees=Fraction("0.00000000000000000000000000007"),
        fill_count=2,
    )


def test_configured_costs_and_explicit_costs_book_once_under_low_precision() -> None:
    fees = FeeConfig(
        maker_fee_per_unit=Decimal("0.000000123456789"),
        maker_rebate_per_unit=Decimal("0.000000000000007"),
        taker_fee_per_unit=Decimal("0.000000987654321"),
        proportional_fee_rate=Decimal("0.000123456789"),
    )
    ledger = Portfolio(tick_value="0.00001", fees=fees, identity_tolerance=0)
    with localcontext() as context:
        context.prec = 6
        ledger.record_fill(
            side=Side.BID,
            quantity=3,
            price_ticks=123_457,
            liquidity_role=LiquidityRole.MAKER,
        )
        result = ledger.apply_fill(
            AccountingFill(
                side=Side.ASK,
                quantity=1,
                price_ticks=123_459,
                exchange_fill_timestamp_ns=2,
                liquidity_role=LiquidityRole.TAKER,
                fee=Decimal("0.000000000000123456789"),
                rebate=Decimal("0.000000000000000000007"),
            ),
            mark_ticks="123458.25",
        )
        expected_fees = (
            3 * Fraction("0.000000123456789")
            + 3 * 123_457 * Fraction("0.00001") * Fraction("0.000123456789")
            + Fraction("0.000000000000123456789")
        )
        expected_rebates = 3 * Fraction("0.000000000000007") + Fraction(
            "0.000000000000000000007"
        )
        _assert_snapshot(
            result,
            inventory=2,
            cash_ticks=-246_912,
            signed_pool=Fraction(246_914),
            mark=Fraction(493_833, 4),
            tick=Fraction(1, 100_000),
            fees=expected_fees,
            rebates=expected_rebates,
            fill_count=2,
        )


@pytest.mark.parametrize(
    "invalid",
    (
        {"mark_ticks": 0},
        {"mark_ticks": -1},
        {"mark_ticks": "NaN"},
        {"mark_ticks": "Infinity"},
        {"timestamp_ns": -1},
        {"fee": -1, "rebate": 0},
        {"fee": "NaN", "rebate": 0},
        {"fee": "Infinity", "rebate": 0},
        {"fee": 0, "rebate": "NaN"},
        {"fee": 0, "rebate": -1},
    ),
)
def test_rejected_fill_leaves_the_existing_ledger_unchanged(
    invalid: dict[str, Any],
) -> None:
    ledger = Portfolio()
    ledger.record_fill(
        side=Side.BID, quantity=3, price_ticks=100, timestamp_ns=1, mark_ticks=101
    )
    before = asdict(ledger.snapshot())
    request: dict[str, Any] = dict(
        side=Side.ASK, quantity=1, price_ticks=103, timestamp_ns=2, mark_ticks=102
    )
    request.update(invalid)
    with pytest.raises(AccountingError):
        ledger.record_fill(**request)
    assert asdict(ledger.snapshot()) == before


@pytest.mark.parametrize("invalid_mark", ("NaN", "Infinity", -1))
def test_rejected_mark_leaves_snapshot_and_peak_unchanged(
    invalid_mark: str | int,
) -> None:
    ledger = Portfolio()
    ledger.record_fill(side=Side.ASK, quantity=3, price_ticks=100, timestamp_ns=1)
    before = asdict(ledger.snapshot())
    with pytest.raises(AccountingError):
        ledger.mark_to_market(invalid_mark, timestamp_ns=2)
    assert asdict(ledger.snapshot()) == before


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("trade_cash_ticks", -299),
        ("inventory", 4),
        ("average_cost_ticks", Decimal(101)),
        ("average_cost_ticks", Decimal("NaN")),
        ("realized_pnl_ticks", Decimal(1)),
        ("total_fees", Decimal(-1)),
        ("buy_volume", -1),
    ),
)
def test_corrupted_public_state_is_not_hidden_by_exact_reference(
    field: str, value: object
) -> None:
    ledger = Portfolio()
    ledger.record_fill(side=Side.BID, quantity=3, price_ticks=100)
    setattr(ledger, field, value)
    with pytest.raises(AccountingError):
        ledger.assert_invariants(100)
