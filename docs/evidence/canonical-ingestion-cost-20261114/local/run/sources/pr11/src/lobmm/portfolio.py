"""Exact fill accounting and marked portfolio state.

Matching prices remain integer ticks.  Currency conversion happens only in
this accounting layer and uses :class:`~decimal.Decimal`, so fees and rebates
are never mixed into binary floating-point arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from lobmm.config import FeeConfig, InstrumentConfig
from lobmm.enums import LiquidityRole, MarkPrice, Side

DecimalLike = Decimal | int | float | str
ZERO = Decimal(0)


class AccountingError(ValueError):
    """Raised when a fill or accounting identity is invalid."""


def _decimal(value: DecimalLike) -> Decimal:
    """Convert configuration/user numeric input without inheriting float noise."""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class FillCosts:
    """Nonnegative cost and income booked for one fill."""

    fee: Decimal = ZERO
    rebate: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.fee < 0:
            raise AccountingError("fee must be nonnegative")
        if self.rebate < 0:
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

    units = Decimal(quantity)
    notional = units * Decimal(price_ticks) * tick
    proportional_fee = config.proportional_fee_rate * notional
    if role is LiquidityRole.MAKER:
        return FillCosts(
            fee=config.maker_fee_per_unit * units + proportional_fee,
            rebate=config.maker_rebate_per_unit * units,
        )
    return FillCosts(
        fee=config.taker_fee_per_unit * units + proportional_fee,
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


class Portfolio:
    """Mutable authoritative accounting ledger.

    The exact identity in tick notional is::

        gross_pnl_ticks = trade_cash_ticks + inventory * mark_ticks

    Fees and rebates are separate currency ledgers and are applied exactly once
    to cash and net P&L.
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
        resolved_tick = (
            instrument.tick_size
            if instrument is not None
            else _decimal(1 if tick_value is None else tick_value)
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

    @property
    def trade_cash(self) -> Decimal:
        return Decimal(self.trade_cash_ticks) * self.tick_value

    @property
    def cash(self) -> Decimal:
        return self.trade_cash - self.total_fees + self.total_rebates

    @property
    def fees(self) -> Decimal:
        return self.total_fees

    @property
    def rebates(self) -> Decimal:
        return self.total_rebates

    @property
    def turnover(self) -> Decimal:
        return Decimal(self.turnover_ticks) * self.tick_value

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
        """Book one fill and return the resulting immutable marked snapshot."""

        if not isinstance(side, Side):
            raise AccountingError("side must be a Side")
        if quantity <= 0:
            raise AccountingError("quantity must be positive")
        if price_ticks <= 0:
            raise AccountingError("price_ticks must be positive")
        if timestamp_ns is not None and timestamp_ns < 0:
            raise AccountingError("timestamp_ns must be nonnegative")

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
        previous_average = self.average_cost_ticks
        new_inventory = previous_inventory + signed_quantity
        price = Decimal(price_ticks)

        if previous_inventory == 0:
            new_average: Decimal | None = price
        elif (previous_inventory > 0) == (signed_quantity > 0):
            existing_notional = Decimal(
                abs(previous_inventory)
            ) * previous_average_or_raise(previous_average)
            added_notional = Decimal(quantity) * price
            new_average = (existing_notional + added_notional) / Decimal(
                abs(new_inventory)
            )
        else:
            average = previous_average_or_raise(previous_average)
            closing_quantity = min(abs(previous_inventory), quantity)
            self.realized_pnl_ticks += (
                Decimal(closing_quantity)
                * (price - average)
                * Decimal(1 if previous_inventory > 0 else -1)
            )
            if new_inventory == 0:
                new_average = None
            elif (new_inventory > 0) == (previous_inventory > 0):
                new_average = average
            else:
                new_average = price

        self.inventory = new_inventory
        self.average_cost_ticks = new_average
        self.trade_cash_ticks -= signed_quantity * price_ticks
        self.total_fees += costs.fee
        self.total_rebates += costs.rebate
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
            else (self._last_mark_ticks if self._last_mark_ticks is not None else price)
        )
        return self.mark_to_market(effective_mark, timestamp_ns=timestamp_ns)

    def apply_fill(
        self,
        fill: FillLike,
        *,
        mark_ticks: DecimalLike | None = None,
        calculate_costs: bool = False,
    ) -> PortfolioSnapshot:
        """Book a structural exchange fill.

        By default the fill's already-calculated economics are authoritative.
        Set ``calculate_costs`` when the exchange intentionally delegates
        economics to this portfolio's configured fee schedule.
        """

        fee: DecimalLike | None = None if calculate_costs else fill.fee
        rebate: DecimalLike | None = None if calculate_costs else fill.rebate
        return self.record_fill(
            side=fill.side,
            quantity=fill.quantity,
            price_ticks=fill.price_ticks,
            timestamp_ns=fill.exchange_fill_timestamp_ns,
            fee=fee,
            rebate=rebate,
            liquidity_role=fill.liquidity_role,
            mark_ticks=mark_ticks,
        )

    def mark_to_market(
        self,
        mark_ticks: DecimalLike,
        *,
        timestamp_ns: int | None = None,
    ) -> PortfolioSnapshot:
        """Update the current mark and return an identity-checked snapshot."""

        mark = _decimal(mark_ticks)
        if mark <= 0 and self.inventory != 0:
            raise AccountingError("mark_ticks must be positive for open inventory")
        if timestamp_ns is not None and timestamp_ns < 0:
            raise AccountingError("timestamp_ns must be nonnegative")
        self._last_mark_ticks = mark
        if timestamp_ns is not None:
            self._last_timestamp_ns = timestamp_ns

        exposure = Decimal(abs(self.inventory)) * mark * self.tick_value
        self.peak_gross_exposure = max(self.peak_gross_exposure, exposure)
        self.assert_invariants(mark)
        return self._snapshot(mark, exposure)

    def snapshot(
        self,
        mark_ticks: DecimalLike | None = None,
        *,
        timestamp_ns: int | None = None,
    ) -> PortfolioSnapshot:
        """Return current state, optionally updating the mark first."""

        if mark_ticks is not None:
            return self.mark_to_market(mark_ticks, timestamp_ns=timestamp_ns)
        if self._last_mark_ticks is None:
            if self.inventory != 0:
                raise AccountingError("open inventory requires a mark")
            mark = ZERO
        else:
            mark = self._last_mark_ticks
        if timestamp_ns is not None:
            if timestamp_ns < 0:
                raise AccountingError("timestamp_ns must be nonnegative")
            self._last_timestamp_ns = timestamp_ns
        exposure = Decimal(abs(self.inventory)) * mark * self.tick_value
        self.assert_invariants(mark)
        return self._snapshot(mark, exposure)

    def assert_invariants(self, mark_ticks: DecimalLike | None = None) -> None:
        """Raise if ledger signs or average-cost identities do not reconcile."""

        if self.total_fees < 0 or self.total_rebates < 0:
            raise AccountingError("fees and rebates must remain nonnegative")
        if self.inventory == 0 and self.average_cost_ticks is not None:
            raise AccountingError("flat inventory must not retain average cost")
        if self.inventory != 0 and self.average_cost_ticks is None:
            raise AccountingError("open inventory requires average cost")
        if self.buy_volume < 0 or self.sell_volume < 0:
            raise AccountingError("fill volumes must remain nonnegative")

        mark = self._last_mark_ticks if mark_ticks is None else _decimal(mark_ticks)
        if mark is None:
            return
        gross_ticks = Decimal(self.trade_cash_ticks) + Decimal(self.inventory) * mark
        unrealized_ticks = self._unrealized_ticks(mark)
        difference = abs(gross_ticks - (self.realized_pnl_ticks + unrealized_ticks))
        if difference > self.identity_tolerance:
            raise AccountingError(
                "average-cost identity failed: "
                f"gross={gross_ticks}, realized+unrealized="
                f"{self.realized_pnl_ticks + unrealized_ticks}"
            )
        gross = gross_ticks * self.tick_value
        net = gross - self.total_fees + self.total_rebates
        marked_cash_identity = (
            self.cash + Decimal(self.inventory) * mark * self.tick_value
        )
        if abs(net - marked_cash_identity) > self.identity_tolerance:
            raise AccountingError("cash/mark net P&L identity failed")

    def _unrealized_ticks(self, mark: Decimal) -> Decimal:
        if self.inventory == 0:
            return ZERO
        average = previous_average_or_raise(self.average_cost_ticks)
        return Decimal(self.inventory) * (mark - average)

    def _snapshot(self, mark: Decimal, current_exposure: Decimal) -> PortfolioSnapshot:
        gross_ticks = Decimal(self.trade_cash_ticks) + Decimal(self.inventory) * mark
        unrealized_ticks = self._unrealized_ticks(mark)
        gross = gross_ticks * self.tick_value
        return PortfolioSnapshot(
            timestamp_ns=self._last_timestamp_ns,
            currency=self.currency,
            tick_value=self.tick_value,
            mark_ticks=mark,
            inventory=self.inventory,
            average_cost_ticks=self.average_cost_ticks,
            trade_cash_ticks=self.trade_cash_ticks,
            trade_cash=self.trade_cash,
            cash=self.cash,
            realized_pnl_ticks=self.realized_pnl_ticks,
            unrealized_pnl_ticks=unrealized_ticks,
            gross_pnl_ticks=gross_ticks,
            realized_pnl=self.realized_pnl_ticks * self.tick_value,
            unrealized_pnl=unrealized_ticks * self.tick_value,
            gross_pnl=gross,
            fees=self.total_fees,
            rebates=self.total_rebates,
            net_pnl=gross - self.total_fees + self.total_rebates,
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
