"""Canonical Polars schema and conversions for Level 2 market events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import polars as pl

from lobmm.enums import EventType, Side
from lobmm.events import EventValidationError, MarketEvent

TIMESTAMP_COLUMN = "timestamp_ns"
SEQUENCE_COLUMN = "sequence_number"
EVENT_TYPE_COLUMN = "event_type"
SIDE_COLUMN = "side"
PRICE_COLUMN = "price_ticks"
QUANTITY_COLUMN = "quantity"

CANONICAL_COLUMNS: tuple[str, ...] = (
    TIMESTAMP_COLUMN,
    SEQUENCE_COLUMN,
    EVENT_TYPE_COLUMN,
    SIDE_COLUMN,
    PRICE_COLUMN,
    QUANTITY_COLUMN,
)

# ``side`` is nullable because RESET has no resting-book side. Event types are
# stored as strings so Parquet files remain self-describing outside Python.
CANONICAL_SCHEMA: dict[str, type[pl.DataType]] = {
    TIMESTAMP_COLUMN: pl.Int64,
    SEQUENCE_COLUMN: pl.Int64,
    EVENT_TYPE_COLUMN: pl.String,
    SIDE_COLUMN: pl.Int8,
    PRICE_COLUMN: pl.Int64,
    QUANTITY_COLUMN: pl.Int64,
}


class DataSchemaError(ValueError):
    """Raised when tabular data cannot satisfy the canonical event schema."""


def empty_event_frame() -> pl.DataFrame:
    """Return an empty frame with the exact canonical schema."""

    return pl.DataFrame(schema=CANONICAL_SCHEMA)


def events_to_frame(events: Iterable[MarketEvent]) -> pl.DataFrame:
    """Materialize typed events as a canonical Polars frame."""

    rows = [event.as_dict() for event in events]
    if not rows:
        return empty_event_frame()
    try:
        return pl.DataFrame(rows).select(
            pl.col(column).cast(dtype, strict=True).alias(column)
            for column, dtype in CANONICAL_SCHEMA.items()
        )
    except (pl.exceptions.PolarsError, TypeError, ValueError) as exc:
        raise DataSchemaError(f"cannot encode canonical market events: {exc}") from exc


def coerce_canonical_frame(
    frame: pl.DataFrame,
    *,
    allow_extra_columns: bool = False,
) -> pl.DataFrame:
    """Select and strictly cast canonical columns.

    Casting is intentionally strict: lossy casts (for example, a fractional
    quantity) and unparseable enum encodings fail at the ingestion boundary.
    Semantic checks are performed by :func:`frame_to_events` and the stream
    validator.
    """

    columns = set(frame.columns)
    expected = set(CANONICAL_COLUMNS)
    missing = expected - columns
    extra = columns - expected
    if missing:
        raise DataSchemaError(
            "missing canonical column(s): " + ", ".join(sorted(missing))
        )
    if extra and not allow_extra_columns:
        raise DataSchemaError("unexpected column(s): " + ", ".join(sorted(extra)))
    try:
        return frame.select(
            pl.col(column).cast(dtype, strict=True).alias(column)
            for column, dtype in CANONICAL_SCHEMA.items()
        )
    except (pl.exceptions.PolarsError, TypeError, ValueError) as exc:
        raise DataSchemaError(f"cannot cast canonical event columns: {exc}") from exc


def event_from_mapping(
    row: Mapping[str, Any],
    *,
    row_index: int | None = None,
) -> MarketEvent:
    """Parse one canonical row into a validated :class:`MarketEvent`."""

    location = f"row {row_index}" if row_index is not None else "event row"
    try:
        raw_event_type = row[EVENT_TYPE_COLUMN]
        event_type = (
            raw_event_type
            if isinstance(raw_event_type, EventType)
            else EventType(str(raw_event_type))
        )
        raw_side = row[SIDE_COLUMN]
        side = (
            None
            if raw_side is None
            else raw_side
            if isinstance(raw_side, Side)
            else Side(int(raw_side))
        )
        return MarketEvent(
            timestamp_ns=int(row[TIMESTAMP_COLUMN]),
            sequence_number=int(row[SEQUENCE_COLUMN]),
            event_type=event_type,
            side=side,
            price_ticks=int(row[PRICE_COLUMN]),
            quantity=int(row[QUANTITY_COLUMN]),
        )
    except KeyError as exc:
        raise DataSchemaError(f"{location}: missing field {exc.args[0]!r}") from exc
    except (EventValidationError, TypeError, ValueError) as exc:
        raise DataSchemaError(f"{location}: invalid canonical event: {exc}") from exc


def frame_to_events(
    frame: pl.DataFrame,
    *,
    allow_extra_columns: bool = False,
) -> list[MarketEvent]:
    """Convert a canonical Polars frame to validated typed events."""

    canonical = coerce_canonical_frame(frame, allow_extra_columns=allow_extra_columns)
    return [
        event_from_mapping(row, row_index=index)
        for index, row in enumerate(canonical.iter_rows(named=True))
    ]


# Friendly aliases used at reporting and integration boundaries.
to_frame = events_to_frame
to_events = frame_to_events
