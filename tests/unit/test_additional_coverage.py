from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import (
    TimerMessage,
    _dataclass_row,
    _latency,
    _select_events,
    _serializable,
)
from lobmm.book import BookError, CrossedBookError, L2Book
from lobmm.channels import FixedLatency, UniformJitterLatency
from lobmm.config import (
    AppConfig,
    BacktestConfig,
    ConfigError,
    FeeConfig,
    MetricsConfig,
    RiskConfig,
    StrategyConfig,
    SyntheticConfig,
    load_config,
)
from lobmm.data.loaders import (
    DataLoadError,
    load_events,
    read_csv,
    read_parquet,
    write_csv,
    write_parquet,
)
from lobmm.data.schema import (
    CANONICAL_COLUMNS,
    DataSchemaError,
    coerce_canonical_frame,
    empty_event_frame,
    event_from_mapping,
    events_to_frame,
)
from lobmm.enums import EventType, OrderStatus, ReportType, Side, StrategyName
from lobmm.events import BookView, EventValidationError, MarketEvent
from lobmm.metrics import build_metrics, classify_regimes, compute_markouts
from lobmm.orders import ExecutionReport
from lobmm.strategies import make_strategy
from lobmm.strategies.base import (
    DesiredQuote,
    ManagedQuote,
    MarketMakingStrategy,
)
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.synthetic import (
    SyntheticGenerationError,
    generate_synthetic_events,
    regime_for_sequence,
)
from lobmm.types import floor_price_to_ticks
from lobmm.validation import (
    StreamValidationError,
    validate_event_stream,
    validate_frame,
)


def _view(
    *,
    sequence: int = 1,
    midpoint: float | None = 100.5,
    microprice: float | None = 100.75,
    imbalance: float | None = 0.25,
) -> BookView:
    return BookView(
        timestamp_ns=10,
        sequence_number=sequence,
        best_bid_ticks=100,
        best_ask_ticks=101,
        best_bid_quantity=20,
        best_ask_quantity=10,
        midpoint_ticks=midpoint,
        microprice_ticks=microprice,
        imbalance=imbalance,
        bids=((100, 20),),
        asks=((101, 10),),
    )


class ControlledStrategy(MarketMakingStrategy):
    def __init__(
        self,
        config: StrategyConfig,
        targets: tuple[DesiredQuote, ...],
    ) -> None:
        super().__init__(config)
        self.targets = targets

    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        del view, known_inventory, max_abs_inventory
        return self.targets


def _canonical_row() -> dict[str, object]:
    return {
        "timestamp_ns": 1,
        "sequence_number": 2,
        "event_type": EventType.ADD,
        "side": Side.BID,
        "price_ticks": 100,
        "quantity": 5,
    }


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: FeeConfig(maker_fee_per_unit=Decimal("-0.01")), "nonnegative"),
        (lambda: RiskConfig(max_loss=Decimal("0")), "positive"),
        (
            lambda: BacktestConfig(start_timestamp_ns=2, end_timestamp_ns=1),
            "must not be after",
        ),
        (lambda: MetricsConfig(markout_horizons_ns=()), "cannot be empty"),
        (
            lambda: MetricsConfig(markout_horizons_ns=(1, 0)),
            "must be positive",
        ),
    ],
)
def test_config_cross_field_invariants_fail_early(
    factory: object,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        factory()  # type: ignore[operator]


def test_config_loader_handles_missing_empty_and_non_mapping_yaml(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_config(empty) == AppConfig()

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_config(scalar)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("instrument: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(malformed)


def test_canonical_schema_empty_typed_and_mapping_paths() -> None:
    frame = empty_event_frame()
    assert frame.is_empty()
    assert tuple(frame.columns) == CANONICAL_COLUMNS
    assert events_to_frame([]).schema == frame.schema

    typed = event_from_mapping(_canonical_row())
    assert typed.event_type is EventType.ADD
    assert typed.side is Side.BID

    missing = _canonical_row()
    missing.pop("quantity")
    with pytest.raises(DataSchemaError, match="missing field 'quantity'"):
        event_from_mapping(missing, row_index=7)

    invalid = _canonical_row()
    invalid["quantity"] = 0
    with pytest.raises(DataSchemaError, match="positive quantity"):
        event_from_mapping(invalid)


def test_schema_and_loaders_wrap_cast_read_decode_and_write_failures(
    tmp_path: Path,
) -> None:
    malformed = pl.DataFrame(
        {
            "timestamp_ns": [0],
            "sequence_number": [0],
            "event_type": ["ADD"],
            "side": [1],
            "price_ticks": ["not-an-integer"],
            "quantity": [1],
        }
    )
    with pytest.raises(DataSchemaError, match="cannot cast"):
        coerce_canonical_frame(malformed)

    with pytest.raises(DataLoadError, match="canonical CSV"):
        read_csv(tmp_path / "missing.csv")
    with pytest.raises(DataLoadError, match="canonical Parquet"):
        read_parquet(tmp_path / "missing.parquet")

    invalid_enum = tmp_path / "invalid.csv"
    pl.DataFrame(
        {
            "timestamp_ns": [0],
            "sequence_number": [0],
            "event_type": ["MODIFY"],
            "side": [1],
            "price_ticks": [100],
            "quantity": [1],
        }
    ).write_csv(invalid_enum)
    with pytest.raises(DataLoadError, match="cannot decode"):
        load_events(invalid_enum)

    missing_columns = pl.DataFrame({"timestamp_ns": [0]})
    with pytest.raises(DataLoadError, match="write canonical Parquet"):
        write_parquet(missing_columns, tmp_path / "bad.parquet")
    with pytest.raises(DataLoadError, match="write canonical CSV"):
        write_csv(missing_columns, tmp_path / "bad.csv")


def test_book_defensive_queries_and_integrity_checks() -> None:
    one_sided = L2Book()
    one_sided.apply(MarketEvent(0, 0, EventType.ADD, Side.BID, 100, 5))
    assert one_sided.weighted_imbalance() is None
    assert one_sided != object()
    with pytest.raises(ValueError, match="positive"):
        one_sided.consume(Side.BID, 100, 0)
    with pytest.raises(BookError, match="invalid side"):
        one_sided.quantity_at(0, 100)  # type: ignore[arg-type]

    invalid_bid = L2Book()
    invalid_bid._bids[100] = 0  # type: ignore[attr-defined]
    with pytest.raises(BookError, match="bid quantities"):
        invalid_bid.assert_valid()

    invalid_ask = L2Book()
    invalid_ask._asks[101] = -1  # type: ignore[attr-defined]
    with pytest.raises(BookError, match="ask quantities"):
        invalid_ask.assert_valid()

    crossed = L2Book()
    crossed._bids[101] = 1  # type: ignore[attr-defined]
    crossed._asks[101] = 1  # type: ignore[attr-defined]
    with pytest.raises(CrossedBookError, match="crossed book"):
        crossed.assert_valid()


def test_event_and_price_defensive_type_boundaries() -> None:
    with pytest.raises(EventValidationError, match="EventType"):
        MarketEvent(0, 0, "ADD", Side.BID, 100, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tick_size"):
        floor_price_to_ticks("100", "0")


def test_quote_value_helpers_and_factory_defensive_branch() -> None:
    with pytest.raises(ValueError, match="positive"):
        DesiredQuote(Side.BID, 0, 1)
    with pytest.raises(ValueError, match="positive"):
        DesiredQuote(Side.ASK, 101, 0)

    assert MarketMakingStrategy.rounded_quotes(1.0, 0.0) == (1, 2)
    assert MarketMakingStrategy.inventory_scaled_quantity(8, 7, 0) == 8
    assert MarketMakingStrategy.inventory_scaled_quantity(8, 100, 100) == 2

    invalid = StrategyConfig().model_copy(update={"name": "unsupported"})
    with pytest.raises(ValueError, match="unsupported strategy"):
        make_strategy(invalid)


@pytest.mark.parametrize(
    "name",
    [
        StrategyName.FIXED_SPREAD,
        StrategyName.INVENTORY_AWARE,
        StrategyName.MICROPRICE,
    ],
)
def test_each_strategy_suppresses_the_risk_increasing_side_at_limits(
    name: StrategyName,
) -> None:
    strategy = make_strategy(StrategyConfig(name=name))
    long_quotes = strategy.desired_quotes(
        view=_view(),
        known_inventory=10,
        max_abs_inventory=10,
    )
    short_quotes = strategy.desired_quotes(
        view=_view(),
        known_inventory=-10,
        max_abs_inventory=10,
    )
    assert {quote.side for quote in long_quotes} == {Side.ASK}
    assert {quote.side for quote in short_quotes} == {Side.BID}


def test_inventory_and_microprice_strategies_handle_zero_limit_and_missing_signal() -> (
    None
):
    inventory = make_strategy(StrategyConfig(name=StrategyName.INVENTORY_AWARE))
    inventory_quotes = inventory.desired_quotes(
        view=_view(),
        known_inventory=0,
        max_abs_inventory=0,
    )
    assert inventory_quotes == ()

    microprice = make_strategy(StrategyConfig(name=StrategyName.MICROPRICE))
    fallback_quotes = microprice.desired_quotes(
        view=_view(microprice=None, imbalance=None),
        known_inventory=0,
        max_abs_inventory=0,
    )
    assert fallback_quotes == ()


def test_strategy_rejects_backward_inputs_and_report_before_exchange() -> None:
    strategy = ControlledStrategy(
        StrategyConfig(),
        (DesiredQuote(Side.BID, 99, 1),),
    )
    strategy.on_market_data(_view(sequence=2))
    with pytest.raises(ValueError, match="sequence moved backward"):
        strategy.on_market_data(_view(sequence=1))

    report = ExecutionReport(
        report_id="early",
        report_type=ReportType.REJECTED,
        exchange_timestamp_ns=10,
        order_id="",
        client_order_id="unknown",
        order_status=OrderStatus.REJECTED,
        remaining_quantity=1,
    )
    with pytest.raises(ValueError, match="cannot precede"):
        strategy.on_execution_report(report, notification_timestamp_ns=9)
    strategy.on_execution_report(report, notification_timestamp_ns=10)
    assert strategy.report_sequence == 0


def test_quote_lifecycle_uses_exchange_id_and_avoids_duplicate_cancel() -> None:
    strategy = ControlledStrategy(
        StrategyConfig(
            minimum_quote_lifetime_ns=10,
            stale_after_ns=100,
            refresh_interval_ns=50,
            price_change_threshold_ticks=1,
        ),
        (
            DesiredQuote(Side.BID, 99, 2),
            DesiredQuote(Side.ASK, 102, 2),
        ),
    )
    strategy.on_market_data(_view())
    submissions = strategy.decide(timestamp_ns=100, max_abs_inventory=10)
    assert len(submissions) == 2
    assert len(strategy.active_quotes) == 2
    bid = next(action for action in submissions if action.side is Side.BID)

    strategy.on_execution_report(
        ExecutionReport(
            report_id="accepted",
            report_type=ReportType.ACCEPTED,
            exchange_timestamp_ns=101,
            order_id="O-BID",
            client_order_id=bid.client_order_id,
            order_status=OrderStatus.LIVE,
            remaining_quantity=2,
        ),
        notification_timestamp_ns=102,
    )
    strategy.targets = (
        DesiredQuote(Side.BID, 98, 2),
        DesiredQuote(Side.ASK, 102, 2),
    )
    assert strategy.decide(timestamp_ns=105, max_abs_inventory=10) == ()
    cancels = strategy.decide(timestamp_ns=110, max_abs_inventory=10)
    assert len(cancels) == 1
    assert cancels[0].order_id == "O-BID"  # type: ignore[attr-defined]
    assert strategy.decide(timestamp_ns=111, max_abs_inventory=10) == ()

    strategy.on_execution_report(
        ExecutionReport(
            report_id="cancelled",
            report_type=ReportType.CANCELLED,
            exchange_timestamp_ns=112,
            order_id="O-BID",
            client_order_id=bid.client_order_id,
            order_status=OrderStatus.CANCELLED,
            remaining_quantity=2,
        ),
        notification_timestamp_ns=113,
    )
    assert {quote.side for quote in strategy.active_quotes} == {Side.ASK}


def test_quote_management_side_suppression_cancel_all_and_replacement_reasons() -> None:
    config = StrategyConfig(
        minimum_quote_lifetime_ns=10,
        stale_after_ns=100,
        refresh_interval_ns=50,
        price_change_threshold_ticks=2,
        quantity_change_threshold=2,
    )
    strategy = ControlledStrategy(
        config,
        (
            DesiredQuote(Side.BID, 99, 5),
            DesiredQuote(Side.ASK, 102, 5),
        ),
    )
    strategy.on_market_data(_view())
    strategy.decide(timestamp_ns=100, max_abs_inventory=10)
    strategy.targets = (DesiredQuote(Side.ASK, 102, 5),)
    suppressed = strategy.decide(timestamp_ns=101, max_abs_inventory=10)
    assert len(suppressed) == 1
    assert strategy.decisions[-1].reason == "risk_side_suppression"

    all_cancelled = strategy.cancel_all(timestamp_ns=102)
    assert len(all_cancelled) == 1
    assert strategy.cancel_all(timestamp_ns=103) == ()

    managed = ManagedQuote(
        side=Side.BID,
        client_order_id="managed",
        price_ticks=100,
        original_quantity=5,
        remaining_quantity=5,
        sent_timestamp_ns=0,
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 100, 5), 100
        )
        == "stale"
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 98, 5), 5
        )
        is None
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 98, 5), 10
        )
        == "price_change"
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 100, 3), 10
        )
        == "quantity_change"
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 100, 5), 50
        )
        == "refresh"
    )
    assert (
        strategy._replacement_reason(  # type: ignore[attr-defined]
            managed, DesiredQuote(Side.BID, 100, 5), 20
        )
        is None
    )


def test_runtime_validates_latency_sequence_and_exposes_delayed_state() -> None:
    strategy = ControlledStrategy(
        StrategyConfig(),
        (DesiredQuote(Side.BID, 99, 1), DesiredQuote(Side.ASK, 102, 1)),
    )
    with pytest.raises(ValueError, match="max_abs_inventory"):
        StrategyRuntime(strategy=strategy, max_abs_inventory=0)
    with pytest.raises(ValueError, match="depth"):
        StrategyRuntime(strategy=strategy, max_abs_inventory=10, depth=0)

    runtime = StrategyRuntime(strategy=strategy, max_abs_inventory=10)
    event = MarketEvent(10, 2, EventType.ADD, Side.BID, 100, 5)
    with pytest.raises(ValueError, match="before exchange"):
        runtime.deliver_market(event, notification_timestamp_ns=9)
    runtime.deliver_market(event, notification_timestamp_ns=10)
    assert runtime.observed_sequence == 2
    assert runtime.latest_view is strategy.latest_view
    assert runtime.known_inventory == 0
    with pytest.raises(ValueError, match="sequence moved backward"):
        runtime.deliver_market(
            MarketEvent(11, 1, EventType.ADD, Side.BID, 99, 1),
            notification_timestamp_ns=11,
        )
    assert len(runtime.decide(timestamp_ns=12)) == 0


def test_strategy_decide_negative_time_and_suppressed_quotes() -> None:
    strategy = ControlledStrategy(
        StrategyConfig(),
        (DesiredQuote(Side.BID, 99, 1),),
    )
    with pytest.raises(ValueError, match="nonnegative"):
        strategy.decide(timestamp_ns=-1, max_abs_inventory=10)
    assert strategy.recent_volatility_ticks == 0.0
    assert strategy.decide(timestamp_ns=0, max_abs_inventory=10) == ()


def test_backtest_helpers_filter_latency_and_serialize_without_loss(
    tmp_path: Path,
) -> None:
    events = [
        MarketEvent(1, 1, EventType.ADD, Side.BID, 97, 1),
        MarketEvent(2, 2, EventType.ADD, Side.BID, 98, 1),
        MarketEvent(3, 3, EventType.ADD, Side.BID, 99, 1),
    ]
    base = AppConfig()
    filtered = base.model_copy(
        update={
            "backtest": base.backtest.model_copy(
                update={
                    "start_timestamp_ns": 2,
                    "end_timestamp_ns": 3,
                    "event_limit": 1,
                }
            )
        }
    )
    assert [event.timestamp_ns for event in _select_events(filtered, events)] == [2]
    assert [event.timestamp_ns for event in _select_events(base, events)] == [1, 2, 3]

    assert isinstance(_latency(5, 0, 1), FixedLatency)
    assert isinstance(_latency(5, 2, 1), UniformJitterLatency)
    assert _dataclass_row(TimerMessage(7)) == {"timer_sequence": 7}
    assert _serializable(
        {
            Side.BID: (
                Decimal("1.25"),
                tmp_path,
                TimerMessage(3),
            )
        }
    ) == {"1": [1.25, str(tmp_path), {"timer_sequence": 3}]}
    with pytest.raises(TypeError, match="expected dataclass"):
        _dataclass_row("not-a-dataclass")


def test_validation_synthetic_and_metrics_edge_contracts() -> None:
    with pytest.raises(TypeError, match="ValidationMode"):
        validate_event_stream([], mode="strict")  # type: ignore[arg-type]
    result = validate_event_stream(
        [MarketEvent(0, 0, EventType.ADD, Side.BID, 100, 1)],
        reconstruct_book=False,
    )
    assert result.valid
    assert result.final_book is None
    assert result.issue_count == 0

    crossed = events_to_frame(
        [
            MarketEvent(0, 0, EventType.ADD, Side.BID, 100, 1),
            MarketEvent(1, 1, EventType.ADD, Side.ASK, 100, 1),
        ]
    )
    with pytest.raises(StreamValidationError, match="invalid_book_transition"):
        validate_frame(crossed)
    with pytest.raises(StreamValidationError, match="invalid_schema"):
        validate_frame(pl.DataFrame({"bad": [1]}))

    config = SyntheticConfig(event_count=10, levels=2)
    with pytest.raises(ValueError, match="nonnegative"):
        regime_for_sequence(-1, config)
    with pytest.raises(SyntheticGenerationError, match="seed"):
        generate_synthetic_events(config, seed=-1)
    with pytest.raises(SyntheticGenerationError, match="too small"):
        generate_synthetic_events(
            SyntheticConfig(
                event_count=101,
                levels=50,
                start_price_ticks=11,
            )
        )
    with pytest.raises(TypeError, match="config"):
        generate_synthetic_events(object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="horizons"):
        compute_markouts([], [], [0], tick_size=0.01)
    states: list[dict[str, object]] = []
    classify_regimes(states)
    assert states == []

    metrics = build_metrics(
        orders=[{"order_id": "fallback", "previous_status": "live"}],
        fills=[
            {
                "fill_id": "F",
                "side": 1,
                "quantity": 1,
                "price_ticks": 100,
                "exchange_fill_timestamp_ns": 0,
                "liquidity_role": "maker",
            }
        ],
        inventory=[],
        pnl=[],
        market_states=[],
        markouts=[],
        risk_events=[],
        quote_messages=[],
        diagnostics={},
        tick_size=0.01,
    )
    assert metrics["trading_activity"]["submitted_orders"] == 1
    assert metrics["execution_quality"]["realized_spread_ticks"] is None
