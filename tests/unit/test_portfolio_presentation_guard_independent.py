"""Retain the original tick-notional corruption bound for writable averages."""

from decimal import Decimal

import pytest

from lobmm.enums import Side
from lobmm.portfolio import AccountingError, Portfolio


@pytest.mark.parametrize(
    ("tolerance", "changed_average"),
    (
        ("1e-18", "100.0000000000000000001"),
        ("1e-12", "100.0000000000001"),
    ),
)
def test_average_view_corruption_is_bounded_in_tick_notional(
    tolerance: str, changed_average: str
) -> None:
    # The original guard compares q * average error with a tick-notional bound.
    # Comparing the average error alone silently weakens that guard at size.
    ledger = Portfolio(identity_tolerance=tolerance)
    ledger.record_fill(side=Side.BID, quantity=1_000_000_000, price_ticks=100)
    ledger.average_cost_ticks = Decimal(changed_average)
    with pytest.raises(AccountingError, match="average-cost identity"):
        ledger.assert_invariants(100)
