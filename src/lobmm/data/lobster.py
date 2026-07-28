"""Adapter for paired LOBSTER message and order-book CSV files."""

from __future__ import annotations

import calendar
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

from lobmm.data._adapter_utils import (
    MarketDataAdapterError,
    TableSource,
    assert_uncrossed,
    decimal_value,
    integer_value,
    materialize_events,
    read_csv_source,
    require_positive_decimal,
    scaled_quantity,
    ticks_from_price,
)
from lobmm.data.adapters import BaseL2DataAdapter
from lobmm.enums import EventType, Side

_MESSAGE_COLUMN_COUNT = 6
_ORDERBOOK_COLUMNS_PER_LEVEL = 4
_SECONDS_PER_DAY = Decimal(86_400)
_NS_PER_SECOND = Decimal(1_000_000_000)


@dataclass(frozen=True, slots=True)
class LobsterData:
    """Paired headerless LOBSTER message and order-book files or frames."""

    messages: TableSource
    orderbook: TableSource


type _BookState = dict[Side, dict[int, int]]
type _EventTuple = tuple[int, EventType, Side | None, int, int]


class LobsterCSVAdapter(BaseL2DataAdapter[LobsterData]):
    """Normalize a finite-depth LOBSTER message/order-book pair.

    Each order-book row describes state *after* the corresponding message row.
    The first row is therefore emitted as a reset and snapshot. Later visible
    submissions, cancellations/deletions, and executions retain their causal
    event type when the paired book rows support the change. Any residual
    finite-window changes are reconciled with ``ADD``/``CANCEL`` deltas.
    """

    def __init__(
        self,
        *,
        tick_size: Decimal | str,
        trading_date: date | str,
        levels: int | None = None,
        timezone: str | tzinfo = "America/New_York",
        lobster_price_scale: Decimal | str | int = 10_000,
        quantity_multiplier: Decimal | str | int = 1,
    ) -> None:
        self.tick_size = require_positive_decimal(tick_size, field="tick_size")
        self.trading_date = self._parse_date(trading_date)
        if levels is not None and levels <= 0:
            raise ValueError("levels must be positive when provided")
        self.levels = levels
        self.lobster_price_scale = require_positive_decimal(
            lobster_price_scale,
            field="lobster_price_scale",
        )
        self.quantity_multiplier = require_positive_decimal(
            quantity_multiplier,
            field="quantity_multiplier",
        )
        self.timezone: tzinfo
        if isinstance(timezone, str):
            try:
                self.timezone = ZoneInfo(timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    f"unknown or unavailable timezone: {timezone!r}; "
                    "install the tzdata package on systems without an IANA "
                    "timezone database"
                ) from exc
        elif isinstance(timezone, tzinfo):
            self.timezone = timezone
        else:
            raise TypeError("timezone must be an IANA name or datetime.tzinfo")
        self._midnight_epoch_ns = self._midnight_ns()
        self._last_diagnostics: Mapping[str, int] = MappingProxyType({})

    @property
    def name(self) -> str:
        return "lobster_message_orderbook"

    @property
    def last_diagnostics(self) -> Mapping[str, int]:
        """Counts from the most recent conversion."""

        return self._last_diagnostics

    def to_canonical(self, source: LobsterData) -> pl.DataFrame:
        """Convert a paired LOBSTER sample into canonical replay events."""

        if not isinstance(source, LobsterData):
            raise TypeError("source must be LobsterData")
        messages = read_csv_source(
            source.messages,
            dataset_name="LOBSTER message",
            has_header=False,
        )
        orderbook = read_csv_source(
            source.orderbook,
            dataset_name="LOBSTER orderbook",
            has_header=False,
        )
        self._validate_shapes(messages, orderbook)

        diagnostics: Counter[str] = Counter()
        specs: list[_EventTuple] = []
        state: _BookState = {Side.BID: {}, Side.ASK: {}}
        halted = False
        previous_timestamp_ns: int | None = None

        message_rows = list(messages.iter_rows())
        book_rows = list(orderbook.iter_rows())
        for row_index, (message_row, book_row) in enumerate(
            zip(message_rows, book_rows, strict=True)
        ):
            diagnostics["source_rows"] += 1
            timestamp_ns = self._timestamp_ns(message_row[0], row_index=row_index)
            if (
                previous_timestamp_ns is not None
                and timestamp_ns < previous_timestamp_ns
            ):
                raise MarketDataAdapterError(
                    "LOBSTER message time regressed at row "
                    f"{row_index}: {timestamp_ns} follows {previous_timestamp_ns}"
                )
            target = self._book_state(book_row, row_index=row_index)
            event_code = integer_value(
                message_row[1],
                field="event_type",
                row_index=row_index,
            )
            if event_code not in range(1, 8):
                raise MarketDataAdapterError(
                    f"unsupported LOBSTER event_type={event_code} at row {row_index}"
                )

            if event_code == 7:
                halt_code = integer_value(
                    message_row[4],
                    field="halt_indicator",
                    row_index=row_index,
                )
                halted = self._handle_halt(
                    specs=specs,
                    timestamp_ns=timestamp_ns,
                    halt_code=halt_code,
                    target=target,
                    state=state,
                    halted=halted,
                    diagnostics=diagnostics,
                    row_index=row_index,
                )
                previous_timestamp_ns = timestamp_ns
                continue

            if row_index == 0:
                if event_code in {1, 2, 3, 4}:
                    self._visible_message_details(
                        message_row,
                        row_index=row_index,
                    )
                self._append_reset_snapshot(
                    specs,
                    timestamp_ns=timestamp_ns,
                    target=target,
                    diagnostics=diagnostics,
                )
                state = self._copy_state(target)
                diagnostics["first_message_represented_by_snapshot"] += 1
                previous_timestamp_ns = timestamp_ns
                continue

            if halted:
                diagnostics["messages_while_halted_skipped"] += 1
                previous_timestamp_ns = timestamp_ns
                continue

            self._apply_supported_message(
                specs=specs,
                timestamp_ns=timestamp_ns,
                event_code=event_code,
                message_row=message_row,
                state=state,
                target=target,
                diagnostics=diagnostics,
                row_index=row_index,
            )
            self._reconcile(
                specs,
                timestamp_ns=timestamp_ns,
                state=state,
                target=target,
                diagnostics=diagnostics,
            )
            previous_timestamp_ns = timestamp_ns

        result = materialize_events(specs)
        diagnostics["output_events"] = result.height
        self._last_diagnostics = MappingProxyType(dict(diagnostics))
        return result

    def _validate_shapes(
        self,
        messages: pl.DataFrame,
        orderbook: pl.DataFrame,
    ) -> None:
        if messages.width != _MESSAGE_COLUMN_COUNT:
            raise MarketDataAdapterError(
                "LOBSTER message file must have exactly 6 columns "
                f"(found {messages.width})"
            )
        if orderbook.width == 0 or orderbook.width % _ORDERBOOK_COLUMNS_PER_LEVEL:
            raise MarketDataAdapterError(
                "LOBSTER orderbook file must have 4 columns per requested level "
                f"(found {orderbook.width})"
            )
        inferred_levels = orderbook.width // _ORDERBOOK_COLUMNS_PER_LEVEL
        if self.levels is not None and inferred_levels != self.levels:
            raise MarketDataAdapterError(
                f"LOBSTER orderbook has {inferred_levels} levels, "
                f"configured levels={self.levels}"
            )
        if messages.height != orderbook.height:
            raise MarketDataAdapterError(
                "LOBSTER message/orderbook row counts differ: "
                f"{messages.height} != {orderbook.height}"
            )
        if messages.is_empty():
            raise MarketDataAdapterError("LOBSTER files are empty")

    def _timestamp_ns(self, value: object, *, row_index: int) -> int:
        seconds = decimal_value(value, field="time", row_index=row_index)
        if seconds < 0 or seconds >= _SECONDS_PER_DAY:
            raise MarketDataAdapterError(
                f"time={seconds} must be in [0, 86400) at row {row_index}"
            )
        offset_ns = seconds * _NS_PER_SECOND
        integral = offset_ns.to_integral_value()
        if offset_ns != integral:
            raise MarketDataAdapterError(
                f"time={seconds} has precision finer than one nanosecond "
                f"at row {row_index}"
            )
        return self._midnight_epoch_ns + int(integral)

    def _book_state(
        self,
        row: Sequence[object],
        *,
        row_index: int,
    ) -> _BookState:
        bids: dict[int, int] = {}
        asks: dict[int, int] = {}
        ordered_bids: list[int] = []
        ordered_asks: list[int] = []

        for offset in range(0, len(row), _ORDERBOOK_COLUMNS_PER_LEVEL):
            ask_price_raw, ask_size_raw, bid_price_raw, bid_size_raw = row[
                offset : offset + _ORDERBOOK_COLUMNS_PER_LEVEL
            ]
            self._add_level(
                asks,
                ordered_asks,
                price_raw=ask_price_raw,
                size_raw=ask_size_raw,
                side=Side.ASK,
                row_index=row_index,
            )
            self._add_level(
                bids,
                ordered_bids,
                price_raw=bid_price_raw,
                size_raw=bid_size_raw,
                side=Side.BID,
                row_index=row_index,
            )

        if ordered_asks != sorted(ordered_asks):
            raise MarketDataAdapterError(
                f"LOBSTER asks are not best-to-worst at row {row_index}"
            )
        if ordered_bids != sorted(ordered_bids, reverse=True):
            raise MarketDataAdapterError(
                f"LOBSTER bids are not best-to-worst at row {row_index}"
            )
        assert_uncrossed(
            bids,
            asks,
            context=f"LOBSTER orderbook row {row_index}",
        )
        return {Side.BID: bids, Side.ASK: asks}

    def _add_level(
        self,
        levels: dict[int, int],
        ordered_prices: list[int],
        *,
        price_raw: object,
        size_raw: object,
        side: Side,
        row_index: int,
    ) -> None:
        quantity = scaled_quantity(
            size_raw,
            multiplier=self.quantity_multiplier,
            field=f"{side.name.lower()}_size",
            row_index=row_index,
            allow_zero=True,
        )
        if not quantity:
            return
        raw_price = decimal_value(
            price_raw,
            field=f"{side.name.lower()}_price",
            row_index=row_index,
        )
        price_ticks = ticks_from_price(
            raw_price / self.lobster_price_scale,
            tick_size=self.tick_size,
            field=f"{side.name.lower()}_price",
            row_index=row_index,
        )
        if price_ticks in levels:
            raise MarketDataAdapterError(
                f"duplicate {side.name} price {price_ticks} at row {row_index}"
            )
        levels[price_ticks] = quantity
        ordered_prices.append(price_ticks)

    def _handle_halt(
        self,
        *,
        specs: list[_EventTuple],
        timestamp_ns: int,
        halt_code: int,
        target: _BookState,
        state: _BookState,
        halted: bool,
        diagnostics: Counter[str],
        row_index: int,
    ) -> bool:
        if halt_code == -1:
            if not halted:
                specs.append((timestamp_ns, EventType.RESET, None, 0, 0))
                diagnostics["halt_resets"] += 1
                state[Side.BID].clear()
                state[Side.ASK].clear()
            return True
        if halt_code in {0, 1}:
            if halted:
                self._append_reset_snapshot(
                    specs,
                    timestamp_ns=timestamp_ns,
                    target=target,
                    diagnostics=diagnostics,
                )
                state[Side.BID] = dict(target[Side.BID])
                state[Side.ASK] = dict(target[Side.ASK])
                diagnostics["halt_resumes"] += 1
                return False
            diagnostics["redundant_halt_resume_indicators"] += 1
            return False
        raise MarketDataAdapterError(
            f"invalid LOBSTER halt indicator {halt_code} at row {row_index}"
        )

    def _apply_supported_message(
        self,
        *,
        specs: list[_EventTuple],
        timestamp_ns: int,
        event_code: int,
        message_row: Sequence[object],
        state: _BookState,
        target: _BookState,
        diagnostics: Counter[str],
        row_index: int,
    ) -> None:
        if event_code not in {1, 2, 3, 4}:
            if event_code == 5:
                diagnostics["hidden_executions_not_emitted"] += 1
            elif event_code == 6:
                diagnostics["cross_trades_not_emitted"] += 1
            return

        side, price_ticks, quantity = self._visible_message_details(
            message_row,
            row_index=row_index,
        )

        before = state[side].get(price_ticks, 0)
        after = target[side].get(price_ticks, 0)
        if event_code == 1:
            supported = min(quantity, max(after - before, 0))
            event_type = EventType.ADD
        else:
            supported = min(quantity, max(before - after, 0))
            event_type = EventType.TRADE if event_code == 4 else EventType.CANCEL

        if not supported:
            diagnostics["messages_outside_visible_depth"] += 1
            return
        specs.append((timestamp_ns, event_type, side, price_ticks, supported))
        self._apply_to_state(
            state,
            side=side,
            price_ticks=price_ticks,
            event_type=event_type,
            quantity=supported,
        )
        diagnostics[f"causal_{event_type.value.lower()}_events"] += 1
        if supported < quantity:
            diagnostics["partially_visible_message_quantity"] += quantity - supported

    def _visible_message_details(
        self,
        message_row: Sequence[object],
        *,
        row_index: int,
    ) -> tuple[Side, int, int]:
        direction = integer_value(
            message_row[5],
            field="direction",
            row_index=row_index,
        )
        try:
            side = Side(direction)
        except ValueError as exc:
            raise MarketDataAdapterError(
                f"direction must be -1 or 1 at row {row_index}, found {direction}"
            ) from exc
        raw_price = decimal_value(
            message_row[4],
            field="price",
            row_index=row_index,
        )
        price_ticks = ticks_from_price(
            raw_price / self.lobster_price_scale,
            tick_size=self.tick_size,
            field="price",
            row_index=row_index,
        )
        quantity = scaled_quantity(
            message_row[3],
            multiplier=self.quantity_multiplier,
            field="size",
            row_index=row_index,
            allow_zero=False,
        )
        return side, price_ticks, quantity

    @staticmethod
    def _reconcile(
        specs: list[_EventTuple],
        *,
        timestamp_ns: int,
        state: _BookState,
        target: _BookState,
        diagnostics: Counter[str],
    ) -> None:
        side_order = (Side.BID, Side.ASK)
        for side in side_order:
            prices = sorted(
                set(state[side]) | set(target[side]),
                reverse=side is Side.BID,
            )
            for price_ticks in prices:
                before = state[side].get(price_ticks, 0)
                after = target[side].get(price_ticks, 0)
                if after >= before:
                    continue
                quantity = before - after
                specs.append(
                    (
                        timestamp_ns,
                        EventType.CANCEL,
                        side,
                        price_ticks,
                        quantity,
                    )
                )
                LobsterCSVAdapter._apply_to_state(
                    state,
                    side=side,
                    price_ticks=price_ticks,
                    event_type=EventType.CANCEL,
                    quantity=quantity,
                )
                diagnostics["window_reconciliation_cancels"] += 1

        for side in side_order:
            prices = sorted(
                set(state[side]) | set(target[side]),
                reverse=side is Side.BID,
            )
            for price_ticks in prices:
                before = state[side].get(price_ticks, 0)
                after = target[side].get(price_ticks, 0)
                if after <= before:
                    continue
                quantity = after - before
                specs.append(
                    (
                        timestamp_ns,
                        EventType.ADD,
                        side,
                        price_ticks,
                        quantity,
                    )
                )
                LobsterCSVAdapter._apply_to_state(
                    state,
                    side=side,
                    price_ticks=price_ticks,
                    event_type=EventType.ADD,
                    quantity=quantity,
                )
                diagnostics["window_reconciliation_adds"] += 1

    @staticmethod
    def _apply_to_state(
        state: _BookState,
        *,
        side: Side,
        price_ticks: int,
        event_type: EventType,
        quantity: int,
    ) -> None:
        before = state[side].get(price_ticks, 0)
        if event_type is EventType.ADD:
            state[side][price_ticks] = before + quantity
            return
        after = before - quantity
        if after:
            state[side][price_ticks] = after
        else:
            state[side].pop(price_ticks, None)

    @staticmethod
    def _append_reset_snapshot(
        specs: list[_EventTuple],
        *,
        timestamp_ns: int,
        target: _BookState,
        diagnostics: Counter[str],
    ) -> None:
        specs.append((timestamp_ns, EventType.RESET, None, 0, 0))
        diagnostics["snapshot_resets"] += 1
        for side in (Side.BID, Side.ASK):
            prices = sorted(target[side], reverse=side is Side.BID)
            for price_ticks in prices:
                specs.append(
                    (
                        timestamp_ns,
                        EventType.SNAPSHOT,
                        side,
                        price_ticks,
                        target[side][price_ticks],
                    )
                )
                diagnostics["snapshot_levels"] += 1

    @staticmethod
    def _copy_state(state: _BookState) -> _BookState:
        return {
            Side.BID: dict(state[Side.BID]),
            Side.ASK: dict(state[Side.ASK]),
        }

    @staticmethod
    def _parse_date(value: date | str) -> date:
        if isinstance(value, datetime):
            raise ValueError("trading_date must be a date, not a datetime")
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("trading_date must be an ISO date (YYYY-MM-DD)") from exc

    def _midnight_ns(self) -> int:
        local_midnight = datetime.combine(
            self.trading_date,
            time.min,
            tzinfo=self.timezone,
        )
        utc_midnight = local_midnight.astimezone(UTC)
        return calendar.timegm(utc_midnight.timetuple()) * 1_000_000_000


# Brand-capitalized aliases are convenient at call sites and documentation.
LOBSTERData = LobsterData
LOBSTERCSVAdapter = LobsterCSVAdapter
