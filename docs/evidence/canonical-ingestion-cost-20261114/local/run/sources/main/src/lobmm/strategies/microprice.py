"""Microprice, imbalance, volatility, and inventory-aware strategy."""

from __future__ import annotations

from lobmm.enums import Side
from lobmm.events import BookView
from lobmm.strategies.base import DesiredQuote, MarketMakingStrategy


class MicropriceStrategy(MarketMakingStrategy):
    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        assert view.midpoint_ticks is not None
        normalized = known_inventory / max_abs_inventory if max_abs_inventory else 0.0
        fair = (
            view.microprice_ticks
            if view.microprice_ticks is not None
            else view.midpoint_ticks
        )
        imbalance = view.imbalance or 0.0
        reservation = (
            fair
            + self.config.imbalance_coefficient_ticks * imbalance
            - self.config.inventory_penalty_ticks * normalized
        )
        half_spread = max(
            self.config.minimum_half_spread_ticks,
            self.config.base_half_spread_ticks
            + self.config.volatility_multiplier * self.recent_volatility_ticks,
        )
        bid, ask = self.rounded_quotes(reservation, half_spread)
        size = self.inventory_scaled_quantity(
            self.config.order_size, known_inventory, max_abs_inventory
        )
        quotes: list[DesiredQuote] = []
        if known_inventory < max_abs_inventory:
            quotes.append(DesiredQuote(Side.BID, bid, size))
        if known_inventory > -max_abs_inventory:
            quotes.append(DesiredQuote(Side.ASK, ask, size))
        return tuple(quotes)
