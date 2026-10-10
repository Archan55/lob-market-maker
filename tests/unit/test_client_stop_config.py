from pathlib import Path

import pytest

from lobmm.backtest import run_backtest
from lobmm.config import AppConfig, BacktestConfig, dump_config
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent


@pytest.mark.parametrize(
    "fields",
    [
        {"shutdown_policy": "client_stop"},
        {"shutdown_policy": "client_stop", "client_stop_timestamp_ns": 10},
        {"shutdown_policy": "client_stop", "observation_end_timestamp_ns": 20},
        {
            "shutdown_policy": "client_stop",
            "client_stop_timestamp_ns": 20,
            "observation_end_timestamp_ns": 10,
        },
        {
            "shutdown_policy": "client_stop",
            "client_stop_timestamp_ns": 10,
            "observation_end_timestamp_ns": 20,
            "session_end_policy": "liquidate",
        },
        {"client_stop_timestamp_ns": 10},
        {"observation_end_timestamp_ns": 20},
    ],
)
def test_invalid_shutdown_contract_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BacktestConfig.model_validate(fields)


@pytest.mark.parametrize("stop", [0, 21])
def test_stop_must_lie_within_selected_tape(stop: int, tmp_path: Path) -> None:
    raw = dump_config(AppConfig())
    raw["backtest"].update(
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=stop,
        observation_end_timestamp_ns=30,
    )
    events = [
        MarketEvent(10, 1, EventType.ADD, Side.BID, 99, 2),
        MarketEvent(20, 2, EventType.ADD, Side.ASK, 101, 2),
    ]
    with pytest.raises(ValueError, match="within the selected tape"):
        run_backtest(
            AppConfig.model_validate(raw),
            events,
            run_name="invalid",
            output_root=tmp_path,
        )
