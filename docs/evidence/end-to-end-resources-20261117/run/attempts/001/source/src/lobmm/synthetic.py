"""Deterministic, valid synthetic Level 2 event generation.

The generator is an engineering fixture, not a calibrated market model. It
creates reproducible multi-level books with alternating spread, volatility,
and order-flow regimes so the simulator can be exercised without proprietary
data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np
import polars as pl

from lobmm.book import L2Book
from lobmm.config import AppConfig, SyntheticConfig
from lobmm.data.loaders import write_parquet
from lobmm.data.schema import events_to_frame
from lobmm.enums import EventType, Side, ValidationMode
from lobmm.events import MarketEvent
from lobmm.validation import validate_event_stream


class SyntheticGenerationError(ValueError):
    """Raised when a requested synthetic stream cannot be constructed."""


class VolatilityRegime(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OrderFlowRegime(StrEnum):
    SELL_PRESSURE = "sell_pressure"
    BALANCED = "balanced"
    BUY_PRESSURE = "buy_pressure"


@dataclass(frozen=True, slots=True)
class Regime:
    volatility: VolatilityRegime
    order_flow: OrderFlowRegime
    volatility_scale_ticks: int
    spread_ticks: int
    ask_trade_probability: float


_REGIMES: tuple[Regime, ...] = (
    Regime(
        VolatilityRegime.LOW,
        OrderFlowRegime.BALANCED,
        volatility_scale_ticks=1,
        spread_ticks=1,
        ask_trade_probability=0.50,
    ),
    Regime(
        VolatilityRegime.MEDIUM,
        OrderFlowRegime.SELL_PRESSURE,
        volatility_scale_ticks=2,
        spread_ticks=2,
        ask_trade_probability=0.25,
    ),
    Regime(
        VolatilityRegime.HIGH,
        OrderFlowRegime.BUY_PRESSURE,
        volatility_scale_ticks=4,
        spread_ticks=4,
        ask_trade_probability=0.75,
    ),
)


def regime_for_sequence(
    sequence_number: int,
    config: SyntheticConfig,
) -> Regime:
    """Return the deterministic regime assigned to a source sequence."""

    if sequence_number < 0:
        raise ValueError("sequence_number must be nonnegative")
    index = (sequence_number // config.regime_length) % len(_REGIMES)
    return _REGIMES[index]


class _SyntheticGenerator:
    def __init__(self, config: SyntheticConfig, seed: int) -> None:
        if seed < 0:
            raise SyntheticGenerationError("seed must be nonnegative")
        minimum_events = 1 + 2 * config.levels
        if config.event_count < minimum_events:
            raise SyntheticGenerationError(
                "synthetic.event_count must be at least "
                f"{minimum_events} for {config.levels} levels per side"
            )
        if config.start_price_ticks <= config.levels + 5:
            raise SyntheticGenerationError(
                "synthetic.start_price_ticks is too small for the requested depth"
            )

        self.config = config
        self.rng = np.random.default_rng(seed)
        self.book = L2Book(ValidationMode.STRICT)
        self.events: list[MarketEvent] = []
        self.timestamp_ns = config.start_timestamp_ns
        self.center_ticks = config.start_price_ticks
        self.events_since_reset = 0
        self.last_reset_regime_index = -1

    @property
    def sequence_number(self) -> int:
        return len(self.events)

    @property
    def remaining(self) -> int:
        return self.config.event_count - len(self.events)

    @property
    def snapshot_batch_size(self) -> int:
        return 1 + 2 * self.config.levels

    def emit(
        self,
        event_type: EventType,
        *,
        side: Side | None = None,
        price_ticks: int = 0,
        quantity: int = 0,
    ) -> None:
        event = MarketEvent(
            timestamp_ns=self.timestamp_ns,
            sequence_number=self.sequence_number,
            event_type=event_type,
            side=side,
            price_ticks=price_ticks,
            quantity=quantity,
        )
        self.book.apply(event)
        self.events.append(event)
        self.events_since_reset += 1

    def generate(self) -> list[MarketEvent]:
        self._reset_and_snapshot(initial=True)
        while self.remaining:
            self.timestamp_ns += self.config.interval_ns
            current_regime_index = self.sequence_number // self.config.regime_length
            regime_changed = current_regime_index != self.last_reset_regime_index
            reset_due = self.events_since_reset >= self.config.reset_interval
            if self.remaining >= self.snapshot_batch_size and (
                regime_changed or reset_due
            ):
                self._reset_and_snapshot(initial=False)
            else:
                self._emit_incremental_event()

        # This is deliberately an internal assertion through the public
        # validator: generated fixtures must remain acceptable to ingestion.
        validate_event_stream(self.events, mode=ValidationMode.STRICT)
        return list(self.events)

    def _reset_and_snapshot(self, *, initial: bool) -> None:
        regime_index = self.sequence_number // self.config.regime_length
        regime = regime_for_sequence(self.sequence_number, self.config)
        if not initial:
            random_component = int(
                self.rng.integers(
                    -regime.volatility_scale_ticks,
                    regime.volatility_scale_ticks + 1,
                )
            )
            directed_component = (
                regime.volatility_scale_ticks
                if regime.order_flow is OrderFlowRegime.BUY_PRESSURE
                else -regime.volatility_scale_ticks
                if regime.order_flow is OrderFlowRegime.SELL_PRESSURE
                else 1
            )
            self.center_ticks = max(
                self.config.levels + 5,
                self.center_ticks + random_component + directed_component,
            )

        self.emit(EventType.RESET)
        self.events_since_reset = 0
        best_bid = self.center_ticks - regime.spread_ticks // 2
        best_ask = best_bid + regime.spread_ticks
        for level in range(self.config.levels):
            bid_quantity = self._snapshot_quantity(level, Side.BID, regime)
            ask_quantity = self._snapshot_quantity(level, Side.ASK, regime)
            self.emit(
                EventType.SNAPSHOT,
                side=Side.BID,
                price_ticks=best_bid - level,
                quantity=bid_quantity,
            )
            self.emit(
                EventType.SNAPSHOT,
                side=Side.ASK,
                price_ticks=best_ask + level,
                quantity=ask_quantity,
            )
        self.events_since_reset = 0
        self.last_reset_regime_index = regime_index

    def _snapshot_quantity(
        self,
        level: int,
        side: Side,
        regime: Regime,
    ) -> int:
        base = self.config.base_quantity + level * max(
            1, self.config.base_quantity // 10
        )
        pressure_multiplier = 1.0
        if regime.order_flow is OrderFlowRegime.BUY_PRESSURE:
            pressure_multiplier = 1.20 if side is Side.BID else 0.80
        elif regime.order_flow is OrderFlowRegime.SELL_PRESSURE:
            pressure_multiplier = 0.80 if side is Side.BID else 1.20
        jitter_bound = max(2, self.config.base_quantity // 5)
        jitter = int(self.rng.integers(-jitter_bound, jitter_bound + 1))
        return max(1, round(base * pressure_multiplier) + jitter)

    def _emit_incremental_event(self) -> None:
        regime = regime_for_sequence(self.sequence_number, self.config)
        if self._repair_spread_if_needed(regime):
            return
        if self._repair_depth_if_needed(regime):
            return
        draw = float(self.rng.random())
        if draw < 0.43:
            self._emit_add(regime)
        elif draw < 0.72:
            self._emit_reduction(EventType.CANCEL, regime)
        else:
            self._emit_reduction(EventType.TRADE, regime)

    def _repair_spread_if_needed(self, regime: Regime) -> bool:
        best_bid = self.book.best_bid
        best_ask = self.book.best_ask
        assert best_bid is not None and best_ask is not None
        spread = best_ask - best_bid
        maximum_normal_spread = regime.spread_ticks + regime.volatility_scale_ticks
        if spread <= maximum_normal_spread:
            return False

        if regime.order_flow is OrderFlowRegime.BUY_PRESSURE:
            side = Side.BID
        elif regime.order_flow is OrderFlowRegime.SELL_PRESSURE:
            side = Side.ASK
        else:
            side = Side.BID if float(self.rng.random()) < 0.5 else Side.ASK
        price = (
            best_ask - regime.spread_ticks
            if side is Side.BID
            else best_bid + regime.spread_ticks
        )
        self.emit(
            EventType.ADD,
            side=side,
            price_ticks=price,
            quantity=self._snapshot_quantity(0, side, regime),
        )
        return True

    def _repair_depth_if_needed(self, regime: Regime) -> bool:
        bid_shortfall = self.config.levels - len(self.book.bids)
        ask_shortfall = self.config.levels - len(self.book.asks)
        if bid_shortfall <= 0 and ask_shortfall <= 0:
            return False
        if bid_shortfall == ask_shortfall:
            side = Side.BID if float(self.rng.random()) < 0.5 else Side.ASK
        else:
            side = Side.BID if bid_shortfall > ask_shortfall else Side.ASK

        levels = self.book.bids if side is Side.BID else self.book.asks
        if side is Side.BID:
            price = min(levels) - 1
            if price <= 0:
                return False
        else:
            price = max(levels) + 1
        self.emit(
            EventType.ADD,
            side=side,
            price_ticks=price,
            quantity=self._snapshot_quantity(self.config.levels - 1, side, regime),
        )
        return True

    def _draw_side(self, regime: Regime, *, for_trade: bool) -> Side:
        if for_trade:
            return (
                Side.ASK
                if float(self.rng.random()) < regime.ask_trade_probability
                else Side.BID
            )
        bid_probability = (
            0.65
            if regime.order_flow is OrderFlowRegime.BUY_PRESSURE
            else 0.35
            if regime.order_flow is OrderFlowRegime.SELL_PRESSURE
            else 0.50
        )
        return Side.BID if float(self.rng.random()) < bid_probability else Side.ASK

    def _emit_add(self, regime: Regime) -> None:
        side = self._draw_side(regime, for_trade=False)
        levels = self.book.bids if side is Side.BID else self.book.asks
        existing_prices = tuple(levels)
        use_existing = bool(existing_prices) and float(self.rng.random()) < 0.62
        if use_existing:
            price = existing_prices[int(self.rng.integers(0, len(existing_prices)))]
        else:
            price = self._new_valid_price(side)
        quantity = int(self.rng.integers(1, max(3, self.config.base_quantity // 2) + 1))
        self.emit(
            EventType.ADD,
            side=side,
            price_ticks=price,
            quantity=quantity,
        )

    def _new_valid_price(self, side: Side) -> int:
        best_bid = self.book.best_bid
        best_ask = self.book.best_ask
        assert best_bid is not None and best_ask is not None
        spread = best_ask - best_bid
        improve = spread > 1 and float(self.rng.random()) < 0.35
        if side is Side.BID:
            if improve:
                return min(best_bid + 1, best_ask - 1)
            offset = int(self.rng.integers(0, self.config.levels + 2))
            return max(1, best_bid - offset)
        if improve:
            return max(best_ask - 1, best_bid + 1)
        offset = int(self.rng.integers(0, self.config.levels + 2))
        return best_ask + offset

    def _emit_reduction(self, event_type: EventType, regime: Regime) -> None:
        side = self._draw_side(regime, for_trade=event_type is EventType.TRADE)
        levels = self.book.bids if side is Side.BID else self.book.asks
        if not levels:
            self._emit_add(regime)
            return

        if event_type is EventType.TRADE:
            price = self.book.best_bid if side is Side.BID else self.book.best_ask
            assert price is not None
        else:
            prices = tuple(levels)
            price = prices[int(self.rng.integers(0, len(prices)))]

        available = levels[price]
        # Keep at least one level on each side. This makes every prefix usable,
        # including a stream truncated by an event-count limit.
        maximum = available
        if len(levels) == 1:
            maximum = available - 1
        if maximum <= 0:
            self._emit_add(regime)
            return

        remove_full_level = len(levels) > 1 and float(self.rng.random()) < 0.22
        quantity = (
            maximum if remove_full_level else int(self.rng.integers(1, maximum + 1))
        )
        self.emit(
            event_type,
            side=side,
            price_ticks=price,
            quantity=quantity,
        )


def _config_and_seed(
    config: SyntheticConfig | AppConfig | None,
    seed: int | None,
) -> tuple[SyntheticConfig, int]:
    if config is None:
        synthetic_config = SyntheticConfig()
        configured_seed = 7
    elif isinstance(config, AppConfig):
        synthetic_config = config.synthetic
        configured_seed = config.random_seed
    elif isinstance(config, SyntheticConfig):
        synthetic_config = config
        configured_seed = 7
    else:
        raise TypeError("config must be SyntheticConfig, AppConfig, or None")
    return synthetic_config, configured_seed if seed is None else seed


def generate_synthetic_events(
    config: SyntheticConfig | AppConfig | None = None,
    *,
    seed: int | None = None,
) -> list[MarketEvent]:
    """Generate exactly ``event_count`` deterministic canonical events."""

    synthetic_config, effective_seed = _config_and_seed(config, seed)
    return _SyntheticGenerator(synthetic_config, effective_seed).generate()


def generate_synthetic_frame(
    config: SyntheticConfig | AppConfig | None = None,
    *,
    seed: int | None = None,
) -> pl.DataFrame:
    """Generate deterministic events as a canonical Polars frame."""

    return events_to_frame(generate_synthetic_events(config, seed=seed))


def write_synthetic_parquet(
    config: SyntheticConfig | AppConfig | None,
    output_path: str | Path,
    *,
    seed: int | None = None,
) -> Path:
    """Generate and persist one deterministic canonical Parquet dataset."""

    return write_parquet(
        generate_synthetic_events(config, seed=seed),
        output_path,
    )


# Short CLI-friendly spelling.
generate_synthetic = generate_synthetic_events
