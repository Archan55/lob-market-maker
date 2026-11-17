"""CSV and Parquet I/O for canonical market events."""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl

from lobmm.data.schema import (
    _INTEGER_BOUNDS,
    CANONICAL_SCHEMA,
    DataSchemaError,
    _exact_integer,
    coerce_canonical_frame,
    events_to_frame,
    frame_to_events,
)
from lobmm.events import MarketEvent


class DataLoadError(ValueError):
    """Raised when a market-data file cannot be read or written safely."""


_DECIMAL_TOKEN = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)


def _csv_integer_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """Parse original integer-field text without a floating-point intermediate."""

    columns = []
    for column, bounds in _INTEGER_BOUNDS.items():
        if column not in frame.columns:
            continue
        series = frame[column]
        target = CANONICAL_SCHEMA[column]
        try:
            # Ordinary integer tokens stay on the native strict-cast path.
            parsed = series.cast(target, strict=True)
        except pl.exceptions.PolarsError:
            values: list[int | None] = []
            for token in series:
                if token is None:
                    values.append(None)
                    continue
                if _DECIMAL_TOKEN.fullmatch(token) is None:
                    raise DataSchemaError(
                        f"{column}: invalid integer numeric text"
                    ) from None
                try:
                    value = Decimal(token)
                except InvalidOperation as exc:
                    raise DataSchemaError(
                        f"{column}: invalid integer numeric text"
                    ) from exc
                # Bounds precede int(), so compact huge exponents do not cause
                # enormous integer allocation. Equality checks the exact text.
                values.append(_exact_integer(value, field=column, bounds=bounds))
            parsed = pl.Series(column, values, dtype=target)
        columns.append(parsed)
    return frame.with_columns(columns)


def read_csv(
    path: str | Path,
    *,
    allow_extra_columns: bool = False,
) -> pl.DataFrame:
    """Read and strictly normalize a canonical CSV file."""

    input_path = Path(path)
    try:
        frame = pl.read_csv(
            input_path,
            infer_schema=False,
            null_values=["", "null", "NULL", "None"],
        )
        frame = _csv_integer_columns(frame)
        return coerce_canonical_frame(frame, allow_extra_columns=allow_extra_columns)
    except (OSError, pl.exceptions.PolarsError, DataSchemaError) as exc:
        raise DataLoadError(f"cannot load canonical CSV {input_path}: {exc}") from exc


def read_parquet(
    path: str | Path,
    *,
    allow_extra_columns: bool = False,
) -> pl.DataFrame:
    """Read and strictly normalize a canonical Parquet file."""

    input_path = Path(path)
    try:
        frame = pl.read_parquet(input_path)
        return coerce_canonical_frame(frame, allow_extra_columns=allow_extra_columns)
    except (OSError, pl.exceptions.PolarsError, DataSchemaError) as exc:
        raise DataLoadError(
            f"cannot load canonical Parquet {input_path}: {exc}"
        ) from exc


def load_frame(
    path: str | Path,
    *,
    allow_extra_columns: bool = False,
) -> pl.DataFrame:
    """Load a canonical frame based on its file extension."""

    input_path = Path(path)
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        return read_csv(input_path, allow_extra_columns=allow_extra_columns)
    if suffix in {".parquet", ".pq"}:
        return read_parquet(input_path, allow_extra_columns=allow_extra_columns)
    raise DataLoadError(
        f"unsupported market-data extension {suffix!r}; expected .csv or .parquet"
    )


def load_events(
    path: str | Path,
    *,
    allow_extra_columns: bool = False,
) -> list[MarketEvent]:
    """Load a canonical file as validated typed events."""

    try:
        return frame_to_events(
            load_frame(path, allow_extra_columns=allow_extra_columns)
        )
    except DataSchemaError as exc:
        raise DataLoadError(
            f"cannot decode canonical events from {path}: {exc}"
        ) from exc


def _as_frame(data: pl.DataFrame | Iterable[MarketEvent]) -> pl.DataFrame:
    if isinstance(data, pl.DataFrame):
        return coerce_canonical_frame(data)
    return events_to_frame(data)


def write_parquet(
    data: pl.DataFrame | Iterable[MarketEvent],
    path: str | Path,
) -> Path:
    """Write canonical data to Parquet and return the resolved output path."""

    output_path = Path(path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _as_frame(data).write_parquet(output_path)
    except (OSError, pl.exceptions.PolarsError, DataSchemaError) as exc:
        raise DataLoadError(
            f"cannot write canonical Parquet {output_path}: {exc}"
        ) from exc
    return output_path.resolve()


def write_csv(
    data: pl.DataFrame | Iterable[MarketEvent],
    path: str | Path,
) -> Path:
    """Write canonical data to CSV and return the resolved output path."""

    output_path = Path(path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _as_frame(data).write_csv(output_path)
    except (OSError, pl.exceptions.PolarsError, DataSchemaError) as exc:
        raise DataLoadError(f"cannot write canonical CSV {output_path}: {exc}") from exc
    return output_path.resolve()


# Explicit names make call sites read naturally without hiding the format.
load_csv = read_csv
load_parquet = read_parquet
