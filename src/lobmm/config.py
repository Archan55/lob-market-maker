"""Validated YAML configuration."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lobmm.enums import (
    MarkPrice,
    QueueAllocation,
    SessionEndPolicy,
    ShutdownPolicy,
    StrategyName,
    ValidationMode,
)


class ConfigError(ValueError):
    """Raised when a YAML configuration cannot be loaded."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentConfig(StrictModel):
    symbol: str = "SYNTH"
    currency: str = "USD"
    tick_size: Decimal = Decimal("0.01")
    lot_size: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def validate_tick(self) -> InstrumentConfig:
        if self.tick_size <= 0:
            raise ValueError("instrument.tick_size must be positive")
        return self


class DatasetSourceType(StrEnum):
    """Research interpretation of the configured market-event source."""

    SYNTHETIC = "synthetic"
    HISTORICAL = "historical"


class DatasetProvenanceConfig(StrictModel):
    """Auditable manifest for the event dataset used by a run.

    Synthetic Parquet is still synthetic: storage format and ``input_path`` do
    not determine research provenance.
    """

    source_type: DatasetSourceType = DatasetSourceType.SYNTHETIC
    provider: str = "lobmm deterministic synthetic generator"
    venue: str = "SIMULATED"
    symbol: str = "SYNTH"
    dataset_id: str = "lobmm-synthetic-v1"
    session_start: date | None = None
    session_end: date | None = None
    checksum_sha256: str | None = None
    license_notes: str = (
        "Generated locally by lobmm; not licensed or historical market data."
    )

    @field_validator("provider", "venue", "symbol", "dataset_id", "license_notes")
    @classmethod
    def validate_nonblank_metadata(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("dataset provenance text fields cannot be blank")
        return normalized

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("checksum_sha256 must contain 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def validate_manifest(self) -> DatasetProvenanceConfig:
        if (
            self.session_start is not None
            and self.session_end is not None
            and self.session_start > self.session_end
        ):
            raise ValueError("dataset session_start must not follow session_end")
        if self.source_type is DatasetSourceType.HISTORICAL:
            if self.session_start is None or self.session_end is None:
                raise ValueError(
                    "historical dataset provenance requires a session date range"
                )
            if self.checksum_sha256 is None:
                raise ValueError(
                    "historical dataset provenance requires checksum_sha256"
                )
            if self.venue.upper() == "SIMULATED":
                raise ValueError("historical dataset venue cannot be SIMULATED")
            if "synthetic" in self.provider.casefold():
                raise ValueError(
                    "historical dataset provider cannot identify a synthetic generator"
                )
        return self

    @property
    def synthetic_demonstration(self) -> bool:
        return self.source_type is DatasetSourceType.SYNTHETIC

    def artifact(self) -> dict[str, Any]:
        """Return the JSON-safe manifest embedded in run artifacts."""

        result = self.model_dump(mode="json")
        result["synthetic_demonstration"] = self.synthetic_demonstration
        return result


class DataConfig(StrictModel):
    input_path: Path | None = None
    validation_mode: ValidationMode = ValidationMode.STRICT
    book_depth: int = Field(default=5, ge=1, le=100)
    provenance: DatasetProvenanceConfig = DatasetProvenanceConfig()


class SyntheticConfig(StrictModel):
    event_count: int = Field(default=2_000, ge=10)
    start_timestamp_ns: int = Field(default=0, ge=0)
    interval_ns: int = Field(default=1_000_000, gt=0)
    start_price_ticks: int = Field(default=10_000, gt=10)
    levels: int = Field(default=5, ge=2, le=50)
    base_quantity: int = Field(default=100, gt=0)
    regime_length: int = Field(default=250, gt=10)
    reset_interval: int = Field(default=400, ge=20)


class LatencyConfig(StrictModel):
    market_data_ns: int = Field(default=100_000, ge=0)
    order_entry_ns: int = Field(default=150_000, ge=0)
    cancellation_ns: int = Field(default=150_000, ge=0)
    fill_report_ns: int = Field(default=100_000, ge=0)
    market_data_jitter_ns: int = Field(default=0, ge=0)
    order_entry_jitter_ns: int = Field(default=0, ge=0)
    cancellation_jitter_ns: int = Field(default=0, ge=0)
    fill_report_jitter_ns: int = Field(default=0, ge=0)


class ExchangeConfig(StrictModel):
    validation_mode: ValidationMode = ValidationMode.LENIENT
    rest_unfilled_marketable_quantity: bool = True
    cancel_orders_on_reset: bool = True


class QueueModelConfig(StrictModel):
    cancellation_allocation: QueueAllocation = QueueAllocation.BACK_OF_QUEUE
    price_through_fills: bool = True


class FeeConfig(StrictModel):
    maker_fee_per_unit: Decimal = Decimal("0")
    maker_rebate_per_unit: Decimal = Decimal("0.0002")
    taker_fee_per_unit: Decimal = Decimal("0.0003")
    proportional_fee_rate: Decimal = Decimal("0")

    @model_validator(mode="after")
    def validate_nonnegative(self) -> FeeConfig:
        for name, value in (
            ("maker_fee_per_unit", self.maker_fee_per_unit),
            ("maker_rebate_per_unit", self.maker_rebate_per_unit),
            ("taker_fee_per_unit", self.taker_fee_per_unit),
            ("proportional_fee_rate", self.proportional_fee_rate),
        ):
            if value < 0:
                raise ValueError(f"fees.{name} must be nonnegative")
        return self


class StrategyConfig(StrictModel):
    name: StrategyName = StrategyName.FIXED_SPREAD
    strategy_id: str = "market-maker"
    order_size: int = Field(default=10, gt=0)
    base_half_spread_ticks: float = Field(default=1.0, ge=0.5)
    minimum_half_spread_ticks: float = Field(default=1.0, ge=0.5)
    inventory_penalty_ticks: float = Field(default=2.0, ge=0.0)
    imbalance_coefficient_ticks: float = 1.0
    volatility_multiplier: float = Field(default=1.0, ge=0.0)
    volatility_window: int = Field(default=50, ge=2)
    refresh_interval_ns: int = Field(default=50_000_000, gt=0)
    minimum_quote_lifetime_ns: int = Field(default=5_000_000, ge=0)
    stale_after_ns: int = Field(default=250_000_000, gt=0)
    price_change_threshold_ticks: int = Field(default=1, ge=0)
    quantity_change_threshold: int = Field(default=1, ge=0)
    rejection_cooldown_ns: int = Field(default=50_000_000, ge=0)
    rejection_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    rejection_max_cooldown_ns: int = Field(default=1_000_000_000, ge=0)
    message_rate_cooldown_ns: int = Field(default=1_000_000_000, gt=0)
    post_only: bool = True

    @model_validator(mode="after")
    def validate_rejection_backoff(self) -> StrategyConfig:
        if self.rejection_max_cooldown_ns < self.rejection_cooldown_ns:
            raise ValueError(
                "strategy.rejection_max_cooldown_ns must be at least "
                "strategy.rejection_cooldown_ns"
            )
        return self


class RiskConfig(StrictModel):
    max_abs_inventory: int = Field(default=100, gt=0)
    max_order_size: int = Field(default=25, gt=0)
    max_total_open_quantity: int = Field(default=100, gt=0)
    max_open_orders: int = Field(default=4, gt=0)
    max_loss: Decimal = Decimal("1000")
    max_drawdown: Decimal = Decimal("1000")
    max_quote_age_ns: int = Field(default=500_000_000, gt=0)
    minimum_spread_ticks: int = Field(default=1, ge=0)
    max_volatility_ticks: float | None = Field(default=None, gt=0)
    max_messages_per_second: int | None = Field(default=None, gt=0)
    prohibit_new_quotes_last_ns: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_losses(self) -> RiskConfig:
        if self.max_loss <= 0 or self.max_drawdown <= 0:
            raise ValueError("risk loss and drawdown limits must be positive")
        return self


class BacktestConfig(StrictModel):
    warmup_events: int = Field(default=50, ge=0)
    start_timestamp_ns: int | None = Field(default=None, ge=0)
    end_timestamp_ns: int | None = Field(default=None, ge=0)
    event_limit: int | None = Field(default=None, gt=0)
    timer_interval_ns: int = Field(default=50_000_000, gt=0)
    mark_price: MarkPrice = MarkPrice.MIDPOINT
    session_end_policy: SessionEndPolicy = SessionEndPolicy.MARK
    shutdown_policy: ShutdownPolicy = ShutdownPolicy.FORCED_EXPIRY
    client_stop_timestamp_ns: int | None = Field(default=None, ge=0)
    observation_end_timestamp_ns: int | None = Field(default=None, ge=0)
    track_memory: bool = False

    @model_validator(mode="after")
    def validate_interval(self) -> BacktestConfig:
        if (
            self.start_timestamp_ns is not None
            and self.end_timestamp_ns is not None
            and self.start_timestamp_ns > self.end_timestamp_ns
        ):
            raise ValueError("backtest start must not be after end")
        if self.shutdown_policy is ShutdownPolicy.CLIENT_STOP:
            if (
                self.client_stop_timestamp_ns is None
                or self.observation_end_timestamp_ns is None
            ):
                raise ValueError(
                    "client_stop requires explicit stop and observation end timestamps"
                )
            if self.observation_end_timestamp_ns < self.client_stop_timestamp_ns:
                raise ValueError("observation end must not precede client stop")
            if self.session_end_policy is not SessionEndPolicy.MARK:
                raise ValueError(
                    "client_stop supports mark accounting only; liquidation needs a separate execution policy"
                )
        elif (
            self.client_stop_timestamp_ns is not None
            or self.observation_end_timestamp_ns is not None
        ):
            raise ValueError(
                "client stop timestamps require shutdown_policy=client_stop"
            )
        return self


class MetricsConfig(StrictModel):
    markout_horizons_ns: tuple[int, ...] = (
        100_000_000,
        1_000_000_000,
        5_000_000_000,
    )
    near_limit_fraction: float = Field(default=0.8, gt=0, le=1)

    @model_validator(mode="after")
    def validate_horizons(self) -> MetricsConfig:
        if not self.markout_horizons_ns:
            raise ValueError("metrics.markout_horizons_ns cannot be empty")
        if any(value <= 0 for value in self.markout_horizons_ns):
            raise ValueError("all markout horizons must be positive")
        return self


class OutputConfig(StrictModel):
    runs_directory: Path = Path("runs")
    write_plots: bool = True


class AppConfig(StrictModel):
    instrument: InstrumentConfig = InstrumentConfig()
    data: DataConfig = DataConfig()
    synthetic: SyntheticConfig = SyntheticConfig()
    latency: LatencyConfig = LatencyConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    queue_model: QueueModelConfig = QueueModelConfig()
    fees: FeeConfig = FeeConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()
    metrics: MetricsConfig = MetricsConfig()
    output: OutputConfig = OutputConfig()
    random_seed: int = Field(default=7, ge=0)

    @model_validator(mode="after")
    def validate_dataset_identity(self) -> AppConfig:
        provenance = self.data.provenance
        if provenance.symbol != self.instrument.symbol:
            raise ValueError(
                "data.provenance.symbol must match instrument.symbol "
                f"({provenance.symbol!r} != {self.instrument.symbol!r})"
            )
        if (
            provenance.source_type is DatasetSourceType.HISTORICAL
            and self.data.input_path is None
        ):
            raise ValueError("historical dataset provenance requires data.input_path")
        return self


def load_config(path: str | Path) -> AppConfig:
    """Load and validate one YAML configuration file."""

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration root must be a mapping: {config_path}")
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration {config_path}: {exc}") from exc


def dump_config(config: AppConfig) -> dict[str, Any]:
    """Return a JSON/YAML-safe representation."""

    result = config.model_dump(mode="json")
    # Keep the historical baseline's serialized configuration and hashes intact.
    if config.backtest.shutdown_policy is ShutdownPolicy.FORCED_EXPIRY:
        for name in (
            "shutdown_policy",
            "client_stop_timestamp_ns",
            "observation_end_timestamp_ns",
        ):
            result["backtest"].pop(name)
    return result
