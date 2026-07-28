"""Fixed-spread midpoint benchmark."""

from __future__ import annotations

from lobmm.enums import Side
from lobmm.events import BookView
from lobmm.strategies.base import DesiredQuote, MarketMakingStrategy


class FixedSpreadStrategy(MarketMakingStrategy):
    def desired_quotes(
        self,
        *,
        view: BookView,
        known_inventory: int,
        max_abs_inventory: int,
    ) -> tuple[DesiredQuote, ...]:
        assert view.midpoint_ticks is not None
        bid, ask = self.rounded_quotes(
            view.midpoint_ticks, self.config.base_half_spread_ticks
        )
        quotes: list[DesiredQuote] = []
        if known_inventory < max_abs_inventory:
            quotes.append(DesiredQuote(Side.BID, bid, self.config.order_size))
        if known_inventory > -max_abs_inventory:
            quotes.append(DesiredQuote(Side.ASK, ask, self.config.order_size))
        return tuple(quotes)
