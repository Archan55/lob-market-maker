from decimal import Decimal

import pytest

from lobmm.config import FeeConfig, InstrumentConfig
from lobmm.enums import LiquidityRole, MarkPrice, Side
from lobmm.orders import Fill
from lobmm.portfolio import (
    AccountingError,
    AccountingFill,
    FillCosts,
    Portfolio,
    calculate_fill_costs,
    previous_average_or_raise,
    select_mark_price,
)


def test_fee_schedule_keeps_fees_and_rebates_separate() -> None:
    config = FeeConfig(
        maker_fee_per_unit=Decimal("0.01"),
        maker_rebate_per_unit=Decimal("0.002"),
        taker_fee_per_unit=Decimal("0.03"),
        proportional_fee_rate=Decimal("0.001"),
    )

    maker = calculate_fill_costs(
        config,
        role=LiquidityRole.MAKER,
        quantity=10,
        price_ticks=100,
        tick_value=Decimal("0.01"),
    )
    taker = calculate_fill_costs(
        config,
        role=LiquidityRole.TAKER,
        quantity=10,
        price_ticks=100,
        tick_value=Decimal("0.01"),
    )

    assert maker == FillCosts(fee=Decimal("0.11000"), rebate=Decimal("0.020"))
    assert taker == FillCosts(fee=Decimal("0.31000"), rebate=Decimal("0"))


def test_average_cost_partial_close_and_mark_identity() -> None:
    portfolio = Portfolio(tick_value=Decimal("0.01"))
    portfolio.record_fill(
        side=Side.BID,
        quantity=10,
        price_ticks=100,
        fee=0,
        rebate=0,
        mark_ticks=100,
    )
    snapshot = portfolio.record_fill(
        side=Side.ASK,
        quantity=4,
        price_ticks=105,
        fee=0,
        rebate=0,
        mark_ticks=105,
    )

    assert snapshot.inventory == 6
    assert snapshot.average_cost_ticks == Decimal(100)
    assert snapshot.trade_cash_ticks == -580
    assert snapshot.realized_pnl_ticks == Decimal(20)
    assert snapshot.unrealized_pnl_ticks == Decimal(30)
    assert snapshot.gross_pnl_ticks == Decimal(50)
    assert snapshot.gross_pnl == Decimal("0.50")
    assert snapshot.cash == Decimal("-5.80")
    assert snapshot.cash + Decimal(6 * 105) * Decimal("0.01") == snapshot.net_pnl


def test_crossing_through_zero_opens_new_average_cost() -> None:
    portfolio = Portfolio(tick_value=1)
    portfolio.record_fill(
        side=Side.BID,
        quantity=6,
        price_ticks=100,
        fee=0,
        rebate=0,
    )
    snapshot = portfolio.record_fill(
        side=Side.ASK,
        quantity=8,
        price_ticks=90,
        fee=0,
        rebate=0,
        mark_ticks=90,
    )

    assert snapshot.inventory == -2
    assert snapshot.average_cost_ticks == Decimal(90)
    assert snapshot.realized_pnl_ticks == Decimal(-60)
    assert snapshot.unrealized_pnl_ticks == 0
    assert snapshot.gross_pnl_ticks == Decimal(-60)

    flat = portfolio.record_fill(
        side=Side.BID,
        quantity=2,
        price_ticks=85,
        fee=0,
        rebate=0,
        mark_ticks=85,
    )
    assert flat.inventory == 0
    assert flat.average_cost_ticks is None
    assert flat.realized_pnl_ticks == Decimal(-50)
    assert flat.gross_pnl_ticks == Decimal(-50)


def test_apply_exchange_fill_books_explicit_costs_once() -> None:
    portfolio = Portfolio(
        instrument=InstrumentConfig(tick_size=Decimal("0.01"), currency="CAD"),
        fees=FeeConfig(
            maker_fee_per_unit=Decimal("9"),
            maker_rebate_per_unit=Decimal("9"),
        ),
    )
    fill = Fill(
        fill_id="F1",
        order_id="O1",
        client_order_id="C1",
        strategy_id="S1",
        side=Side.BID,
        quantity=5,
        price_ticks=100,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=123,
        fee=Decimal("0.05"),
        rebate=Decimal("0.01"),
    )

    snapshot = portfolio.apply_fill(fill, mark_ticks=101)

    assert snapshot.currency == "CAD"
    assert snapshot.timestamp_ns == 123
    assert snapshot.fees == Decimal("0.05")
    assert snapshot.rebates == Decimal("0.01")
    assert snapshot.gross_pnl == Decimal("0.05")
    assert snapshot.net_pnl == Decimal("0.01")
    assert snapshot.cash == Decimal("-5.04")
    assert snapshot.fill_count == 1


def test_apply_fill_can_delegate_cost_calculation() -> None:
    fees = FeeConfig(
        maker_fee_per_unit=Decimal("0.01"),
        maker_rebate_per_unit=Decimal("0.002"),
        proportional_fee_rate=Decimal("0"),
    )
    portfolio = Portfolio(fees=fees, tick_value=Decimal("0.01"))
    fill = Fill(
        fill_id="F1",
        order_id="O1",
        client_order_id="C1",
        strategy_id="S1",
        side=Side.BID,
        quantity=5,
        price_ticks=100,
        liquidity_role=LiquidityRole.MAKER,
        exchange_fill_timestamp_ns=1,
    )

    snapshot = portfolio.apply_fill(fill, calculate_costs=True)

    assert snapshot.fees == Decimal("0.05")
    assert snapshot.rebates == Decimal("0.010")
    assert snapshot.net_pnl == Decimal("-0.040")


def test_weighted_average_cost_for_inventory_adds() -> None:
    portfolio = Portfolio()
    portfolio.record_fill(side=Side.BID, quantity=2, price_ticks=100, fee=0, rebate=0)
    snapshot = portfolio.record_fill(
        side=Side.BID,
        quantity=3,
        price_ticks=110,
        fee=0,
        rebate=0,
        mark_ticks=106,
    )

    assert snapshot.average_cost_ticks == Decimal(106)
    assert snapshot.inventory == 5
    assert snapshot.trade_cash_ticks == -530
    assert snapshot.gross_pnl_ticks == 0


@pytest.mark.parametrize(
    ("method", "inventory", "expected"),
    [
        (MarkPrice.MIDPOINT, 10, Decimal("100.5")),
        (MarkPrice.CONSERVATIVE, 10, Decimal(100)),
        (MarkPrice.CONSERVATIVE, -10, Decimal(101)),
        (MarkPrice.CONSERVATIVE, 0, Decimal("100.5")),
        (MarkPrice.MICROPRICE, 0, Decimal("100.75")),
    ],
)
def test_mark_price_choices(
    method: MarkPrice, inventory: int, expected: Decimal
) -> None:
    assert (
        select_mark_price(
            method,
            inventory=inventory,
            best_bid_ticks=100,
            best_ask_ticks=101,
            microprice_ticks=Decimal("100.75"),
        )
        == expected
    )


def test_peak_exposure_is_monotonic_across_marks_and_fills() -> None:
    portfolio = Portfolio(tick_value=Decimal("0.01"))
    first = portfolio.record_fill(
        side=Side.BID,
        quantity=10,
        price_ticks=100,
        fee=0,
        rebate=0,
        mark_ticks=101,
    )
    second = portfolio.mark_to_market(99)
    third = portfolio.record_fill(
        side=Side.ASK,
        quantity=5,
        price_ticks=100,
        fee=0,
        rebate=0,
        mark_ticks=105,
    )

    assert first.current_gross_exposure == Decimal("10.10")
    assert second.current_gross_exposure == Decimal("9.90")
    assert second.peak_gross_exposure == Decimal("10.10")
    assert third.current_gross_exposure == Decimal("5.25")
    assert third.peak_gross_exposure == Decimal("10.10")


def test_invalid_cost_and_fill_inputs_fail_early() -> None:
    with pytest.raises(AccountingError, match="fee"):
        FillCosts(fee=Decimal("-0.01"))
    with pytest.raises(AccountingError, match="uncrossed"):
        select_mark_price(
            MarkPrice.MIDPOINT,
            inventory=0,
            best_bid_ticks=101,
            best_ask_ticks=101,
        )
    with pytest.raises(AccountingError, match="microprice"):
        select_mark_price(
            MarkPrice.MICROPRICE,
            inventory=0,
            best_bid_ticks=100,
            best_ask_ticks=101,
        )
    with pytest.raises(AccountingError, match="quantity"):
        Portfolio().record_fill(
            side=Side.BID,
            quantity=0,
            price_ticks=100,
            fee=0,
            rebate=0,
        )


def test_flat_unmarked_snapshot_is_well_defined() -> None:
    portfolio = Portfolio()
    portfolio.assert_invariants()
    snapshot = portfolio.snapshot()

    assert snapshot.mark_ticks == 0
    assert snapshot.inventory == 0
    assert snapshot.net_pnl == 0
    assert snapshot.equity == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"role": "maker"}, "LiquidityRole"),
        ({"quantity": 0}, "quantity"),
        ({"price_ticks": 0}, "price_ticks"),
        ({"tick_value": 0}, "tick_value"),
    ],
)
def test_fee_calculation_rejects_invalid_inputs(
    kwargs: dict[str, object], match: str
) -> None:
    arguments: dict[str, object] = {
        "role": LiquidityRole.MAKER,
        "quantity": 1,
        "price_ticks": 100,
        "tick_value": Decimal("0.01"),
    }
    arguments.update(kwargs)
    with pytest.raises(AccountingError, match=match):
        calculate_fill_costs(FeeConfig(), **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"side": 1}, "side"),
        ({"quantity": 0}, "quantity"),
        ({"price_ticks": 0}, "price_ticks"),
        ({"exchange_fill_timestamp_ns": -1}, "timestamp"),
        ({"liquidity_role": "maker"}, "liquidity_role"),
        ({"rebate": Decimal("-1")}, "rebate"),
    ],
)
def test_accounting_fill_validates_exchange_boundary(
    kwargs: dict[str, object], match: str
) -> None:
    arguments: dict[str, object] = {
        "side": Side.BID,
        "quantity": 1,
        "price_ticks": 100,
        "exchange_fill_timestamp_ns": 0,
    }
    arguments.update(kwargs)
    with pytest.raises(AccountingError, match=match):
        AccountingFill(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (
            lambda: Portfolio(InstrumentConfig(), tick_value=1),
            "instrument or tick_value",
        ),
        (lambda: Portfolio(tick_value=0), "tick_value"),
        (lambda: Portfolio(currency=""), "currency"),
        (lambda: Portfolio(identity_tolerance=-1), "identity_tolerance"),
    ],
)
def test_portfolio_configuration_validation(build: object, match: str) -> None:
    with pytest.raises(AccountingError, match=match):
        build()  # type: ignore[operator]


def test_record_fill_validation_and_default_zero_costs() -> None:
    portfolio = Portfolio()
    snapshot = portfolio.record_fill(
        side=Side.ASK,
        quantity=1,
        price_ticks=100,
        timestamp_ns=2,
    )
    assert snapshot.fees == 0
    assert snapshot.rebates == 0
    assert portfolio.fees == 0
    assert portfolio.rebates == 0
    assert portfolio.last_mark_ticks == Decimal(100)

    with pytest.raises(AccountingError, match="side"):
        portfolio.record_fill(  # type: ignore[arg-type]
            side=1, quantity=1, price_ticks=100, fee=0, rebate=0
        )
    with pytest.raises(AccountingError, match="price_ticks"):
        portfolio.record_fill(side=Side.BID, quantity=1, price_ticks=0, fee=0, rebate=0)
    with pytest.raises(AccountingError, match="timestamp"):
        portfolio.record_fill(
            side=Side.BID,
            quantity=1,
            price_ticks=100,
            timestamp_ns=-1,
            fee=0,
            rebate=0,
        )
    with pytest.raises(AccountingError, match="supplied together"):
        portfolio.record_fill(side=Side.BID, quantity=1, price_ticks=100, fee=0)
    with pytest.raises(AccountingError, match="liquidity_role"):
        Portfolio(fees=FeeConfig()).record_fill(
            side=Side.BID, quantity=1, price_ticks=100
        )


def test_mark_and_snapshot_validation() -> None:
    portfolio = Portfolio()
    portfolio.record_fill(side=Side.BID, quantity=1, price_ticks=100, fee=0, rebate=0)
    assert portfolio.snapshot(mark_ticks=101, timestamp_ns=3).timestamp_ns == 3
    assert portfolio.snapshot(timestamp_ns=4).timestamp_ns == 4

    with pytest.raises(AccountingError, match="positive"):
        portfolio.mark_to_market(0)
    with pytest.raises(AccountingError, match="timestamp"):
        portfolio.mark_to_market(100, timestamp_ns=-1)
    with pytest.raises(AccountingError, match="timestamp"):
        portfolio.snapshot(timestamp_ns=-1)


def test_accounting_invariant_guards_surface_corruption() -> None:
    portfolio = Portfolio()
    portfolio.average_cost_ticks = Decimal(100)
    with pytest.raises(AccountingError, match="flat inventory"):
        portfolio.assert_invariants(100)

    portfolio.average_cost_ticks = None
    portfolio.inventory = 1
    with pytest.raises(AccountingError, match="requires average cost"):
        portfolio.assert_invariants(100)

    portfolio.inventory = 0
    portfolio.buy_volume = -1
    with pytest.raises(AccountingError, match="fill volumes"):
        portfolio.assert_invariants(100)

    portfolio.buy_volume = 0
    portfolio.total_fees = Decimal("-1")
    with pytest.raises(AccountingError, match="fees and rebates"):
        portfolio.assert_invariants(100)

    valid = Portfolio()
    valid.record_fill(side=Side.BID, quantity=1, price_ticks=100, fee=0, rebate=0)
    valid.realized_pnl_ticks = Decimal(1)
    with pytest.raises(AccountingError, match="average-cost identity"):
        valid.assert_invariants(100)

    with pytest.raises(AccountingError, match="missing average cost"):
        previous_average_or_raise(None)


def test_additional_mark_validation() -> None:
    with pytest.raises(AccountingError, match="MarkPrice"):
        select_mark_price(  # type: ignore[arg-type]
            "midpoint",
            inventory=0,
            best_bid_ticks=100,
            best_ask_ticks=101,
        )
    with pytest.raises(AccountingError, match="positive"):
        select_mark_price(
            MarkPrice.MIDPOINT,
            inventory=0,
            best_bid_ticks=0,
            best_ask_ticks=101,
        )
    with pytest.raises(AccountingError, match="microprice_ticks"):
        select_mark_price(
            MarkPrice.MICROPRICE,
            inventory=0,
            best_bid_ticks=100,
            best_ask_ticks=101,
            microprice_ticks=0,
        )
