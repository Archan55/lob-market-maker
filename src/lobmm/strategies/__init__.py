"""Interpretable research market-making strategies."""

from lobmm.config import StrategyConfig
from lobmm.enums import StrategyName
from lobmm.strategies.base import MarketMakingStrategy
from lobmm.strategies.fixed_spread import FixedSpreadStrategy
from lobmm.strategies.inventory_aware import InventoryAwareStrategy
from lobmm.strategies.microprice import MicropriceStrategy


def make_strategy(config: StrategyConfig) -> MarketMakingStrategy:
    """Build the configured strategy without exposing exchange state."""

    if config.name is StrategyName.FIXED_SPREAD:
        return FixedSpreadStrategy(config)
    if config.name is StrategyName.INVENTORY_AWARE:
        return InventoryAwareStrategy(config)
    if config.name is StrategyName.MICROPRICE:
        return MicropriceStrategy(config)
    raise ValueError(f"unsupported strategy: {config.name}")


__all__ = [
    "FixedSpreadStrategy",
    "InventoryAwareStrategy",
    "MarketMakingStrategy",
    "MicropriceStrategy",
    "make_strategy",
]
