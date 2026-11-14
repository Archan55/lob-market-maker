"""Inventory-adjusted reservation-price market maker."""

from __future__ import annotations

from lobmm.enums import Side
from lobmm.events import BookView
from lobmm.strategies.base import DesiredQuote, MarketMakingStrategy


class InventoryAwareStrategy(MarketMakingStrategy):
    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        assert view.midpoint_ticks is not None
        normalized = known_inventory / max_abs_inventory if max_abs_inventory else 0.0
        reservation = (
            view.midpoint_ticks - self.config.inventory_penalty_ticks * normalized
        )
        bid, ask = self.rounded_quotes(reservation, self.config.base_half_spread_ticks)
        size = self.inventory_scaled_quantity(
            self.config.order_size, known_inventory, max_abs_inventory
        )
        quotes: list[DesiredQuote] = []
        if known_inventory < max_abs_inventory:
            quotes.append(DesiredQuote(Side.BID, bid, size))
        if known_inventory > -max_abs_inventory:
            quotes.append(DesiredQuote(Side.ASK, ask, size))
        return tuple(quotes)
