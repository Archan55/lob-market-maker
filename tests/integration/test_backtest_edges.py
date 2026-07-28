from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig
from lobmm.enums import EventType, MarkPrice, SessionEndPolicy, Side, ValidationMode
from lobmm.events import MarketEvent
from lobmm.validation import StreamValidationError, validate_event_stream


def _config(
    tmp_path: Path,
    *,
    session_end_policy: SessionEndPolicy = SessionEndPolicy.MARK,
    max_order_size: int = 25,
    order_size: int = 1,
    write_plots: bool = False,
) -> AppConfig:
    base = AppConfig()
    return base.model_copy(
        update={
            "latency": base.latency.model_copy(
                update={
                    "market_data_ns": 0,
                    "order_entry_ns": 0,
                    "cancellation_ns": 0,
                    "fill_report_ns": 0,
                }
            ),
            "strategy": base.strategy.model_copy(
                update={
                    "order_size": order_size,
                    "minimum_quote_lifetime_ns": 0,
                    "refresh_interval_ns": 1_000_000_000,
                    "stale_after_ns": 2_000_000_000,
                }
            ),
            "risk": base.risk.model_copy(update={"max_order_size": max_order_size}),
            "backtest": base.backtest.model_copy(
                update={
                    "warmup_events": 0,
                    "timer_interval_ns": 1_000_000_000,
                    "session_end_policy": session_end_policy,
                }
            ),
            "output": base.output.model_copy(
                update={
                    "runs_directory": tmp_path,
                    "write_plots": write_plots,
                }
            ),
        }
    )


def _event(
    timestamp_ns: int,
    sequence_number: int,
    event_type: EventType,
    side: Side | None = None,
    price_ticks: int = 0,
    quantity: int = 0,
) -> MarketEvent:
    return MarketEvent(
        timestamp_ns,
        sequence_number,
        event_type,
        side,
        price_ticks,
        quantity,
    )


@pytest.mark.parametrize("run_name", ["", ".", "..", "nested/run", "nested\\run"])
def test_backtest_rejects_unsafe_run_names(
    tmp_path: Path,
    run_name: str,
) -> None:
    with pytest.raises(ValueError, match="run_name"):
        run_backtest(
            _config(tmp_path),
            [_event(0, 0, EventType.ADD, Side.BID, 100, 1)],
            run_name=run_name,
            generate_plots=False,
        )


def test_backtest_rejects_an_empty_selected_interval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(update={"start_timestamp_ns": 100})
        }
    )
    with pytest.raises(ValueError, match="no market events"):
        run_backtest(
            config,
            [_event(0, 0, EventType.ADD, Side.BID, 100, 1)],
            run_name="filtered",
            generate_plots=False,
        )


def test_backtest_rejects_out_of_order_input_before_filtering(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(update={"start_timestamp_ns": 1})
        }
    )
    events = [
        _event(1, 1, EventType.ADD, Side.BID, 100, 1),
        _event(0, 0, EventType.ADD, Side.BID, 99, 1),
    ]

    with pytest.raises(StreamValidationError, match="timestamp_regression"):
        run_backtest(
            config,
            events,
            run_name="out-of-order",
            generate_plots=False,
        )


def test_backtest_rejects_validation_certificate_from_another_stream(
    tmp_path: Path,
) -> None:
    certified = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 100, 1),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 1),
    ]
    different = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 99, 1),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 1),
    ]

    with pytest.raises(ValueError, match="validation certificate"):
        run_backtest(
            _config(tmp_path),
            different,
            run_name="wrong-certificate",
            generate_plots=False,
            input_validation=validate_event_stream(certified),
        )


def test_one_sided_run_writes_final_snapshot_and_uses_configured_plot_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated: list[Path] = []
    monkeypatch.setattr(
        "lobmm.report.generate_report",
        lambda path: generated.append(Path(path)),
    )
    result = run_backtest(
        _config(tmp_path, write_plots=True),
        [_event(0, 0, EventType.ADD, Side.BID, 100, 1)],
        run_name="one-sided",
    )
    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    assert pnl.height == 1
    assert pnl["net_pnl"].to_list() == [0.0]
    assert generated == [result.run_directory]


def test_pretrade_risk_rejections_are_audited_without_exchange_orders(
    tmp_path: Path,
) -> None:
    events = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 100, 10),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 10),
        _event(10, 3, EventType.ADD, Side.BID, 99, 1),
    ]
    config = _config(tmp_path, max_order_size=1, order_size=2)
    # A positive report delay also verifies that repeated risk-reducing
    # decisions remain causally bounded within the finite session.
    config = config.model_copy(
        update={"latency": config.latency.model_copy(update={"fill_report_ns": 1})}
    )
    result = run_backtest(
        config,
        events,
        run_name="risk-rejection",
        generate_plots=False,
    )
    risk_events = pl.read_parquet(result.run_directory / "risk_events.parquet")
    assert risk_events.height == 2
    assert set(risk_events["reason"].to_list()) == {"max_order_size"}
    assert pl.read_parquet(result.run_directory / "orders.parquet").is_empty()
    diagnostics = result.diagnostics
    assert diagnostics["desired_quote_attempts"] == 2
    assert diagnostics["risk_blocked_quote_attempts"] == 2
    assert diagnostics["rejected_quote_attempts"] == 2
    assert diagnostics["submitted_order_messages"] == 0
    assert diagnostics["retry_suppressed_quote_attempts"] > 0


def test_risk_quote_age_is_enforced_independently_of_strategy_refresh(
    tmp_path: Path,
) -> None:
    events = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 100, 10),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 10),
        _event(20, 3, EventType.ADD, Side.BID, 99, 1),
    ]
    base = _config(tmp_path)
    config = base.model_copy(
        update={
            "risk": base.risk.model_copy(update={"max_quote_age_ns": 5}),
        }
    )

    result = run_backtest(
        config,
        events,
        run_name="quote-age",
        generate_plots=False,
    )

    risk_events = pl.read_parquet(result.run_directory / "risk_events.parquet")
    quote_age_events = risk_events.filter(pl.col("reason") == "quote_age")
    assert quote_age_events.height >= 2
    assert result.diagnostics["risk_cancel_request_messages"] == (
        quote_age_events.height
    )


def test_configured_microprice_mark_is_used_by_backtest(tmp_path: Path) -> None:
    events = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 100, 10),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 30),
    ]
    base = _config(tmp_path)
    config = base.model_copy(
        update={
            "backtest": base.backtest.model_copy(
                update={"mark_price": MarkPrice.MICROPRICE}
            )
        }
    )

    result = run_backtest(
        config,
        events,
        run_name="microprice-mark",
        generate_plots=False,
    )

    pnl = pl.read_parquet(result.run_directory / "pnl.parquet")
    assert pnl["mark_ticks"].tail(1).item() == 100.5


def test_configured_lenient_data_validation_is_recorded(tmp_path: Path) -> None:
    events = [
        _event(0, 0, EventType.SNAPSHOT, Side.BID, 100, 5),
        _event(0, 1, EventType.SNAPSHOT, Side.ASK, 102, 5),
        _event(1, 2, EventType.CANCEL, Side.BID, 100, 10),
    ]
    base = _config(tmp_path)
    config = base.model_copy(
        update={
            "data": base.data.model_copy(
                update={"validation_mode": ValidationMode.LENIENT}
            )
        }
    )

    result = run_backtest(
        config,
        events,
        run_name="lenient-input",
        generate_plots=False,
    )

    assert result.diagnostics["input_validation_issue_count"] == 1
    assert result.diagnostics["input_validation_diagnostics"]["book_event_clamped"] == 1
    certificate = result.diagnostics["input_validation_certificate"]
    assert certificate["event_count"] == len(events)
    assert certificate["mode"] == "lenient"
    assert certificate["reconstructed_book"] is True
    assert len(certificate["event_stream_sha256"]) == 64


@pytest.mark.parametrize("filled_side", [Side.BID, Side.ASK])
def test_session_liquidation_flattens_long_and_short_inventory(
    tmp_path: Path,
    filled_side: Side,
) -> None:
    price = 100 if filled_side is Side.BID else 102
    events = [
        _event(0, 0, EventType.RESET),
        _event(0, 1, EventType.SNAPSHOT, Side.BID, 100, 10),
        _event(0, 2, EventType.SNAPSHOT, Side.ASK, 102, 10),
        _event(5, 3, EventType.ADD, filled_side, price, 5),
        _event(10, 4, EventType.TRADE, filled_side, price, 11),
    ]
    result = run_backtest(
        _config(tmp_path, session_end_policy=SessionEndPolicy.LIQUIDATE),
        events,
        run_name=f"liquidate-{filled_side.name.lower()}",
        generate_plots=False,
    )
    fills = pl.read_parquet(result.run_directory / "fills.parquet")
    assert fills.height == 2
    assert set(fills["strategy_id"].to_list()) == {
        "market-maker",
        "session-policy",
    }
    assert result.metrics["inventory"]["end_of_session"] == 0
