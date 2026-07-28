"""Internal parsing helpers shared by external market-data adapters."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent
from lobmm.types import price_to_ticks

type TableSource = pl.DataFrame | str | Path


class MarketDataAdapterError(ValueError):
    """Raised when vendor data cannot be normalized without guessing."""


def read_csv_source(
    source: TableSource,
    *,
    dataset_name: str,
    has_header: bool,
) -> pl.DataFrame:
    """Read a frame or CSV path while preserving numeric text exactly."""

    if isinstance(source, pl.DataFrame):
        return source.clone()

    path = Path(source)
    try:
        return pl.read_csv(
            path,
            has_header=has_header,
            infer_schema=False,
            null_values=["", "null", "NULL", "None"],
        )
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise MarketDataAdapterError(
            f"cannot read {dataset_name} CSV {path}: {exc}"
        ) from exc


def decimal_value(
    value: object,
    *,
    field: str,
    row_index: int,
) -> Decimal:
    """Parse a finite decimal without silently accepting binary artifacts."""

    if value is None:
        raise MarketDataAdapterError(f"{field} is null at row {row_index}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataAdapterError(
            f"{field}={value!r} is not numeric at row {row_index}"
        ) from exc
    if not parsed.is_finite():
        raise MarketDataAdapterError(
            f"{field}={value!r} must be finite at row {row_index}"
        )
    return parsed


def integer_value(
    value: object,
    *,
    field: str,
    row_index: int,
    nonnegative: bool = False,
) -> int:
    """Parse an exact integer and optionally require it to be nonnegative."""

    parsed = decimal_value(value, field=field, row_index=row_index)
    integral = parsed.to_integral_value()
    if parsed != integral:
        raise MarketDataAdapterError(
            f"{field}={value!r} is not an integer at row {row_index}"
        )
    result = int(integral)
    if nonnegative and result < 0:
        raise MarketDataAdapterError(
            f"{field}={result} must be nonnegative at row {row_index}"
        )
    return result


def scaled_quantity(
    value: object,
    *,
    multiplier: Decimal,
    field: str,
    row_index: int,
    allow_zero: bool,
) -> int:
    """Scale a source amount into exact integer simulator units."""

    parsed = decimal_value(value, field=field, row_index=row_index)
    if parsed < 0 or (not allow_zero and parsed == 0):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise MarketDataAdapterError(
            f"{field}={parsed} must be {qualifier} at row {row_index}"
        )
    scaled = parsed * multiplier
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise MarketDataAdapterError(
            f"{field}={parsed} cannot be represented exactly with "
            f"quantity_multiplier={multiplier} at row {row_index}"
        )
    result = int(integral)
    if result == 0 and parsed > 0:
        raise MarketDataAdapterError(
            f"{field}={parsed} rounds to zero after scaling at row {row_index}"
        )
    return result


def ticks_from_price(
    value: object,
    *,
    tick_size: Decimal,
    field: str,
    row_index: int,
) -> int:
    """Convert a source price to exact positive integer ticks."""

    parsed = decimal_value(value, field=field, row_index=row_index)
    if parsed <= 0:
        raise MarketDataAdapterError(
            f"{field}={parsed} must be positive at row {row_index}"
        )
    try:
        return price_to_ticks(parsed, tick_size)
    except ValueError as exc:
        raise MarketDataAdapterError(f"{field} at row {row_index}: {exc}") from exc


def require_positive_decimal(value: Decimal | str | int, *, field: str) -> Decimal:
    """Normalize a positive adapter configuration value."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be a finite positive decimal")
    return parsed


def assert_uncrossed(
    bids: dict[int, int],
    asks: dict[int, int],
    *,
    context: str,
) -> None:
    """Reject crossed source snapshots before they reach replay."""

    if bids and asks and max(bids) >= min(asks):
        raise MarketDataAdapterError(
            f"{context} is crossed: best_bid={max(bids)}, best_ask={min(asks)}"
        )


def materialize_events(
    specs: list[tuple[int, EventType, Side | None, int, int]],
) -> pl.DataFrame:
    """Assign deterministic sequence numbers and build a canonical frame."""

    previous_timestamp: int | None = None
    events: list[MarketEvent] = []
    for sequence_number, (
        timestamp_ns,
        event_type,
        side,
        price_ticks,
        quantity,
    ) in enumerate(specs):
        if previous_timestamp is not None and timestamp_ns < previous_timestamp:
            raise MarketDataAdapterError(
                "normalized timestamp regression: "
                f"{timestamp_ns} follows {previous_timestamp}"
            )
        events.append(
            MarketEvent(
                timestamp_ns=timestamp_ns,
                sequence_number=sequence_number,
                event_type=event_type,
                side=side,
                price_ticks=price_ticks,
                quantity=quantity,
            )
        )
        previous_timestamp = timestamp_ns

    from lobmm.data.schema import events_to_frame

    return events_to_frame(events)
