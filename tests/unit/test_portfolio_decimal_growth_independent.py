"""Fixed small-fill growth witness and independent Decimal-rendering checks.

After k buy-one/sell-one cycles from three units acquired at 100, the signed
cost pool is 303 - 3*(3/4)**k and the average is 101 - (3/4)**k. These closed
forms supply the reference without reproducing Portfolio's update algorithm.
"""

from __future__ import annotations

import sys
from decimal import Decimal, getcontext
from fractions import Fraction

import pytest

from lobmm.enums import Side
from lobmm.portfolio import Portfolio, _fraction_decimal


def test_public_small_fill_cycles_survive_terminating_decimal_growth() -> None:
    guard = sys.get_int_max_str_digits()
    assert guard == 4300
    context_before = repr(getcontext())
    ledger = Portfolio(tick_value="0.01", identity_tolerance="1e-12")
    ledger.record_fill(
        side=Side.BID,
        quantity=3,
        price_ticks=100,
        timestamp_ns=1,
        fee=0,
        rebate=0,
        mark_ticks=100,
    )
    result = ledger.snapshot()
    for cycle in range(1, 2305):
        ledger.record_fill(
            side=Side.BID,
            quantity=1,
            price_ticks=101,
            timestamp_ns=2 * cycle,
            fee=0,
            rebate=0,
            mark_ticks=100,
        )
        result = ledger.record_fill(
            side=Side.ASK,
            quantity=1,
            price_ticks=100,
            timestamp_ns=2 * cycle + 1,
            fee=0,
            rebate=0,
            mark_ticks=100,
        )
        assert result.inventory == 3
        assert result.trade_cash_ticks == -300 - cycle
        assert result.fill_count == 1 + 2 * cycle
        assert Fraction(result.gross_pnl_ticks) == -cycle

    average = Fraction(101) - Fraction(3, 4) ** 2304
    pool = 3 * average
    assert result.average_cost_ticks is not None
    assert Fraction(result.average_cost_ticks) == average
    assert Fraction(result.realized_pnl_ticks) == Fraction(-2604) + pool
    assert Fraction(result.unrealized_pnl_ticks) == 300 - pool
    assert Fraction(result.net_pnl) == Fraction(-2304, 100)
    assert result.timestamp_ns == 4609
    assert result.buy_volume == 2307
    assert result.sell_volume == 2304
    ledger.assert_invariants()
    assert sys.get_int_max_str_digits() == guard
    assert repr(getcontext()) == context_before


@pytest.mark.parametrize("sign", (1, -1))
def test_reachable_4301_digit_decimal_renders_exactly_without_guard_override(
    sign: int,
) -> None:
    guard = sys.get_int_max_str_digits()
    assert guard == 4300
    context_before = repr(getcontext())
    value = sign * (Fraction(101) - Fraction(3, 4) ** 2149)
    rendered = _fraction_decimal(value)
    assert isinstance(rendered, Decimal)
    assert rendered.is_finite()
    assert Fraction(rendered) == value
    assert len(rendered.as_tuple().digits) == 4301
    assert rendered.as_tuple().exponent == -4298
    assert sys.get_int_max_str_digits() == guard
    assert repr(getcontext()) == context_before


@pytest.mark.parametrize(
    ("twos", "fives"), ((257, 123), (123, 257), (0, 1024), (1024, 0))
)
@pytest.mark.parametrize("sign", (1, -1))
def test_asymmetric_terminating_denominators_preserve_exact_value(
    twos: int, fives: int, sign: int
) -> None:
    context_before = repr(getcontext())
    value = Fraction(sign * 13, 2**twos * 5**fives)
    rendered = _fraction_decimal(value)
    assert rendered.is_finite()
    assert Fraction(rendered) == value
    assert repr(getcontext()) == context_before


@pytest.mark.parametrize("sign", (1, -1))
def test_other_prime_after_two_and_five_factors_stays_a_rounded_view(sign: int) -> None:
    value = Fraction(sign * 13, 2**17 * 5**23 * 3)
    # Compute the independently specified presentation before checking that the
    # renderer itself preserves the caller's already-established context.
    expected = Decimal(value.numerator) / Decimal(value.denominator)
    context_before = repr(getcontext())
    rendered = _fraction_decimal(value)
    assert rendered == expected
    assert Fraction(rendered) != value
    assert repr(getcontext()) == context_before


@pytest.mark.parametrize("value", (Fraction(0), Fraction(12345), Fraction(-12345)))
def test_integer_and_zero_rendering_do_not_require_denominator_factors(
    value: Fraction,
) -> None:
    context_before = repr(getcontext())
    assert Fraction(_fraction_decimal(value)) == value
    assert repr(getcontext()) == context_before
