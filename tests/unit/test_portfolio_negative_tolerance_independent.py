"""Negative mutable tolerances must not bypass equal-view fast paths.

Inputs and rejection criteria were fixed before execution. These checks use
only the public Portfolio API and do not reproduce its invariant arithmetic.
"""

from decimal import Decimal

import pytest

from lobmm.enums import Side
from lobmm.portfolio import AccountingError, Portfolio


@pytest.mark.parametrize("with_fill_costs", [False, True], ids=["unmarked", "fees"])
@pytest.mark.parametrize("operation", ["assert_invariants", "snapshot"])
def test_negative_mutable_tolerance_rejected_when_public_views_are_unchanged(
    with_fill_costs: bool, operation: str
) -> None:
    ledger = Portfolio(identity_tolerance=0)
    if with_fill_costs:
        filled = ledger.record_fill(
            side=Side.BID,
            quantity=2,
            price_ticks=100,
            fee=Decimal("0.003"),
            rebate=Decimal("0.001"),
            mark_ticks=101,
        )
        assert filled.average_cost_ticks == Decimal(100)
        assert filled.realized_pnl_ticks == Decimal(0)
        assert filled.fees == Decimal("0.003")
        assert filled.rebates == Decimal("0.001")
    else:
        assert ledger.last_mark_ticks is None
    ledger.assert_invariants()

    # The constructor disallows this value. Mutating the public field cannot
    # make it acceptable merely because both public views equal their originals.
    ledger.identity_tolerance = Decimal("-1e-18")
    with pytest.raises(AccountingError):
        if operation == "assert_invariants":
            ledger.assert_invariants()
        else:
            ledger.snapshot(timestamp_ns=999)
