"""Reduce-only admission must conserve capacity across outstanding requests.

OpenOrderExposure intentionally represents accepted, in-flight, and pending-cancel
orders identically: none releases its remaining capacity before venue termination.
The oracle enumerates actual possible unit fills instead of using risk projections.
"""

from itertools import product

import pytest

from lobmm.config import RiskConfig
from lobmm.enums import RiskReason, Side
from lobmm.risk import OpenOrderExposure, RiskManager


def _exposure(name: str, side: Side, quantity: int) -> OpenOrderExposure:
    return OpenOrderExposure(name, side, quantity, 0, is_quote=False)


@pytest.mark.parametrize("inventory", [-2, 2])
def test_reduce_only_rejects_aggregate_reservations_that_reverse_inventory(
    inventory: int,
) -> None:
    side = Side.ASK if inventory > 0 else Side.BID
    manager = RiskManager(RiskConfig(max_abs_inventory=2, max_order_size=2))
    decision = manager.check_order(
        side=side,
        quantity=2,
        timestamp_ns=1,
        inventory=inventory,
        open_orders=(
            _exposure("live-reduction", side, 2),
            _exposure("pending:reduction", side, 2),
        ),
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )

    assert not decision.approved
    assert decision.reason is RiskReason.INVALID_ORDER
    assert "outstanding" in decision.detail
    assert "inventory" in decision.detail
    assert manager.events[-1].reason is RiskReason.INVALID_ORDER


@pytest.mark.parametrize("inventory", [-4, -3, -2, -1, 1, 2, 3, 4])
def test_reduce_only_admission_matches_exhaustive_partial_fill_oracle(
    inventory: int,
) -> None:
    """All fill subsets must retain the current sign, even if asks/bids coexist."""

    side = Side.ASK if inventory > 0 else Side.BID
    direction = 1 if inventory > 0 else -1
    config = RiskConfig(
        max_abs_inventory=2,
        max_order_size=4,
        max_total_open_quantity=100,
        max_open_orders=100,
    )
    for quantities in product(range(3), repeat=3):
        reducing = tuple(
            _exposure(name, side, quantity)
            for name, quantity in zip(
                ("live", "pending:new", "pending-cancel"), quantities, strict=True
            )
            if quantity
        )
        for opposite_quantity in range(3):
            opposite = (
                (_exposure("opposite", side.opposite, opposite_quantity),)
                if opposite_quantity
                else ()
            )
            outstanding = reducing + opposite
            for proposed_quantity in range(1, 5):
                reservoir = (
                    *outstanding,
                    _exposure("proposed", side, proposed_quantity),
                )
                can_reverse = any(
                    direction
                    * (
                        inventory
                        + sum(
                            int(order.side) * units
                            for order, units in zip(reservoir, fills, strict=True)
                        )
                    )
                    < 0
                    for fills in product(
                        *(range(order.remaining_quantity + 1) for order in reservoir)
                    )
                )
                decision = RiskManager(config).check_order(
                    side=side,
                    quantity=proposed_quantity,
                    timestamp_ns=1,
                    inventory=inventory,
                    open_orders=outstanding,
                    reduce_only=True,
                    is_quote=False,
                    may_rest=False,
                )
                assert decision.approved is (not can_reverse), (
                    inventory,
                    quantities,
                    opposite_quantity,
                    proposed_quantity,
                )
                if decision.approved:
                    assert decision.risk_reducing
                else:
                    assert decision.reason is RiskReason.INVALID_ORDER


@pytest.mark.parametrize("inventory", [-5, 5])
def test_cancel_request_does_not_release_reduce_only_reservation(
    inventory: int,
) -> None:
    side = Side.ASK if inventory > 0 else Side.BID
    manager = RiskManager(RiskConfig(max_abs_inventory=2, max_order_size=5))
    cancelling = _exposure("pending-cancel", side, 3)
    before_fill = manager.check_order(
        side=side,
        quantity=3,
        timestamp_ns=1,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
        inventory=inventory,
        open_orders=(cancelling,),
    )
    assert not before_fill.approved

    # A one-unit fill consumes inventory and the reservation together; it does
    # not free capacity for an additional same-direction reducing request.
    after_fill_inventory = inventory + int(side)
    after_fill = manager.check_order(
        side=side,
        quantity=3,
        timestamp_ns=1,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
        inventory=after_fill_inventory,
        open_orders=(_exposure("pending-cancel", side, 2),),
    )
    assert not after_fill.approved

    # Only authoritative terminal cancellation releases the unfilled remainder.
    # Safe reduction is still admitted when current inventory exceeds its cap.
    after_cancel = manager.check_order(
        side=side,
        quantity=3,
        timestamp_ns=1,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
        inventory=after_fill_inventory,
        open_orders=(),
    )
    assert after_cancel.approved
    assert after_cancel.risk_reducing


@pytest.mark.parametrize("inventory", [-3, 3])
def test_opposite_orders_do_not_supply_reduce_only_headroom(inventory: int) -> None:
    side = Side.ASK if inventory > 0 else Side.BID
    decision = RiskManager(RiskConfig(max_order_size=3)).check_order(
        side=side,
        quantity=2,
        timestamp_ns=1,
        inventory=inventory,
        open_orders=(
            _exposure("already-reducing", side, 2),
            _exposure("opposite-could-remain-unfilled", side.opposite, 10),
        ),
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )
    assert not decision.approved
    assert decision.reason is RiskReason.INVALID_ORDER


@pytest.mark.parametrize("inventory", [-4, 4])
def test_reserved_reduction_can_flatten_inventory_above_cap_during_kill(
    inventory: int,
) -> None:
    side = Side.ASK if inventory > 0 else Side.BID
    manager = RiskManager(RiskConfig(max_abs_inventory=2, max_order_size=4))
    manager.activate_kill_switch(timestamp_ns=0)
    decision = manager.check_order(
        side=side,
        quantity=3,
        timestamp_ns=1,
        inventory=inventory,
        open_orders=(_exposure("pending:reduction", side, 1),),
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )
    assert decision.approved
    assert decision.risk_reducing
    assert inventory + int(side) * (1 + 3) == 0
