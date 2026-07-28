"""Small shared value helpers."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

type PriceTicks = int
type Quantity = int
type TimestampNs = int


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
