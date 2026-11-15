"""Small shared value helpers."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from math import gcd

type PriceTicks = int
type Quantity = int
type TimestampNs = int


def _decimal_components(value: Decimal) -> tuple[int, int]:
    """Extract a finite signed coefficient without context math or text-to-int."""

    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("decimal value must be finite")
    return int(Decimal((sign, digits, 0))), exponent


def _exact_scaled_integer(
    numerator: int, denominator: int, exponent: int
) -> int | None:
    """Return numerator/denominator * 10**exponent iff it is an integer.

    Callers establish a positive denominator and valid units first. Cancel
    compact decimal exponents before allocating powers. Negative powers are
    bounded by coefficient size; unbounded positive powers are actual outputs.
    """

    if numerator == 0:
        return 0
    common = gcd(numerator, denominator)
    numerator //= common
    denominator //= common
    if exponent >= 0:
        twos = (denominator & -denominator).bit_length() - 1
        denominator >>= twos
        fives = 0
        while denominator % 5 == 0:
            denominator //= 5
            fives += 1
        if denominator != 1 or twos > exponent or fives > exponent:
            return None
        five_power: int = 5 ** (exponent - fives)
        return (numerator << (exponent - twos)) * five_power
    if denominator != 1:
        return None
    places = -exponent
    absolute = abs(numerator)
    # Divisibility by 10**places requires at least this many binary zeros.
    # If it is possible, allocating that power is bounded by coefficient size.
    if places > (absolute & -absolute).bit_length() - 1:
        return None
    result, remainder = divmod(numerator, 10**places)
    return result if remainder == 0 else None


def price_to_ticks(
    price: Decimal | float | str,
    tick_size: Decimal | float | str,
    *,
    strict: bool = True,
) -> int:
    """Convert a display price to integer ticks.

    Strict conversion rejects prices that are not exactly on tick. Non-strict
    conversion rounds to the nearest tick using ties-to-even.
    """

    value = Decimal(str(price))
    tick = Decimal(str(tick_size))
    if bool(strict):
        if not value.is_finite():
            raise ValueError("price must be finite")
        if not tick.is_finite() or tick <= 0:
            raise ValueError("tick_size must be positive")
        numerator, value_exponent = _decimal_components(value)
        denominator, tick_exponent = _decimal_components(tick)
        result = _exact_scaled_integer(
            numerator, denominator, value_exponent - tick_exponent
        )
        if result is None:
            raise ValueError(f"price {value} is not aligned to tick size {tick}")
        return result
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    raw = value / tick
    integral = raw.to_integral_value(rounding=ROUND_HALF_EVEN)
    if strict and raw != integral:
        raise ValueError(f"price {value} is not aligned to tick size {tick}")
    return int(integral)


def floor_price_to_ticks(
    price: Decimal | float | str, tick_size: Decimal | float | str
) -> int:
    """Round a price down to the nearest valid integer tick."""

    value = Decimal(str(price))
    tick = Decimal(str(tick_size))
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    return int((value / tick).to_integral_value(rounding=ROUND_FLOOR))


def ticks_to_price(price_ticks: int, tick_size: Decimal | float | str) -> Decimal:
    tick = Decimal(str(tick_size))
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    return Decimal(price_ticks) * tick
