from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lobmm.config import (
    AppConfig,
    ConfigError,
    DatasetProvenanceConfig,
    DatasetSourceType,
    dump_config,
    load_config,
)
from lobmm.enums import EventType, Side
from lobmm.events import EventValidationError, MarketEvent
from lobmm.types import floor_price_to_ticks, price_to_ticks, ticks_to_price


def test_side_values_and_trade_semantics_are_explicit() -> None:
    assert int(Side.BID) == 1
    assert int(Side.ASK) == -1
    assert Side.BID.opposite is Side.ASK
    trade = MarketEvent(1, 2, EventType.TRADE, Side.BID, 10_000, 7)
    assert trade.side is Side.BID


def test_reset_has_canonical_zero_fields() -> None:
    event = MarketEvent.reset(timestamp_ns=10, sequence_number=3)
    assert event.as_dict() == {
        "timestamp_ns": 10,
        "sequence_number": 3,
        "event_type": "RESET",
        "side": None,
        "price_ticks": 0,
        "quantity": 0,
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timestamp_ns": -1}, "timestamp_ns"),
        ({"sequence_number": -1}, "sequence_number"),
        ({"side": None}, "valid side"),
        ({"price_ticks": 0}, "positive price_ticks"),
        ({"quantity": 0}, "positive quantity"),
    ],
)
def test_market_event_rejects_invalid_fields(
    kwargs: dict[str, object],
    match: str,
) -> None:
    values: dict[str, object] = {
        "timestamp_ns": 1,
        "sequence_number": 1,
        "event_type": EventType.ADD,
        "side": Side.BID,
        "price_ticks": 100,
        "quantity": 10,
    }
    values.update(kwargs)
    with pytest.raises(EventValidationError, match=match):
        MarketEvent(**values)  # type: ignore[arg-type]


def test_reset_rejects_nonzero_price_or_quantity() -> None:
    with pytest.raises(EventValidationError, match="quantity"):
        MarketEvent(0, 0, EventType.RESET, quantity=1)
    with pytest.raises(EventValidationError, match="price_ticks"):
        MarketEvent(0, 0, EventType.RESET, price_ticks=1)


def test_price_conversion_is_exact_and_tick_safe() -> None:
    assert price_to_ticks("100.01", "0.01") == 10_001
    assert ticks_to_price(10_001, "0.01") == Decimal("100.01")
    assert floor_price_to_ticks("100.019", "0.01") == 10_001
    with pytest.raises(ValueError, match="not aligned"):
        price_to_ticks("100.015", "0.01")
    assert price_to_ticks("100.005", "0.01", strict=False) == 10_000


def test_price_conversion_rejects_nonpositive_tick_size() -> None:
    with pytest.raises(ValueError, match="tick_size"):
        price_to_ticks("100", "0")
    with pytest.raises(ValueError, match="tick_size"):
        ticks_to_price(100, "-0.01")


def test_yaml_config_load_and_json_safe_dump(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "instrument:",
                "  symbol: TEST",
                "  tick_size: '0.25'",
                "data:",
                "  provenance:",
                "    symbol: TEST",
                "synthetic:",
                "  event_count: 25",
                "  levels: 3",
                "random_seed: 19",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.instrument.symbol == "TEST"
    assert config.instrument.tick_size == Decimal("0.25")
    assert config.synthetic.event_count == 25
    assert dump_config(config)["random_seed"] == 19
    assert (
        dump_config(config)["data"]["provenance"]["source_type"]
        == DatasetSourceType.SYNTHETIC
    )


def test_config_rejects_unknown_or_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("instrument:\n  mystery: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(path)
    with pytest.raises(ValueError):
        AppConfig.model_validate({"instrument": {"tick_size": 0}})
    with pytest.raises(ValueError, match="rejection_max_cooldown_ns"):
        AppConfig.model_validate(
            {
                "strategy": {
                    "rejection_cooldown_ns": 2,
                    "rejection_max_cooldown_ns": 1,
                }
            }
        )


@pytest.mark.parametrize(
    "config_name",
    [
        "benchmark.yaml",
        "ci.yaml",
        "fixed_spread.yaml",
        "inventory_aware.yaml",
        "microprice.yaml",
        "synthetic.yaml",
    ],
)
def test_shipped_synthetic_configs_have_explicit_provenance(
    config_name: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "configs" / config_name)

    assert config.data.provenance.source_type is DatasetSourceType.SYNTHETIC
    assert config.data.provenance.synthetic_demonstration is True
    assert config.data.provenance.artifact()["synthetic_demonstration"] is True
    if config_name in {
        "fixed_spread.yaml",
        "inventory_aware.yaml",
        "microprice.yaml",
    }:
        assert config.data.input_path is not None


def test_historical_provenance_requires_a_complete_manifest() -> None:
    valid = AppConfig.model_validate(
        {
            "instrument": {"symbol": "ES"},
            "data": {
                "input_path": "data/processed/es.parquet",
                "provenance": {
                    "source_type": "historical",
                    "provider": "Example Data Provider",
                    "venue": "CME",
                    "symbol": "ES",
                    "dataset_id": "es-2026-01",
                    "session_start": "2026-01-02",
                    "session_end": "2026-01-30",
                    "checksum_sha256": "a" * 64,
                    "license_notes": "Internal research license; redistribution prohibited.",
                },
            },
        }
    )
    assert valid.data.provenance.source_type is DatasetSourceType.HISTORICAL
    assert valid.data.provenance.synthetic_demonstration is False
    assert valid.data.provenance.artifact()["synthetic_demonstration"] is False

    with pytest.raises(ValueError, match=r"data\.input_path"):
        AppConfig.model_validate(
            {
                "data": {
                    "provenance": {
                        **valid.data.provenance.model_dump(mode="json"),
                        "symbol": "SYNTH",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"checksum_sha256": "not-a-checksum"}, "64 hexadecimal"),
        (
            {"session_start": "2026-02-01", "session_end": "2026-01-01"},
            "session_start",
        ),
        ({"checksum_sha256": None}, "checksum_sha256"),
        ({"venue": "SIMULATED"}, "SIMULATED"),
        (
            {"provider": "synthetic generator presented as history"},
            "synthetic generator",
        ),
    ],
)
def test_historical_manifest_rejects_unsubstantiated_provenance(
    overrides: dict[str, object], match: str
) -> None:
    values: dict[str, object] = {
        "source_type": "historical",
        "provider": "Example Provider",
        "venue": "CME",
        "symbol": "ES",
        "dataset_id": "es-session",
        "session_start": "2026-01-01",
        "session_end": "2026-01-02",
        "checksum_sha256": "b" * 64,
        "license_notes": "Licensed for internal research.",
    }
    values.update(overrides)
    with pytest.raises(ValueError, match=match):
        DatasetProvenanceConfig.model_validate(values)


def test_provenance_symbol_must_match_instrument() -> None:
    with pytest.raises(ValueError, match="must match"):
        AppConfig.model_validate(
            {
                "instrument": {"symbol": "ES"},
                "data": {"provenance": {"symbol": "NQ"}},
            }
        )
