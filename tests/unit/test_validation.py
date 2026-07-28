from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from lobmm.data.adapters import BaseL2DataAdapter, L2DataAdapter
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import (
    DataLoadError,
    load_events,
    load_frame,
    write_csv,
    write_parquet,
)
from lobmm.data.schema import (
    CANONICAL_COLUMNS,
    DataSchemaError,
    coerce_canonical_frame,
    events_to_frame,
    frame_to_events,
)
from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import MarketEvent
from lobmm.validation import (
    StreamValidationError,
    validate_event_stream,
    validate_file,
    validate_frame,
)


def valid_events() -> list[MarketEvent]:
    return [
        MarketEvent.reset(0, 0),
        MarketEvent(0, 1, EventType.SNAPSHOT, Side.BID, 100, 10),
        MarketEvent(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 12),
        MarketEvent(1, 3, EventType.ADD, Side.BID, 99, 8),
        MarketEvent(2, 4, EventType.CANCEL, Side.ASK, 102, 2),
        MarketEvent(3, 5, EventType.TRADE, Side.BID, 100, 4),
    ]


def test_valid_stream_reconstructs_expected_final_book() -> None:
    result = validate_event_stream(valid_events())
    assert result.valid
    assert result.event_count == 6
    assert result.final_book is not None
    assert result.final_book.bids == ((100, 6), (99, 8))
    assert result.final_book.asks == ((102, 10),)
    assert dict(result.diagnostics) == {}
    assert result.event_stream_sha256 == event_stream_sha256(valid_events())
    assert result.mode is ValidationMode.STRICT
    assert result.reconstructed_book
    assert result.required_nonempty


def test_timestamp_regression_and_duplicate_sequence_fail_strictly() -> None:
    events = valid_events()
    events[4] = MarketEvent(0, 4, EventType.CANCEL, Side.ASK, 102, 2)
    with pytest.raises(StreamValidationError, match="timestamp_regression"):
        validate_event_stream(events)

    duplicate = valid_events()
    duplicate[3] = MarketEvent(1, 2, EventType.ADD, Side.BID, 99, 8)
    with pytest.raises(StreamValidationError, match="sequence_not_strictly_increasing"):
        validate_event_stream(duplicate)


def test_lenient_stream_reports_order_and_depth_repairs() -> None:
    events = valid_events()[:3]
    events.extend(
        [
            MarketEvent(2, 3, EventType.CANCEL, Side.BID, 100, 999),
            MarketEvent(1, 4, EventType.ADD, Side.BID, 99, 1),
        ]
    )
    result = validate_event_stream(events, mode=ValidationMode.LENIENT)
    assert not result.valid
    assert {issue.code for issue in result.issues} == {
        "book_event_clamped",
        "timestamp_regression",
    }
    assert result.diagnostics["clamped_cancel"] == 1
    assert result.final_book is not None
    assert result.final_book.bids == ((99, 1),)


def test_empty_stream_policy_is_explicit() -> None:
    with pytest.raises(StreamValidationError, match="empty_stream"):
        validate_event_stream([])
    result = validate_event_stream([], require_nonempty=False)
    assert result.valid
    assert result.event_count == 0
    assert result.final_book is None


def test_frame_round_trip_preserves_exact_events_and_schema() -> None:
    events = valid_events()
    frame = events_to_frame(events)
    assert tuple(frame.columns) == CANONICAL_COLUMNS
    assert frame_to_events(frame) == events
    assert validate_frame(frame).valid


def test_schema_rejects_missing_extra_and_invalid_enum_fields() -> None:
    frame = events_to_frame(valid_events())
    with pytest.raises(DataSchemaError, match="missing"):
        coerce_canonical_frame(frame.drop("quantity"))
    with pytest.raises(DataSchemaError, match="unexpected"):
        coerce_canonical_frame(frame.with_columns(pl.lit(1).alias("extra")))
    assert coerce_canonical_frame(
        frame.with_columns(pl.lit(1).alias("extra")),
        allow_extra_columns=True,
    ).columns == list(CANONICAL_COLUMNS)

    invalid = frame.with_columns(
        pl.when(pl.col("sequence_number") == 1)
        .then(pl.lit("MODIFY"))
        .otherwise(pl.col("event_type"))
        .alias("event_type")
    )
    with pytest.raises(DataSchemaError, match="invalid canonical event"):
        frame_to_events(invalid)


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_file_round_trip_and_validate_file(tmp_path: Path, suffix: str) -> None:
    output = tmp_path / f"events{suffix}"
    events = valid_events()
    if suffix == ".csv":
        returned = write_csv(events, output)
    else:
        returned = write_parquet(events_to_frame(events), output)
    assert returned == output.resolve()
    assert load_events(output) == events
    assert load_frame(output).columns == list(CANONICAL_COLUMNS)
    assert validate_file(output).valid


def test_loader_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    with pytest.raises(DataLoadError, match="unsupported"):
        load_frame(path)


class ExampleAdapter(BaseL2DataAdapter[list[dict[str, object]]]):
    @property
    def name(self) -> str:
        return "example"

    def to_canonical(self, source: list[dict[str, object]]) -> pl.DataFrame:
        return pl.DataFrame(source)


def test_adapter_abc_and_protocol_check_canonical_output() -> None:
    source = [event.as_dict() for event in valid_events()]
    adapter = ExampleAdapter()
    assert isinstance(adapter, L2DataAdapter)
    assert adapter.name == "example"
    assert frame_to_events(adapter.checked(source)) == valid_events()
