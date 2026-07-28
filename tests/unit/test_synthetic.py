from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from statistics import fmean

from lobmm.book import L2Book
from lobmm.config import AppConfig, SyntheticConfig
from lobmm.data.loaders import load_events, read_parquet
from lobmm.data.schema import CANONICAL_COLUMNS
from lobmm.enums import EventType, Side
from lobmm.synthetic import (
    OrderFlowRegime,
    SyntheticGenerationError,
    VolatilityRegime,
    generate_synthetic_events,
    generate_synthetic_frame,
    regime_for_sequence,
    write_synthetic_parquet,
)
from lobmm.validation import validate_event_stream


def compact_config() -> SyntheticConfig:
    return SyntheticConfig(
        event_count=180,
        start_timestamp_ns=100,
        interval_ns=10,
        start_price_ticks=10_000,
        levels=3,
        base_quantity=40,
        regime_length=30,
        reset_interval=45,
    )


def test_generator_is_exact_reproducible_and_seed_sensitive() -> None:
    config = compact_config()
    first = generate_synthetic_events(config, seed=17)
    second = generate_synthetic_events(config, seed=17)
    different = generate_synthetic_events(config, seed=18)
    assert len(first) == config.event_count
    assert first == second
    assert first != different
    assert [event.sequence_number for event in first] == list(range(config.event_count))
    assert all(
        left.timestamp_ns <= right.timestamp_ns for left, right in pairwise(first)
    )
    assert validate_event_stream(first).valid


def test_generator_builds_multilevel_depth_and_changing_spreads() -> None:
    events = generate_synthetic_events(compact_config(), seed=9)
    book = L2Book()
    observed_spreads: set[int] = set()
    observed_depths: set[tuple[int, int]] = set()
    spread_path: list[int] = []
    maximum_levels = 0
    for event in events:
        book.apply(event)
        if book.spread is not None:
            observed_spreads.add(book.spread)
            spread_path.append(book.spread)
            assert book.best_bid is not None
            assert book.best_ask is not None
            observed_depths.add(
                (
                    book.quantity_at(Side.BID, book.best_bid),
                    book.quantity_at(Side.ASK, book.best_ask),
                )
            )
        maximum_levels = max(maximum_levels, len(book.bids), len(book.asks))
        book.assert_valid()
    assert maximum_levels >= compact_config().levels
    assert len(observed_spreads) >= 2
    assert len(observed_depths) >= 5
    assert fmean(spread_path) <= 4.5
    assert max(spread_path) <= 12
    assert {event.event_type for event in events} >= {
        EventType.ADD,
        EventType.CANCEL,
        EventType.TRADE,
        EventType.RESET,
        EventType.SNAPSHOT,
    }


def test_regime_schedule_includes_volatility_and_order_flow_states() -> None:
    config = compact_config()
    regimes = {
        regime_for_sequence(sequence, config)
        for sequence in range(0, config.event_count, config.regime_length)
    }
    assert {regime.volatility for regime in regimes} == {
        VolatilityRegime.LOW,
        VolatilityRegime.MEDIUM,
        VolatilityRegime.HIGH,
    }
    assert {regime.order_flow for regime in regimes} == {
        OrderFlowRegime.BALANCED,
        OrderFlowRegime.SELL_PRESSURE,
        OrderFlowRegime.BUY_PRESSURE,
    }


def test_frame_and_parquet_outputs_use_canonical_contract(tmp_path: Path) -> None:
    config = compact_config()
    frame = generate_synthetic_frame(config, seed=4)
    assert tuple(frame.columns) == CANONICAL_COLUMNS
    output = tmp_path / "nested" / "synthetic.parquet"
    returned = write_synthetic_parquet(config, output, seed=4)
    assert returned == output.resolve()
    assert read_parquet(output).equals(frame)
    assert load_events(output) == generate_synthetic_events(config, seed=4)


def test_app_config_seed_is_used_when_no_override_is_given() -> None:
    config = AppConfig(
        synthetic=compact_config(),
        random_seed=123,
    )
    assert generate_synthetic_events(config) == generate_synthetic_events(
        compact_config(), seed=123
    )


def test_impossible_initial_depth_fails_clearly() -> None:
    config = SyntheticConfig(event_count=10, levels=5)
    try:
        generate_synthetic_events(config)
    except SyntheticGenerationError as exc:
        assert "at least 11" in str(exc)
    else:  # pragma: no cover - defensive test guard
        raise AssertionError("expected SyntheticGenerationError")
