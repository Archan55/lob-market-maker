from decimal import Decimal

import pytest

from lobmm.config import RiskConfig
from lobmm.enums import RiskReason, SessionEndPolicy, Side
from lobmm.risk import (
    OpenOrderExposure,
    OrderRiskRequest,
    RiskContext,
    RiskError,
    RiskManager,
    project_worst_case_exposure,
)


def order(
    order_id: str,
    side: Side,
    quantity: int,
    *,
    accepted_timestamp_ns: int = 0,
    is_quote: bool = True,
) -> OpenOrderExposure:
    return OpenOrderExposure(
        order_id=order_id,
        side=side,
        remaining_quantity=quantity,
        accepted_timestamp_ns=accepted_timestamp_ns,
        is_quote=is_quote,
    )


def test_projected_exposure_does_not_net_bids_against_asks() -> None:
    exposure = project_worst_case_exposure(
        3,
        (order("bid", Side.BID, 8), order("ask", Side.ASK, 7)),
        OrderRiskRequest(
            side=Side.BID,
            quantity=4,
            timestamp_ns=0,
        ),
    )

    assert exposure.worst_long_inventory == 15
    assert exposure.worst_short_inventory == -4
    assert exposure.total_open_quantity == 19
    assert exposure.open_order_count == 3


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"quantity": 6}, RiskReason.MAX_ORDER_SIZE),
        (
            {"quantity": 4, "open_orders": (order("b", Side.BID, 7),)},
            RiskReason.MAX_OPEN_QUANTITY,
        ),
        (
            {
                "quantity": 1,
                "open_orders": (
                    order("b", Side.BID, 1),
                    order("a", Side.ASK, 1),
                ),
            },
            RiskReason.MAX_OPEN_ORDERS,
        ),
    ],
)
def test_order_and_open_limits(
    kwargs: dict[str, object], expected_reason: RiskReason
) -> None:
    manager = RiskManager(
        RiskConfig(
            max_abs_inventory=100,
            max_order_size=5,
            max_total_open_quantity=10,
            max_open_orders=2,
        )
    )
    decision = manager.check_order(
        side=Side.BID,
        quantity=kwargs.get("quantity", 1),  # type: ignore[arg-type]
        timestamp_ns=0,
        inventory=0,
        open_orders=kwargs.get("open_orders", ()),  # type: ignore[arg-type]
        is_quote=False,
    )

    assert not decision.approved
    assert decision.reason is expected_reason
    assert manager.events[-1].reason is expected_reason


def test_directional_position_limits_use_worst_case_live_orders() -> None:
    manager = RiskManager(RiskConfig(max_abs_inventory=10, max_order_size=10))

    long = manager.check_order(
        side=Side.BID,
        quantity=3,
        timestamp_ns=0,
        inventory=5,
        open_orders=(order("b", Side.BID, 3), order("a", Side.ASK, 9)),
        is_quote=False,
    )
    short = manager.check_order(
        side=Side.ASK,
        quantity=3,
        timestamp_ns=1,
        inventory=-5,
        open_orders=(order("a2", Side.ASK, 3), order("b2", Side.BID, 9)),
        is_quote=False,
    )

    assert long.reason is RiskReason.POSITION_LIMIT
    assert short.reason is RiskReason.POSITION_LIMIT


def test_reduce_only_liquidation_is_allowed_during_kill_switch() -> None:
    manager = RiskManager(RiskConfig(max_order_size=20))
    manager.activate_kill_switch(timestamp_ns=5)

    opening = manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=6,
        inventory=10,
        is_quote=False,
    )
    reducing = manager.check_order(
        side=Side.ASK,
        quantity=10,
        timestamp_ns=7,
        inventory=10,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )
    crossing = manager.check_order(
        side=Side.ASK,
        quantity=11,
        timestamp_ns=8,
        inventory=10,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )

    assert opening.reason is RiskReason.KILL_SWITCH
    assert opening.cancel_open_orders
    assert reducing.approved
    assert reducing.risk_reducing
    assert crossing.reason is RiskReason.INVALID_ORDER


def test_loss_and_drawdown_activate_sticky_kill_switch() -> None:
    loss_manager = RiskManager(
        RiskConfig(max_loss=Decimal("10"), max_drawdown=Decimal("100"))
    )
    loss = loss_manager.observe_pnl(Decimal("-10"), timestamp_ns=1)

    assert loss.reason is RiskReason.MAX_LOSS
    assert loss.cancel_open_orders
    assert loss_manager.kill_switch_active

    drawdown_manager = RiskManager(
        RiskConfig(max_loss=Decimal("100"), max_drawdown=Decimal("5"))
    )
    assert drawdown_manager.observe_pnl(10, timestamp_ns=1).approved
    drawdown = drawdown_manager.observe_pnl(5, timestamp_ns=2)

    assert drawdown.reason is RiskReason.MAX_DRAWDOWN
    assert drawdown_manager.high_watermark == Decimal(10)


def test_spread_and_volatility_suppress_quotes_but_not_reduce_only() -> None:
    manager = RiskManager(
        RiskConfig(
            minimum_spread_ticks=2,
            max_volatility_ticks=1.5,
            max_order_size=10,
        )
    )

    spread = manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=0,
        inventory=0,
        spread_ticks=1,
        volatility_ticks=1,
    )
    volatility = manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=1,
        inventory=0,
        spread_ticks=2,
        volatility_ticks=Decimal("1.6"),
    )
    reducing = manager.check_order(
        side=Side.ASK,
        quantity=5,
        timestamp_ns=2,
        inventory=5,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
        spread_ticks=0,
        volatility_ticks=99,
    )

    assert spread.reason is RiskReason.SPREAD_TOO_NARROW
    assert volatility.reason is RiskReason.VOLATILITY_TOO_HIGH
    assert reducing.approved


def test_message_rate_is_a_sliding_one_second_window() -> None:
    manager = RiskManager(RiskConfig(max_messages_per_second=2))

    assert manager.register_message(timestamp_ns=0).approved
    assert manager.register_message(timestamp_ns=999_999_999).approved
    rejected = manager.register_message(timestamp_ns=999_999_999)
    after_window = manager.register_message(timestamp_ns=1_000_000_000)

    assert rejected.reason is RiskReason.MESSAGE_RATE
    assert after_window.approved


def test_quote_age_requests_cancel_once_per_stale_order() -> None:
    manager = RiskManager(RiskConfig(max_quote_age_ns=100))
    orders = (
        order("old", Side.BID, 1, accepted_timestamp_ns=0),
        order("fresh", Side.ASK, 1, accepted_timestamp_ns=50),
        order(
            "nonquote",
            Side.ASK,
            1,
            accepted_timestamp_ns=0,
            is_quote=False,
        ),
    )

    assert manager.stale_quote_ids(timestamp_ns=100, open_orders=orders) == ()
    assert manager.stale_quote_ids(timestamp_ns=101, open_orders=orders) == ("old",)
    assert manager.stale_quote_ids(timestamp_ns=102, open_orders=orders) == ("old",)
    assert [event.reason for event in manager.events] == [RiskReason.QUOTE_AGE]


def test_session_cutoff_and_liquidation_directive() -> None:
    manager = RiskManager(
        RiskConfig(prohibit_new_quotes_last_ns=100, max_order_size=20),
        session_end_ns=1_000,
        session_end_policy=SessionEndPolicy.LIQUIDATE,
    )
    orders = (order("q", Side.BID, 1), order("hedge", Side.ASK, 1, is_quote=False))

    cutoff_rejection = manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=900,
        inventory=5,
    )
    before_end = manager.session_directive(
        timestamp_ns=900, inventory=5, open_orders=orders
    )
    at_end = manager.session_directive(
        timestamp_ns=1_000, inventory=5, open_orders=orders
    )
    liquidation = manager.check_order(
        side=Side.ASK,
        quantity=5,
        timestamp_ns=1_000,
        inventory=5,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )

    assert cutoff_rejection.reason is RiskReason.SESSION_END
    assert before_end.block_new_quotes
    assert before_end.cancel_order_ids == ("q",)
    assert at_end.cancel_order_ids == ("q", "hedge")
    assert at_end.liquidation_side is Side.ASK
    assert at_end.liquidation_quantity == 5
    assert not at_end.mark_remaining_inventory
    assert liquidation.approved


def test_non_liquidating_session_policy_marks_inventory() -> None:
    manager = RiskManager(
        RiskConfig(),
        session_end_ns=10,
        session_end_policy=SessionEndPolicy.MARK,
    )

    directive = manager.session_directive(
        timestamp_ns=10,
        inventory=-3,
        open_orders=(),
    )

    assert directive.active
    assert directive.mark_remaining_inventory
    assert directive.liquidation_quantity == 0


def test_continuous_checks_combine_kill_stale_and_session_cancels() -> None:
    manager = RiskManager(
        RiskConfig(
            max_loss=Decimal("5"),
            max_quote_age_ns=10,
            prohibit_new_quotes_last_ns=10,
        ),
        session_end_ns=100,
    )
    orders = (
        order("stale", Side.BID, 1, accepted_timestamp_ns=0),
        order("other", Side.ASK, 1, accepted_timestamp_ns=95),
    )

    result = manager.continuous_check(
        timestamp_ns=95,
        net_pnl=-5,
        inventory=1,
        open_orders=orders,
    )

    assert result.kill_switch_active
    assert result.cancel_order_ids == ("other", "stale")
    assert result.session.active


def test_invalid_risk_inputs_fail_early() -> None:
    with pytest.raises(RiskError, match="session"):
        RiskManager(RiskConfig(), session_start_ns=2, session_end_ns=1)
    with pytest.raises(RiskError, match="remaining_quantity"):
        order("bad", Side.BID, 0)

    manager = RiskManager(RiskConfig())
    decision = manager.check_order(
        side=Side.BID,
        quantity=0,
        timestamp_ns=0,
        inventory=0,
    )
    assert decision.reason is RiskReason.INVALID_ORDER


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"order_id": ""}, "order_id"),
        ({"side": 1}, "side"),
        ({"accepted_timestamp_ns": -1}, "accepted_timestamp_ns"),
    ],
)
def test_open_order_exposure_validates_boundary(
    kwargs: dict[str, object], match: str
) -> None:
    arguments: dict[str, object] = {
        "order_id": "O1",
        "side": Side.BID,
        "remaining_quantity": 1,
        "accepted_timestamp_ns": 0,
    }
    arguments.update(kwargs)
    with pytest.raises(RiskError, match=match):
        OpenOrderExposure(**arguments)  # type: ignore[arg-type]


def test_projection_validates_proposal_and_handles_non_resting_ask() -> None:
    with pytest.raises(RiskError, match="quantity"):
        project_worst_case_exposure(
            0,
            (),
            OrderRiskRequest(side=Side.BID, quantity=0, timestamp_ns=0),
        )

    exposure = project_worst_case_exposure(
        2,
        (),
        OrderRiskRequest(
            side=Side.ASK,
            quantity=3,
            timestamp_ns=0,
            may_rest=False,
        ),
    )
    assert exposure.worst_short_inventory == -1
    assert exposure.total_open_quantity == 0
    assert exposure.open_order_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_start_ns": -1},
        {"session_end_ns": -1},
        {"session_end_policy": "mark"},
    ],
)
def test_risk_manager_constructor_rejects_invalid_values(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(RiskError):
        RiskManager(RiskConfig(), **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("case_request", "context"),
    [
        (
            OrderRiskRequest(  # type: ignore[arg-type]
                side=1, quantity=1, timestamp_ns=0
            ),
            RiskContext(inventory=0),
        ),
        (
            OrderRiskRequest(side=Side.BID, quantity=1, timestamp_ns=-1),
            RiskContext(inventory=0),
        ),
        (
            OrderRiskRequest(side=Side.BID, quantity=1, timestamp_ns=0, price_ticks=0),
            RiskContext(inventory=0),
        ),
        (
            OrderRiskRequest(side=Side.BID, quantity=1, timestamp_ns=0),
            RiskContext(inventory=0, spread_ticks=-1),
        ),
        (
            OrderRiskRequest(side=Side.BID, quantity=1, timestamp_ns=0),
            RiskContext(inventory=0, volatility_ticks=Decimal("-1")),
        ),
    ],
)
def test_pretrade_input_validation(
    case_request: OrderRiskRequest, context: RiskContext
) -> None:
    decision = RiskManager(RiskConfig()).evaluate(case_request, context)
    assert decision.reason is RiskReason.INVALID_ORDER


def test_session_start_and_end_reject_new_risk() -> None:
    manager = RiskManager(
        RiskConfig(max_order_size=10),
        session_start_ns=100,
        session_end_ns=200,
        session_end_policy=SessionEndPolicy.MARK,
    )

    before = manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=99,
        inventory=0,
        is_quote=False,
    )
    after = manager.check_order(
        side=Side.ASK,
        quantity=1,
        timestamp_ns=200,
        inventory=1,
        reduce_only=True,
        is_quote=False,
        may_rest=False,
    )

    assert before.reason is RiskReason.SESSION_END
    assert after.reason is RiskReason.SESSION_END


def test_order_path_enforces_message_rate_and_bypass_is_explicit() -> None:
    manager = RiskManager(RiskConfig(max_messages_per_second=1))
    assert manager.check_order(
        side=Side.BID,
        quantity=1,
        timestamp_ns=0,
        inventory=0,
        is_quote=False,
    ).approved
    rejected = manager.check_order(
        side=Side.ASK,
        quantity=1,
        timestamp_ns=1,
        inventory=0,
        is_quote=False,
    )
    bypassed = manager.register_message(timestamp_ns=2, bypass_limit=True)

    assert rejected.reason is RiskReason.MESSAGE_RATE
    assert bypassed.approved
    with pytest.raises(RiskError, match="timestamp"):
        manager.register_message(timestamp_ns=-1)


def test_kill_switch_activation_validation_and_idempotence() -> None:
    manager = RiskManager(RiskConfig())
    with pytest.raises(RiskError, match="timestamp"):
        manager.activate_kill_switch(timestamp_ns=-1)
    with pytest.raises(RiskError, match="RiskReason"):
        manager.activate_kill_switch(  # type: ignore[arg-type]
            timestamp_ns=0, reason="manual"
        )

    manager.activate_kill_switch(timestamp_ns=1)
    manager.activate_kill_switch(
        timestamp_ns=2,
        reason=RiskReason.MAX_LOSS,
    )
    assert len(manager.events) == 1
    assert manager.kill_reason is RiskReason.KILL_SWITCH


def test_continuous_and_session_no_action_paths() -> None:
    manager = RiskManager(RiskConfig())
    result = manager.continuous_check(
        timestamp_ns=10,
        net_pnl=1,
        inventory=0,
        open_orders=(),
    )
    assert not result.kill_switch_active
    assert result.cancel_order_ids == ()
    assert not result.session.active

    future_session = RiskManager(
        RiskConfig(prohibit_new_quotes_last_ns=10),
        session_end_ns=100,
        session_end_policy=SessionEndPolicy.LIQUIDATE,
    )
    assert not future_session.session_directive(
        timestamp_ns=50, inventory=0, open_orders=()
    ).active
    short_close = future_session.session_directive(
        timestamp_ns=100, inventory=-2, open_orders=()
    )
    assert short_close.liquidation_side is Side.BID

    with pytest.raises(RiskError, match="timestamp"):
        manager.observe_pnl(0, timestamp_ns=-1)
    with pytest.raises(RiskError, match="timestamp"):
        manager.stale_quote_ids(timestamp_ns=-1, open_orders=())
    with pytest.raises(RiskError, match="timestamp"):
        manager.session_directive(timestamp_ns=-1, inventory=0, open_orders=())
