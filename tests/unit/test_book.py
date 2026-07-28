from __future__ import annotations

import pytest

from lobmm.book import (
    BookError,
    CrossedBookError,
    InsufficientDepthError,
    L2Book,
)
from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import MarketEvent


def event(
    sequence: int,
    event_type: EventType,
    side: Side,
    price: int,
    quantity: int,
) -> MarketEvent:
    return MarketEvent(sequence, sequence, event_type, side, price, quantity)


def seeded_book(
    mode: ValidationMode = ValidationMode.STRICT,
) -> L2Book:
    book = L2Book(mode)
    book.apply_snapshot(
        bids=[(100, 10), (99, 20), (98, 30)],
        asks=[(102, 40), (103, 50), (104, 60)],
    )
    return book


def test_snapshot_and_derived_features() -> None:
    book = seeded_book()
    assert book.best_bid == 100
    assert book.best_ask == 102
    assert book.spread == 2
    assert book.midpoint == 101.0
    assert book.microprice == pytest.approx((102 * 10 + 100 * 40) / 50)
    assert book.top_level_imbalance == pytest.approx((10 - 40) / 50)
    assert book.top_n(Side.BID, 2) == ((100, 10), (99, 20))
    assert book.top_n(Side.ASK, 2) == ((102, 40), (103, 50))
    assert book.weighted_imbalance(2) == pytest.approx(
        ((10 + 20 / 2) - (40 + 50 / 2)) / ((10 + 20 / 2) + (40 + 50 / 2))
    )
    book.assert_valid()


def test_add_cancel_trade_and_empty_level_removal() -> None:
    book = seeded_book()
    added = book.apply(event(1, EventType.ADD, Side.BID, 100, 5))
    assert (added.quantity_before, added.quantity_applied, added.quantity_after) == (
        10,
        5,
        15,
    )
    book.apply(event(2, EventType.CANCEL, Side.BID, 100, 4))
    assert book.quantity_at(Side.BID, 100) == 11
    book.apply(event(3, EventType.TRADE, Side.BID, 100, 11))
    assert book.quantity_at(Side.BID, 100) == 0
    assert book.best_bid == 99


def test_reset_clears_both_sides() -> None:
    book = seeded_book()
    delta = book.apply(MarketEvent.reset(10, 10))
    assert len(book) == 0
    assert book.best_bid is None
    assert book.best_ask is None
    assert delta.quantity_after == 0


def test_strict_reduction_rejects_insufficient_depth_without_mutation() -> None:
    book = seeded_book()
    with pytest.raises(InsufficientDepthError, match="displayed=10"):
        book.apply(event(1, EventType.CANCEL, Side.BID, 100, 11))
    assert book.quantity_at(Side.BID, 100) == 10


def test_lenient_reduction_clamps_and_records_diagnostic() -> None:
    book = seeded_book(ValidationMode.LENIENT)
    delta = book.apply(event(1, EventType.TRADE, Side.BID, 100, 999))
    assert delta.clamped
    assert delta.quantity_applied == 10
    assert book.quantity_at(Side.BID, 100) == 0
    assert book.diagnostics["clamped_trade"] == 1


def test_crossing_add_is_atomic_in_strict_and_ignored_in_lenient() -> None:
    strict = seeded_book()
    with pytest.raises(CrossedBookError):
        strict.apply(event(1, EventType.ADD, Side.BID, 102, 1))
    assert strict.quantity_at(Side.BID, 102) == 0
    assert (strict.best_bid, strict.best_ask) == (100, 102)
    strict.assert_valid()

    lenient = seeded_book(ValidationMode.LENIENT)
    delta = lenient.apply(event(1, EventType.ADD, Side.ASK, 100, 1))
    assert delta.clamped
    assert lenient.quantity_at(Side.ASK, 100) == 0
    assert (lenient.best_bid, lenient.best_ask) == (100, 102)
    assert lenient.diagnostics["crossed_add_ignored"] == 1
    lenient.assert_valid()


def test_apply_snapshot_rejects_duplicates_invalid_levels_and_crosses() -> None:
    book = L2Book()
    with pytest.raises(BookError, match="duplicate"):
        book.apply_snapshot([(100, 1), (100, 2)], [(101, 1)])
    with pytest.raises(BookError, match="positive"):
        book.apply_snapshot([(100, 0)], [(101, 1)])
    with pytest.raises(CrossedBookError):
        book.apply_snapshot([(101, 1)], [(101, 1)])
    assert len(book) == 0


def test_crossed_snapshot_restores_prior_levels_and_cached_best_prices() -> None:
    book = seeded_book()
    prior_bids = dict(book.bids)
    prior_asks = dict(book.asks)

    with pytest.raises(CrossedBookError):
        book.apply_snapshot([(105, 1)], [(104, 1)])

    assert dict(book.bids) == prior_bids
    assert dict(book.asks) == prior_asks
    assert (book.best_bid, book.best_ask) == (100, 102)
    book.assert_valid()


def test_executable_levels_and_consume_respect_limit() -> None:
    book = seeded_book()
    assert book.executable_levels(Side.BID, 103) == ((102, 40), (103, 50))
    assert book.executable_levels(Side.ASK, 99) == ((100, 10), (99, 20))
    assert book.consume(Side.ASK, 102, 15) == 15
    assert book.quantity_at(Side.ASK, 102) == 25
    assert book.consume(Side.ASK, 102, 100) == 25
    assert book.quantity_at(Side.ASK, 102) == 0
    assert book.best_ask == 103
    assert book.consume(Side.ASK, 103, 50) == 50
    assert book.consume(Side.ASK, 104, 60) == 60
    assert book.best_ask is None


def test_integrity_check_detects_corrupted_best_price_cache() -> None:
    bid_cache = seeded_book()
    bid_cache._best_bid = 99  # type: ignore[attr-defined]
    with pytest.raises(BookError, match="cached best bid"):
        bid_cache.assert_valid()

    ask_cache = seeded_book()
    ask_cache._best_ask = 103  # type: ignore[attr-defined]
    with pytest.raises(BookError, match="cached best ask"):
        ask_cache.assert_valid()


def test_view_is_a_frozen_snapshot_of_requested_depth() -> None:
    book = seeded_book()
    view = book.view(timestamp_ns=50, sequence_number=7, depth=2)
    assert view.timestamp_ns == 50
    assert view.sequence_number == 7
    assert view.bids == ((100, 10), (99, 20))
    assert view.asks == ((102, 40), (103, 50))


def test_depth_arguments_are_checked() -> None:
    book = seeded_book()
    with pytest.raises(ValueError, match="nonnegative"):
        book.top_n(Side.BID, -1)
    with pytest.raises(ValueError, match="positive"):
        book.weighted_imbalance(0)
