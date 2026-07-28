from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lobmm.book import L2Book
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    operations=st.lists(
        st.tuples(
            st.sampled_from(tuple(Side)),
            st.sampled_from((EventType.ADD, EventType.CANCEL, EventType.TRADE)),
            st.integers(min_value=0, max_value=9),
            st.integers(min_value=1, max_value=500),
        ),
        min_size=1,
        max_size=100,
    )
)
def test_valid_event_prefixes_preserve_book_invariants(
    operations: list[tuple[Side, EventType, int, int]],
) -> None:
    book = L2Book()
    book.apply_snapshot([(99, 100)], [(101, 100)])
    for sequence, (
        side,
        requested_type,
        price_offset,
        requested_quantity,
    ) in enumerate(operations):
        levels = book.bids if side is Side.BID else book.asks
        if requested_type is EventType.ADD or not levels:
            event_type = EventType.ADD
            price = 99 - price_offset if side is Side.BID else 101 + price_offset
            quantity = requested_quantity
        else:
            event_type = requested_type
            prices = sorted(levels, reverse=side is Side.BID)
            price = prices[price_offset % len(prices)]
            available = levels[price]
            if len(levels) == 1:
                # Preserve a two-sided book in every generated prefix.
                quantity = min(requested_quantity, max(available - 1, 0))
            else:
                quantity = min(requested_quantity, available)
            if quantity == 0:
                event_type = EventType.ADD
                price = 99 - price_offset if side is Side.BID else 101 + price_offset
                quantity = requested_quantity

        event = MarketEvent(
            timestamp_ns=sequence,
            sequence_number=sequence,
            event_type=event_type,
            side=side,
            price_ticks=price,
            quantity=quantity,
        )
        book.apply(event)
        book.assert_valid()
        assert all(value > 0 for value in book.bids.values())
        assert all(value > 0 for value in book.asks.values())
        assert book.best_bid is not None
        assert book.best_ask is not None
        assert book.best_bid < book.best_ask


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    side=st.sampled_from(tuple(Side)),
    price=st.integers(min_value=1, max_value=1_000_000),
    quantity=st.integers(min_value=1, max_value=1_000_000),
)
def test_exact_reduction_removes_empty_price_level(
    side: Side,
    price: int,
    quantity: int,
) -> None:
    # Use only one populated side here, so every positive integer price is a
    # valid book key and crossing is irrelevant.
    book = L2Book()
    book.apply(MarketEvent(0, 0, EventType.ADD, side, price, quantity))
    book.apply(MarketEvent(1, 1, EventType.CANCEL, side, price, quantity))
    assert book.quantity_at(side, price) == 0
    assert price not in (book.bids if side is Side.BID else book.asks)


@pytest.mark.property
@settings(max_examples=75, deadline=None)
@given(
    bid_levels=st.dictionaries(
        keys=st.integers(min_value=90, max_value=99),
        values=st.integers(min_value=1, max_value=1_000),
        min_size=1,
    ),
    ask_levels=st.dictionaries(
        keys=st.integers(min_value=101, max_value=110),
        values=st.integers(min_value=1, max_value=1_000),
        min_size=1,
    ),
)
def test_same_snapshot_produces_identical_state(
    bid_levels: dict[int, int],
    ask_levels: dict[int, int],
) -> None:
    first = L2Book()
    second = L2Book()
    snapshot = (tuple(bid_levels.items()), tuple(ask_levels.items()))
    first.apply_snapshot(*snapshot)
    second.apply_snapshot(*snapshot)
    assert first == second
    assert first.view(1, 1) == second.view(1, 1)
