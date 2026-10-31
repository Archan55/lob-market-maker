"""Exact fill accounting and marked portfolio state.

Matching prices remain integer ticks.  Currency conversion happens only in
this accounting layer and uses :class:`~decimal.Decimal`, so fees and rebates
are never mixed into binary floating-point arithmetic.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from fractions import Fraction
from typing import Protocol, runtime_checkable

from lobmm.config import FeeConfig, InstrumentConfig
from lobmm.enums import LiquidityRole, MarkPrice, Side

DecimalLike = Decimal | int | float | str
ZERO = Decimal(0)


class AccountingError(ValueError):
    """Raised when a fill or accounting identity is invalid."""


def _decimal(value: DecimalLike) -> Decimal:
    """Convert configuration/user numeric input without inheriting float noise."""

    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise AccountingError("accounting values must be finite")
    return result


@dataclass(frozen=True, slots=True)
class FillCosts:
    """Nonnegative cost and income booked for one fill."""

    fee: Decimal = ZERO
    rebate: Decimal = ZERO

    def __post_init__(self) -> None:
        if _decimal(self.fee) < 0:
            raise AccountingError("fee must be nonnegative")
        if _decimal(self.rebate) < 0:
            raise AccountingError("rebate must be nonnegative")


def calculate_fill_costs(
    config: FeeConfig,
    *,
    role: LiquidityRole,
    quantity: int,
    price_ticks: int,
    tick_value: DecimalLike,
) -> FillCosts:
    """Calculate per-unit and proportional economics for a fill.

    A positive fee is a cost and a positive rebate is income.  The
    proportional fee is applied to absolute currency notional.
    """

    if not isinstance(role, LiquidityRole):
        raise AccountingError("role must be a LiquidityRole")
    if quantity <= 0:
        raise AccountingError("quantity must be positive")
    if price_ticks <= 0:
        raise AccountingError("price_ticks must be positive")
    tick = _decimal(tick_value)
    if tick <= 0:
        raise AccountingError("tick_value must be positive")

    units = Fraction(quantity)
    notional = units * price_ticks * Fraction(tick)
    proportional_fee = Fraction(config.proportional_fee_rate) * notional
    if role is LiquidityRole.MAKER:
        return FillCosts(
            fee=_fraction_decimal(
                Fraction(config.maker_fee_per_unit) * units + proportional_fee
            ),
            rebate=_fraction_decimal(Fraction(config.maker_rebate_per_unit) * units),
        )
    return FillCosts(
        fee=_fraction_decimal(
            Fraction(config.taker_fee_per_unit) * units + proportional_fee
        ),
        rebate=ZERO,
    )


@runtime_checkable
class FillLike(Protocol):
    """Structural exchange-to-portfolio fill contract.

    Exchange records can implement this protocol without importing portfolio
    internals.  Costs may be supplied by the exchange; when both are zero, a
    configured fee schedule can calculate them in :meth:`Portfolio.apply_fill`.
    """

    side: Side
    quantity: int
    price_ticks: int
    fee: Decimal
    rebate: Decimal
    liquidity_role: LiquidityRole
    exchange_fill_timestamp_ns: int


@dataclass(frozen=True, slots=True)
class AccountingFill:
    """Small concrete fill useful at package boundaries and in tests."""

    side: Side
    quantity: int
    price_ticks: int
    exchange_fill_timestamp_ns: int
    liquidity_role: LiquidityRole = LiquidityRole.MAKER
    fee: Decimal = ZERO
    rebate: Decimal = ZERO

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise AccountingError("side must be a Side")
        if self.quantity <= 0:
            raise AccountingError("quantity must be positive")
        if self.price_ticks <= 0:
            raise AccountingError("price_ticks must be positive")
        if self.exchange_fill_timestamp_ns < 0:
            raise AccountingError("exchange_fill_timestamp_ns must be nonnegative")
        if not isinstance(self.liquidity_role, LiquidityRole):
            raise AccountingError("liquidity_role must be a LiquidityRole")
        FillCosts(self.fee, self.rebate)


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Immutable marked accounting state in both ticks and currency."""

    timestamp_ns: int | None
    currency: str
    tick_value: Decimal
    mark_ticks: Decimal
    inventory: int
    average_cost_ticks: Decimal | None
    trade_cash_ticks: int
    trade_cash: Decimal
    cash: Decimal
    realized_pnl_ticks: Decimal
    unrealized_pnl_ticks: Decimal
    gross_pnl_ticks: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    rebates: Decimal
    net_pnl: Decimal
    turnover_ticks: int
    turnover: Decimal
    buy_volume: int
    sell_volume: int
    fill_count: int
    current_gross_exposure: Decimal
    peak_gross_exposure: Decimal

    @property
    def equity(self) -> Decimal:
        """Marked equity when the portfolio starts from zero cash."""

        return self.net_pnl


def select_mark_price(
    method: MarkPrice,
    *,
    inventory: int,
    best_bid_ticks: int,
    best_ask_ticks: int,
    microprice_ticks: DecimalLike | None = None,
) -> Decimal:
    """Choose a mark without converting prices out of tick space."""

    if not isinstance(method, MarkPrice):
        raise AccountingError("method must be a MarkPrice")
    if best_bid_ticks <= 0 or best_ask_ticks <= 0:
        raise AccountingError("best bid and ask must be positive")
    if best_bid_ticks >= best_ask_ticks:
        raise AccountingError("mark requires an uncrossed market")

    bid = Decimal(best_bid_ticks)
    ask = Decimal(best_ask_ticks)
    if method is MarkPrice.MIDPOINT:
        return (bid + ask) / 2
    if method is MarkPrice.MICROPRICE:
        if microprice_ticks is None:
            raise AccountingError("microprice mark requires microprice_ticks")
        microprice = _decimal(microprice_ticks)
        if microprice <= 0:
            raise AccountingError("microprice_ticks must be positive")
        return microprice
    if inventory > 0:
        return bid
    if inventory < 0:
        return ask
    return (bid + ask) / 2


def _fraction_decimal(value: Fraction) -> Decimal:
    """Render terminating values exactly; round repeating views in a local context.

    Integer cash, finite decimal costs and finite decimal marks have terminating
    representations, regardless of the ambient Decimal precision. Only views
    such as average cost and realized P&L can require rounding. They never feed
    back into the authoritative rational cost basis.
    """

    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator == 1:
        scale = max(twos, fives)
        coefficient = value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
        digits = tuple(int(char) for char in str(abs(coefficient)))
        return Decimal((int(coefficient < 0), digits, -scale))
    try:
        with localcontext():
            return Decimal(value.numerator) / Decimal(value.denominator)
    except DecimalException as exc:
        raise AccountingError("cannot render repeating accounting value") from exc


class Portfolio:
    """Authoritative rational cost basis with compatible Decimal public views.

    Signed acquisition cost is exact. Realized ticks equal trade cash plus the
    remaining signed cost basis. Rounded averages and P&L views are never used
    to update or validate that identity. Mutating operations validate a candidate
    before committing any state, including timestamps and exposure peaks.
    """

    def __init__(
        self,
        instrument: InstrumentConfig | None = None,
        fees: FeeConfig | None = None,
        *,
        tick_value: DecimalLike | None = None,
        currency: str | None = None,
        identity_tolerance: DecimalLike = Decimal("1e-18"),
    ) -> None:
        if instrument is not None and tick_value is not None:
            raise AccountingError("pass instrument or tick_value, not both")
        resolved_tick = _decimal(
            instrument.tick_size
            if instrument is not None
            else (1 if tick_value is None else tick_value)
        )
        if resolved_tick <= 0:
            raise AccountingError("tick_value must be positive")
        resolved_currency = (
            instrument.currency
            if instrument is not None
            else ("USD" if currency is None else currency)
        )
        if not resolved_currency:
            raise AccountingError("currency cannot be empty")
        tolerance = _decimal(identity_tolerance)
        if tolerance < 0:
            raise AccountingError("identity_tolerance must be nonnegative")
        self.tick_value = resolved_tick
        self.currency = resolved_currency
        self.fee_config = fees
        self.identity_tolerance = tolerance
        self.inventory = 0
        self.average_cost_ticks: Decimal | None = None
        self.trade_cash_ticks = 0
        self.realized_pnl_ticks = ZERO
        self.total_fees = ZERO
        self.total_rebates = ZERO
        self.turnover_ticks = 0
        self.buy_volume = 0
        self.sell_volume = 0
        self.fill_count = 0
        self.peak_gross_exposure = ZERO
        self._last_mark_ticks: Decimal | None = None
        self._last_timestamp_ns: int | None = None
        self._cost_basis_ticks = Fraction(0)
        self._realized_ticks = Fraction(0)
        # Public fields remain writable for compatibility. These expected views
        # detect external corruption without treating representation rounding as
        # an error in the exact ledger, even if the caller changes its context.
        self._average_cost_view: Decimal | None = None
        self._realized_view = ZERO

    @property
    def trade_cash(self) -> Decimal:
        return _fraction_decimal(
            Fraction(self.trade_cash_ticks) * Fraction(self.tick_value)
        )

    @property
    def cash(self) -> Decimal:
        return _fraction_decimal(
            Fraction(self.trade_cash_ticks) * Fraction(self.tick_value)
            - Fraction(self.total_fees)
            + Fraction(self.total_rebates)
        )

    @property
    def fees(self) -> Decimal:
        return self.total_fees

    @property
    def rebates(self) -> Decimal:
        return self.total_rebates

    @property
    def turnover(self) -> Decimal:
        return _fraction_decimal(
            Fraction(self.turnover_ticks) * Fraction(self.tick_value)
        )

    @property
    def last_mark_ticks(self) -> Decimal | None:
        return self._last_mark_ticks

    def record_fill(
        self,
        *,
        side: Side,
        quantity: int,
        price_ticks: int,
        timestamp_ns: int | None = None,
        fee: DecimalLike | None = None,
        rebate: DecimalLike | None = None,
        liquidity_role: LiquidityRole | None = None,
        mark_ticks: DecimalLike | None = None,
    ) -> PortfolioSnapshot:
        """Validate and book one fill atomically, returning its marked snapshot."""

        candidate = copy(self)
        result = candidate._record_fill(
            side=side,
            quantity=quantity,
            price_ticks=price_ticks,
            timestamp_ns=timestamp_ns,
            fee=fee,
            rebate=rebate,
            liquidity_role=liquidity_role,
            mark_ticks=mark_ticks,
        )
        self.__dict__.update(candidate.__dict__)
        return result

    def _record_fill(
        self,
        *,
        side: Side,
        quantity: int,
        price_ticks: int,
        timestamp_ns: int | None,
        fee: DecimalLike | None,
        rebate: DecimalLike | None,
        liquidity_role: LiquidityRole | None,
        mark_ticks: DecimalLike | None,
    ) -> PortfolioSnapshot:
        self.assert_invariants()
        if not isinstance(side, Side):
            raise AccountingError("side must be a Side")
        if type(quantity) is not int or quantity <= 0:
            raise AccountingError("quantity must be a positive integer")
        if type(price_ticks) is not int or price_ticks <= 0:
            raise AccountingError("price_ticks must be a positive integer")
        self._validate_timestamp(timestamp_ns)
        if fee is None and rebate is None:
            if self.fee_config is not None:
                if liquidity_role is None:
                    raise AccountingError(
                        "liquidity_role is required when calculating configured costs"
                    )
                costs = calculate_fill_costs(
                    self.fee_config,
                    role=liquidity_role,
                    quantity=quantity,
                    price_ticks=price_ticks,
                    tick_value=self.tick_value,
                )
            else:
                costs = FillCosts()
        elif fee is None or rebate is None:
            raise AccountingError("fee and rebate must be supplied together")
        else:
            costs = FillCosts(_decimal(fee), _decimal(rebate))
        signed_quantity = int(side) * quantity
        previous_inventory = self.inventory
        new_inventory = previous_inventory + signed_quantity
        if previous_inventory == 0 or (previous_inventory > 0) == (signed_quantity > 0):
            basis = self._cost_basis_ticks + signed_quantity * price_ticks
        elif quantity <= abs(previous_inventory):
            basis = self._cost_basis_ticks * Fraction(
                abs(new_inventory), abs(previous_inventory)
            )
        else:
            basis = Fraction(new_inventory * price_ticks)
        self.inventory = new_inventory
        self._cost_basis_ticks = basis
        self.trade_cash_ticks -= signed_quantity * price_ticks
        self._realized_ticks = Fraction(self.trade_cash_ticks) + basis
        self.average_cost_ticks = (
            _fraction_decimal(basis / new_inventory) if new_inventory else None
        )
        self.realized_pnl_ticks = _fraction_decimal(self._realized_ticks)
        self._average_cost_view = self.average_cost_ticks
        self._realized_view = self.realized_pnl_ticks
        self.total_fees = _fraction_decimal(
            Fraction(self.total_fees) + Fraction(costs.fee)
        )
        self.total_rebates = _fraction_decimal(
            Fraction(self.total_rebates) + Fraction(costs.rebate)
        )
        self.turnover_ticks += quantity * price_ticks
        if side is Side.BID:
            self.buy_volume += quantity
        else:
            self.sell_volume += quantity
        self.fill_count += 1
        self._last_timestamp_ns = timestamp_ns
        effective_mark = (
            _decimal(mark_ticks)
            if mark_ticks is not None
            else (
                self._last_mark_ticks
                if self._last_mark_ticks is not None
                else Decimal(price_ticks)
            )
        )
        return self._mark_to_market(effective_mark, timestamp_ns=timestamp_ns)

    def apply_fill(
        self,
        fill: FillLike,
        *,
        mark_ticks: DecimalLike | None = None,
        calculate_costs: bool = False,
    ) -> PortfolioSnapshot:
        """Use supplied exchange economics unless cost calculation is delegated."""

        return self.record_fill(
            side=fill.side,
            quantity=fill.quantity,
            price_ticks=fill.price_ticks,
            timestamp_ns=fill.exchange_fill_timestamp_ns,
            fee=None if calculate_costs else fill.fee,
            rebate=None if calculate_costs else fill.rebate,
            liquidity_role=fill.liquidity_role,
            mark_ticks=mark_ticks,
        )

    @staticmethod
    def _validate_timestamp(timestamp_ns: int | None) -> None:
        if timestamp_ns is not None and (
            type(timestamp_ns) is not int or timestamp_ns < 0
        ):
            raise AccountingError("timestamp_ns must be a nonnegative integer")

    def mark_to_market(
        self,
        mark_ticks: DecimalLike,
        *,
        timestamp_ns: int | None = None,
    ) -> PortfolioSnapshot:
        """Validate a mark and snapshot before committing mark, time or exposure."""

        candidate = copy(self)
        result = candidate._mark_to_market(mark_ticks, timestamp_ns=timestamp_ns)
        self.__dict__.update(candidate.__dict__)
        return result

    def _mark_to_market(
        self,
        mark_ticks: DecimalLike,
        *,
        timestamp_ns: int | None,
    ) -> PortfolioSnapshot:
        mark = _decimal(mark_ticks)
        if mark <= 0 and self.inventory != 0:
            raise AccountingError("mark_ticks must be positive for open inventory")
        self._validate_timestamp(timestamp_ns)
        self.assert_invariants(mark)
        exposure = _fraction_decimal(
            abs(self.inventory) * Fraction(mark) * Fraction(self.tick_value)
        )
        self.peak_gross_exposure = max(self.peak_gross_exposure, exposure)
        self._last_mark_ticks = mark
        if timestamp_ns is not None:
            self._last_timestamp_ns = timestamp_ns
        return self._snapshot(mark, exposure)

    def snapshot(
        self,
        mark_ticks: DecimalLike | None = None,
        *,
        timestamp_ns: int | None = None,
    ) -> PortfolioSnapshot:
        """Return a snapshot; optional mark/time updates are atomic as well."""

        if mark_ticks is not None:
            return self.mark_to_market(mark_ticks, timestamp_ns=timestamp_ns)
        candidate = copy(self)
        candidate._validate_timestamp(timestamp_ns)
        if candidate._last_mark_ticks is None:
            if candidate.inventory != 0:
                raise AccountingError("open inventory requires a mark")
            mark = ZERO
        else:
            mark = candidate._last_mark_ticks
        candidate.assert_invariants(mark)
        if timestamp_ns is not None:
            candidate._last_timestamp_ns = timestamp_ns
        exposure = _fraction_decimal(
            abs(candidate.inventory) * Fraction(mark) * Fraction(candidate.tick_value)
        )
        result = candidate._snapshot(mark, exposure)
        self.__dict__.update(candidate.__dict__)
        return result

    def assert_invariants(self, mark_ticks: DecimalLike | None = None) -> None:
        """Check exact ledger identities and compatible public-view corruption guards."""

        if _decimal(self.total_fees) < 0 or _decimal(self.total_rebates) < 0:
            raise AccountingError("fees and rebates must remain nonnegative")
        if self.inventory == 0 and self.average_cost_ticks is not None:
            raise AccountingError("flat inventory must not retain average cost")
        if self.inventory != 0 and self.average_cost_ticks is None:
            raise AccountingError("open inventory requires average cost")
        if self.buy_volume < 0 or self.sell_volume < 0:
            raise AccountingError("fill volumes must remain nonnegative")
        if self.inventory != self.buy_volume - self.sell_volume:
            raise AccountingError("inventory/fill volume identity failed")
        tolerance = Fraction(_decimal(self.identity_tolerance))
        if self.average_cost_ticks is not None and (
            self._average_cost_view is None
            or abs(self.inventory)
            * abs(
                Fraction(_decimal(self.average_cost_ticks))
                - Fraction(self._average_cost_view)
            )
            > tolerance
        ):
            raise AccountingError("average-cost identity failed: average view changed")
        if (
            abs(
                Fraction(_decimal(self.realized_pnl_ticks))
                - Fraction(self._realized_view)
            )
            > tolerance
        ):
            raise AccountingError("average-cost identity failed: realized view changed")
        if (self.inventory == 0 and self._cost_basis_ticks != 0) or (
            self.inventory != 0 and self._cost_basis_ticks / self.inventory <= 0
        ):
            raise AccountingError("average-cost identity failed: invalid cost basis")
        if (
            Fraction(self.trade_cash_ticks) + self._cost_basis_ticks
            != self._realized_ticks
        ):
            raise AccountingError("average-cost identity failed: exact cost basis")
        mark = self._last_mark_ticks if mark_ticks is None else _decimal(mark_ticks)
        if mark is None:
            return
        exact_gross = Fraction(self.trade_cash_ticks) + self.inventory * Fraction(mark)
        exact_unrealized = self.inventory * Fraction(mark) - self._cost_basis_ticks
        if exact_gross != self._realized_ticks + exact_unrealized:
            raise AccountingError("average-cost identity failed: exact marked ledger")
        exact_net = (
            exact_gross * Fraction(self.tick_value)
            - Fraction(self.total_fees)
            + Fraction(self.total_rebates)
        )
        if exact_net != Fraction(self.cash) + self.inventory * Fraction(
            mark
        ) * Fraction(self.tick_value):
            raise AccountingError("cash/mark net P&L identity failed")

    def _unrealized_ticks(self, mark: Decimal) -> Decimal:
        return _fraction_decimal(
            self.inventory * Fraction(mark) - self._cost_basis_ticks
        )

    def _snapshot(self, mark: Decimal, current_exposure: Decimal) -> PortfolioSnapshot:
        gross_ticks = Fraction(self.trade_cash_ticks) + self.inventory * Fraction(mark)
        unrealized_ticks = self.inventory * Fraction(mark) - self._cost_basis_ticks
        tick = Fraction(self.tick_value)
        gross = gross_ticks * tick
        return PortfolioSnapshot(
            timestamp_ns=self._last_timestamp_ns,
            currency=self.currency,
            tick_value=self.tick_value,
            mark_ticks=mark,
            inventory=self.inventory,
            average_cost_ticks=(
                _fraction_decimal(self._cost_basis_ticks / self.inventory)
                if self.inventory
                else None
            ),
            trade_cash_ticks=self.trade_cash_ticks,
            trade_cash=self.trade_cash,
            cash=self.cash,
            realized_pnl_ticks=_fraction_decimal(self._realized_ticks),
            unrealized_pnl_ticks=_fraction_decimal(unrealized_ticks),
            gross_pnl_ticks=_fraction_decimal(gross_ticks),
            realized_pnl=_fraction_decimal(self._realized_ticks * tick),
            unrealized_pnl=_fraction_decimal(unrealized_ticks * tick),
            gross_pnl=_fraction_decimal(gross),
            fees=self.total_fees,
            rebates=self.total_rebates,
            net_pnl=_fraction_decimal(
                gross - Fraction(self.total_fees) + Fraction(self.total_rebates)
            ),
            turnover_ticks=self.turnover_ticks,
            turnover=self.turnover,
            buy_volume=self.buy_volume,
            sell_volume=self.sell_volume,
            fill_count=self.fill_count,
            current_gross_exposure=current_exposure,
            peak_gross_exposure=self.peak_gross_exposure,
        )


def previous_average_or_raise(value: Decimal | None) -> Decimal:
    """Return a non-null average cost or surface internal state corruption."""

    if value is None:
        raise AccountingError("open inventory is missing average cost")
    return value
