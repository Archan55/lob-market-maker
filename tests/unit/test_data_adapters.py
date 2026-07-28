from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from lobmm.data import (
    LOBSTERCSVAdapter,
    LOBSTERData,
    MarketDataAdapterError,
    TardisCSVAdapter,
    TardisData,
    frame_to_events,
)
from lobmm.enums import EventType, Side
from lobmm.validation import validate_event_stream

FIXTURES = Path(__file__).parents[1] / "fixtures" / "adapters"


def _row_frame(rows: list[list[object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, orient="row", strict=False)


def test_tardis_absolute_updates_and_trades_normalize_without_double_counting() -> None:
    adapter = TardisCSVAdapter(tick_size="0.01")
    frame = adapter.checked(
        TardisData(
            book=FIXTURES / "tardis_incremental_book_L2.csv",
            trades=FIXTURES / "tardis_trades.csv",
        )
    )
    events = frame_to_events(frame)

    assert [
        (
            event.timestamp_ns,
            event.event_type,
            event.side,
            event.price_ticks,
            event.quantity,
        )
        for event in events
    ] == [
        (2_001_000, EventType.RESET, None, 0, 0),
        (2_001_000, EventType.SNAPSHOT, Side.BID, 10_000, 10),
        (2_001_000, EventType.SNAPSHOT, Side.ASK, 10_100, 12),
        (3_001_000, EventType.TRADE, Side.BID, 10_000, 2),
        (3_001_000, EventType.CANCEL, Side.BID, 10_000, 1),
        (4_001_000, EventType.ADD, Side.ASK, 10_100, 3),
        (5_001_000, EventType.ADD, Side.BID, 9_900, 5),
        (6_001_000, EventType.CANCEL, Side.BID, 10_000, 7),
        (7_001_000, EventType.RESET, None, 0, 0),
        (7_001_000, EventType.SNAPSHOT, Side.BID, 10_200, 9),
        (7_001_000, EventType.SNAPSHOT, Side.ASK, 10_300, 11),
    ]
    result = validate_event_stream(events)
    assert result.valid
    assert result.final_book is not None
    assert result.final_book.bids == ((10_200, 9),)
    assert result.final_book.asks == ((10_300, 11),)
    assert adapter.last_diagnostics["pre_snapshot_rows_skipped"] == 1
    assert adapter.last_diagnostics["matched_trade_quantity"] == 2
    assert adapter.last_diagnostics["unmatched_trade_quantity"] == 2
    assert adapter.last_diagnostics["unknown_side_trades_skipped"] == 1
    assert adapter.last_instrument == ("fixture", "ABC-USD")


def test_tardis_quantity_scaling_and_exchange_timestamp_selection() -> None:
    book = pl.DataFrame(
        {
            "exchange": ["fixture", "fixture", "fixture"],
            "symbol": ["FRACTIONAL", "FRACTIONAL", "FRACTIONAL"],
            "timestamp": [10, 10, 11],
            "local_timestamp": [20, 20, 21],
            "is_snapshot": [True, True, False],
            "side": ["bid", "ask", "bid"],
            "price": ["10.0", "10.5", "10.0"],
            "amount": ["0.50", "0.25", "0.25"],
        }
    )
    adapter = TardisCSVAdapter(
        tick_size="0.5",
        quantity_multiplier=4,
        timestamp_source="timestamp",
    )
    events = frame_to_events(adapter.checked(TardisData(book=book)))

    assert events[0].timestamp_ns == 10_000
    assert events[1].quantity == 2
    assert events[2].quantity == 1
    assert events[3].event_type is EventType.CANCEL
    assert events[3].quantity == 1
    assert validate_event_stream(events).valid


@pytest.mark.parametrize(
    ("book", "match"),
    [
        (
            pl.DataFrame(
                {
                    "exchange": ["fixture"],
                    "symbol": ["ABC"],
                    "timestamp": [1],
                    "local_timestamp": [1],
                    "is_snapshot": [False],
                    "side": ["bid"],
                    "price": ["10"],
                    "amount": ["1"],
                }
            ),
            "contains no snapshot",
        ),
        (
            pl.DataFrame(
                {
                    "exchange": ["fixture", "fixture"],
                    "symbol": ["ABC", "XYZ"],
                    "timestamp": [1, 1],
                    "local_timestamp": [1, 1],
                    "is_snapshot": [True, True],
                    "side": ["bid", "ask"],
                    "price": ["10", "11"],
                    "amount": ["1", "1"],
                }
            ),
            "multiple instruments",
        ),
        (
            pl.DataFrame(
                {
                    "exchange": ["fixture", "fixture"],
                    "symbol": ["ABC", "ABC"],
                    "timestamp": [1, 1],
                    "local_timestamp": [1, 1],
                    "is_snapshot": [True, True],
                    "side": ["bid", "ask"],
                    "price": ["11", "10"],
                    "amount": ["1", "1"],
                }
            ),
            "crossed",
        ),
    ],
)
def test_tardis_rejects_streams_that_require_unsafe_inference(
    book: pl.DataFrame,
    match: str,
) -> None:
    with pytest.raises(MarketDataAdapterError, match=match):
        TardisCSVAdapter(tick_size="1").to_canonical(TardisData(book=book))


def test_tardis_rejects_unrepresentable_fractional_quantity() -> None:
    book = pl.DataFrame(
        {
            "exchange": ["fixture"],
            "symbol": ["ABC"],
            "timestamp": [1],
            "local_timestamp": [1],
            "is_snapshot": [True],
            "side": ["bid"],
            "price": ["10"],
            "amount": ["0.25"],
        }
    )
    with pytest.raises(MarketDataAdapterError, match="cannot be represented"):
        TardisCSVAdapter(tick_size="1").to_canonical(TardisData(book=book))


def test_lobster_pair_preserves_visible_causes_and_reconciles_depth_window() -> None:
    adapter = LOBSTERCSVAdapter(
        tick_size="0.01",
        trading_date=date(2024, 1, 2),
        levels=2,
    )
    events = frame_to_events(
        adapter.checked(
            LOBSTERData(
                messages=FIXTURES / "lobster_message.csv",
                orderbook=FIXTURES / "lobster_orderbook.csv",
            )
        )
    )

    assert events[0].event_type is EventType.RESET
    assert [event.side for event in events[1:5]] == [
        Side.BID,
        Side.BID,
        Side.ASK,
        Side.ASK,
    ]
    assert events[5].event_type is EventType.TRADE
    assert events[5].side is Side.ASK
    assert events[5].price_ticks == 10_100
    assert events[5].quantity == 4
    assert events[6].event_type is EventType.CANCEL
    assert events[6].side is Side.BID

    # The new best bid is causal; the old deepest visible level leaving the
    # requested two-level window is an explicit reconciliation cancellation.
    assert (events[7].event_type, events[7].price_ticks) == (
        EventType.ADD,
        10_050,
    )
    assert (events[8].event_type, events[8].price_ticks) == (
        EventType.CANCEL,
        9_900,
    )

    assert sum(event.event_type is EventType.RESET for event in events) == 3
    assert events[-2].event_type is EventType.TRADE
    assert events[-2].side is Side.BID
    assert events[-2].price_ticks == 10_050
    assert events[-1].event_type is EventType.ADD
    assert events[-1].price_ticks == 9_900
    expected_open_ns = (
        int(datetime(2024, 1, 2, 14, 30, tzinfo=UTC).timestamp()) * 1_000_000_000 + 1
    )
    assert events[0].timestamp_ns == expected_open_ns
    assert events[1].timestamp_ns - events[0].timestamp_ns == 0
    assert events[5].timestamp_ns - events[0].timestamp_ns == 1

    result = validate_event_stream(events)
    assert result.valid
    assert result.final_book is not None
    assert result.final_book.bids == ((10_000, 10), (9_900, 15))
    assert result.final_book.asks == ((10_100, 6), (10_200, 20))
    assert adapter.last_diagnostics["hidden_executions_not_emitted"] == 1
    assert adapter.last_diagnostics["halt_resets"] == 1
    assert adapter.last_diagnostics["halt_resumes"] == 1


def test_lobster_ignores_zero_size_dummy_levels() -> None:
    messages = _row_frame([[1, 1, 1, 1, 1_000_000, 1]])
    orderbook = _row_frame(
        [
            [
                1_010_000,
                2,
                1_000_000,
                3,
                9_999_999_999,
                0,
                -9_999_999_999,
                0,
            ]
        ]
    )
    adapter = LOBSTERCSVAdapter(
        tick_size="0.01",
        trading_date="2024-01-02",
        levels=2,
        timezone=UTC,
    )
    events = frame_to_events(
        adapter.checked(LOBSTERData(messages=messages, orderbook=orderbook))
    )
    assert len(events) == 3
    assert {event.side for event in events[1:]} == {Side.BID, Side.ASK}
    assert validate_event_stream(events).valid


@pytest.mark.parametrize(
    ("messages", "orderbook", "match"),
    [
        (
            _row_frame([[1, 1, 1, 1, 1_000_000, 1]]),
            _row_frame(
                [
                    [1_010_000, 1, 1_000_000, 1],
                    [1_010_000, 1, 1_000_000, 1],
                ]
            ),
            "row counts differ",
        ),
        (
            _row_frame([["1.0000000001", 1, 1, 1, 1_000_000, 1]]),
            _row_frame([[1_010_000, 1, 1_000_000, 1]]),
            "finer than one nanosecond",
        ),
        (
            _row_frame([[1, 1, 1, 1, 1_000_000, 0]]),
            _row_frame([[1_010_000, 1, 1_000_000, 1]]),
            "direction must be",
        ),
        (
            _row_frame([[1, 1, 1, 1, 1_010_000, -1]]),
            _row_frame([[1_000_000, 1, 1_010_000, 1]]),
            "crossed",
        ),
    ],
)
def test_lobster_rejects_misaligned_or_ambiguous_pairs(
    messages: pl.DataFrame,
    orderbook: pl.DataFrame,
    match: str,
) -> None:
    adapter = LOBSTERCSVAdapter(
        tick_size="0.01",
        trading_date="2024-01-02",
        timezone=UTC,
    )
    with pytest.raises(MarketDataAdapterError, match=match):
        adapter.to_canonical(LOBSTERData(messages=messages, orderbook=orderbook))
