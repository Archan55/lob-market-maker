"""Adapter for normalized Tardis ``incremental_book_L2`` and trade CSVs."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

import polars as pl

from lobmm.data._adapter_utils import (
    MarketDataAdapterError,
    TableSource,
    assert_uncrossed,
    integer_value,
    materialize_events,
    read_csv_source,
    require_positive_decimal,
    scaled_quantity,
    ticks_from_price,
)
from lobmm.data.adapters import BaseL2DataAdapter
from lobmm.enums import EventType, Side

type TardisTimestampSource = Literal["local_timestamp", "timestamp"]

_BOOK_COLUMNS = {
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "is_snapshot",
    "side",
    "price",
    "amount",
}
_TRADE_COLUMNS = {
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "id",
    "side",
    "price",
    "amount",
}


@dataclass(frozen=True, slots=True)
class TardisData:
    """External Tardis files or frames for one exchange and one symbol."""

    book: TableSource
    trades: TableSource | None = None


@dataclass(slots=True)
class _EventSpec:
    timestamp_ns: int
    event_type: EventType
    side: Side | None
    price_ticks: int
    quantity: int

    def as_tuple(self) -> tuple[int, EventType, Side | None, int, int]:
        return (
            self.timestamp_ns,
            self.event_type,
            self.side,
            self.price_ticks,
            self.quantity,
        )


@dataclass(slots=True)
class _Reduction:
    timestamp_ns: int
    side: Side
    price_ticks: int
    quantity: int
    matched_trade_quantity: int = 0


@dataclass(frozen=True, slots=True)
class _Trade:
    timestamp_ns: int
    side: Side
    price_ticks: int
    quantity: int


type _Token = _EventSpec | _Reduction


class TardisCSVAdapter(BaseL2DataAdapter[TardisData]):
    """Normalize Tardis L2 updates, conservatively reconciling trade prints.

    Tardis book ``amount`` values are absolute quantities, not deltas. The
    adapter reconstructs source state and emits only the difference. A trade
    print becomes a canonical ``TRADE`` only when compatible displayed depth
    decreased at the same resting-side price within ``trade_match_window_ns``.
    Remaining decreases are emitted as ``CANCEL``. This preserves exact book
    state and prevents a trade plus its book update from consuming depth twice.
    """

    def __init__(
        self,
        *,
        tick_size: Decimal | str,
        quantity_multiplier: Decimal | str | int = 1,
        timestamp_source: TardisTimestampSource = "local_timestamp",
        trade_match_window_ns: int = 1_000_000,
    ) -> None:
        self.tick_size = require_positive_decimal(tick_size, field="tick_size")
        self.quantity_multiplier = require_positive_decimal(
            quantity_multiplier,
            field="quantity_multiplier",
        )
        if timestamp_source not in {"local_timestamp", "timestamp"}:
            raise ValueError(
                "timestamp_source must be 'local_timestamp' or 'timestamp'"
            )
        if trade_match_window_ns < 0:
            raise ValueError("trade_match_window_ns must be nonnegative")
        self.timestamp_source = timestamp_source
        self.trade_match_window_ns = trade_match_window_ns
        self._last_diagnostics: Mapping[str, int] = MappingProxyType({})
        self._last_instrument: tuple[str, str] | None = None

    @property
    def name(self) -> str:
        return "tardis_incremental_book_l2"

    @property
    def last_diagnostics(self) -> Mapping[str, int]:
        """Counts from the most recent conversion."""

        return self._last_diagnostics

    @property
    def last_instrument(self) -> tuple[str, str] | None:
        """Return the normalized ``(exchange, symbol)`` from the latest input."""

        return self._last_instrument

    def to_canonical(self, source: TardisData) -> pl.DataFrame:
        """Convert one Tardis instrument into canonical replay events."""

        if not isinstance(source, TardisData):
            raise TypeError("source must be TardisData")

        self._last_instrument = None
        diagnostics: Counter[str] = Counter()
        book = read_csv_source(
            source.book,
            dataset_name="Tardis incremental_book_L2",
            has_header=True,
        )
        self._require_columns(book, _BOOK_COLUMNS, dataset="incremental_book_L2")
        tokens, reductions, instrument = self._book_tokens(book, diagnostics)

        if source.trades is not None:
            trades = read_csv_source(
                source.trades,
                dataset_name="Tardis trades",
                has_header=True,
            )
            self._require_columns(trades, _TRADE_COLUMNS, dataset="trades")
            parsed_trades = self._parse_trades(
                trades,
                expected_instrument=instrument,
                diagnostics=diagnostics,
            )
            self._match_trades(parsed_trades, reductions, diagnostics)

        specs: list[tuple[int, EventType, Side | None, int, int]] = []
        for token in tokens:
            if isinstance(token, _EventSpec):
                specs.append(token.as_tuple())
                continue
            if token.matched_trade_quantity:
                specs.append(
                    (
                        token.timestamp_ns,
                        EventType.TRADE,
                        token.side,
                        token.price_ticks,
                        token.matched_trade_quantity,
                    )
                )
                diagnostics["trade_events"] += 1
            cancellation = token.quantity - token.matched_trade_quantity
            if cancellation:
                specs.append(
                    (
                        token.timestamp_ns,
                        EventType.CANCEL,
                        token.side,
                        token.price_ticks,
                        cancellation,
                    )
                )
                diagnostics["cancel_events"] += 1

        result = materialize_events(specs)
        diagnostics["output_events"] = result.height
        self._last_diagnostics = MappingProxyType(dict(diagnostics))
        self._last_instrument = instrument
        return result

    @staticmethod
    def _require_columns(
        frame: pl.DataFrame,
        required: set[str],
        *,
        dataset: str,
    ) -> None:
        missing = required - set(frame.columns)
        if missing:
            raise MarketDataAdapterError(
                f"Tardis {dataset} is missing column(s): " + ", ".join(sorted(missing))
            )

    def _book_tokens(
        self,
        frame: pl.DataFrame,
        diagnostics: Counter[str],
    ) -> tuple[list[_Token], list[_Reduction], tuple[str, str]]:
        if frame.is_empty():
            raise MarketDataAdapterError("Tardis incremental_book_L2 is empty")

        tokens: list[_Token] = []
        reductions: list[_Reduction] = []
        levels: dict[Side, dict[int, int]] = {Side.BID: {}, Side.ASK: {}}
        snapshot_keys: set[tuple[Side, int]] = set()
        instrument: tuple[str, str] | None = None
        seen_snapshot = False
        previous_is_snapshot = False
        previous_selected_timestamp: int | None = None
        previous_local_timestamp_us: int | None = None
        current_message_local_timestamp_us: int | None = None

        for row_index, row in enumerate(frame.iter_rows(named=True)):
            diagnostics["book_rows"] += 1
            row_instrument = self._instrument(row, row_index=row_index)
            if instrument is None:
                instrument = row_instrument
            elif row_instrument != instrument:
                raise MarketDataAdapterError(
                    "Tardis incremental_book_L2 contains multiple instruments: "
                    f"{instrument!r} and {row_instrument!r} at row {row_index}"
                )

            local_timestamp_us = integer_value(
                row["local_timestamp"],
                field="local_timestamp",
                row_index=row_index,
                nonnegative=True,
            )
            exchange_timestamp_us = integer_value(
                row["timestamp"],
                field="timestamp",
                row_index=row_index,
                nonnegative=True,
            )
            if (
                previous_local_timestamp_us is not None
                and local_timestamp_us < previous_local_timestamp_us
            ):
                raise MarketDataAdapterError(
                    "Tardis local_timestamp regressed at row "
                    f"{row_index}: {local_timestamp_us} follows "
                    f"{previous_local_timestamp_us}"
                )
            selected_timestamp_us = (
                local_timestamp_us
                if self.timestamp_source == "local_timestamp"
                else exchange_timestamp_us
            )
            timestamp_ns = selected_timestamp_us * 1_000
            if (
                previous_selected_timestamp is not None
                and timestamp_ns < previous_selected_timestamp
            ):
                raise MarketDataAdapterError(
                    f"Tardis {self.timestamp_source} regressed at row {row_index}: "
                    f"{timestamp_ns} follows {previous_selected_timestamp}"
                )

            if (
                current_message_local_timestamp_us is not None
                and local_timestamp_us != current_message_local_timestamp_us
                and seen_snapshot
            ):
                assert_uncrossed(
                    levels[Side.BID],
                    levels[Side.ASK],
                    context=(
                        "Tardis book after local_timestamp "
                        f"{current_message_local_timestamp_us}"
                    ),
                )
            current_message_local_timestamp_us = local_timestamp_us

            is_snapshot = self._snapshot_flag(row["is_snapshot"], row_index)
            side = self._book_side(row["side"], row_index=row_index)
            price_ticks = ticks_from_price(
                row["price"],
                tick_size=self.tick_size,
                field="price",
                row_index=row_index,
            )
            amount = scaled_quantity(
                row["amount"],
                multiplier=self.quantity_multiplier,
                field="amount",
                row_index=row_index,
                allow_zero=True,
            )

            if not seen_snapshot and not is_snapshot:
                diagnostics["pre_snapshot_rows_skipped"] += 1
            elif is_snapshot:
                if not previous_is_snapshot:
                    levels[Side.BID].clear()
                    levels[Side.ASK].clear()
                    snapshot_keys.clear()
                    tokens.append(
                        _EventSpec(
                            timestamp_ns,
                            EventType.RESET,
                            None,
                            0,
                            0,
                        )
                    )
                    diagnostics["resets"] += 1
                    seen_snapshot = True

                key = (side, price_ticks)
                if key in snapshot_keys:
                    raise MarketDataAdapterError(
                        "duplicate Tardis snapshot level at row "
                        f"{row_index}: {side.name} {price_ticks}"
                    )
                snapshot_keys.add(key)
                if amount:
                    levels[side][price_ticks] = amount
                    tokens.append(
                        _EventSpec(
                            timestamp_ns,
                            EventType.SNAPSHOT,
                            side,
                            price_ticks,
                            amount,
                        )
                    )
                    diagnostics["snapshot_levels"] += 1
                else:
                    diagnostics["zero_snapshot_levels_skipped"] += 1
            else:
                before = levels[side].get(price_ticks, 0)
                if amount > before:
                    tokens.append(
                        _EventSpec(
                            timestamp_ns,
                            EventType.ADD,
                            side,
                            price_ticks,
                            amount - before,
                        )
                    )
                    diagnostics["add_events"] += 1
                elif amount < before:
                    reduction = _Reduction(
                        timestamp_ns,
                        side,
                        price_ticks,
                        before - amount,
                    )
                    tokens.append(reduction)
                    reductions.append(reduction)
                else:
                    diagnostics["unchanged_book_rows_skipped"] += 1

                if amount:
                    levels[side][price_ticks] = amount
                else:
                    levels[side].pop(price_ticks, None)

            previous_is_snapshot = is_snapshot
            previous_selected_timestamp = timestamp_ns
            previous_local_timestamp_us = local_timestamp_us

        if not seen_snapshot:
            raise MarketDataAdapterError(
                "Tardis incremental_book_L2 contains no snapshot; "
                "updates before the first snapshot cannot be reconstructed"
            )
        assert instrument is not None
        assert_uncrossed(
            levels[Side.BID],
            levels[Side.ASK],
            context="final Tardis book",
        )
        return tokens, reductions, instrument

    def _parse_trades(
        self,
        frame: pl.DataFrame,
        *,
        expected_instrument: tuple[str, str],
        diagnostics: Counter[str],
    ) -> list[_Trade]:
        trades: list[_Trade] = []
        previous_timestamp_ns: int | None = None
        previous_local_timestamp_us: int | None = None
        for row_index, row in enumerate(frame.iter_rows(named=True)):
            diagnostics["trade_rows"] += 1
            instrument = self._instrument(row, row_index=row_index)
            if instrument != expected_instrument:
                raise MarketDataAdapterError(
                    "Tardis trades instrument does not match book: "
                    f"{instrument!r} != {expected_instrument!r} at row {row_index}"
                )

            local_timestamp_us = integer_value(
                row["local_timestamp"],
                field="local_timestamp",
                row_index=row_index,
                nonnegative=True,
            )
            exchange_timestamp_us = integer_value(
                row["timestamp"],
                field="timestamp",
                row_index=row_index,
                nonnegative=True,
            )
            if (
                previous_local_timestamp_us is not None
                and local_timestamp_us < previous_local_timestamp_us
            ):
                raise MarketDataAdapterError(
                    "Tardis trade local_timestamp regressed at row "
                    f"{row_index}: {local_timestamp_us} follows "
                    f"{previous_local_timestamp_us}"
                )
            selected_timestamp_us = (
                local_timestamp_us
                if self.timestamp_source == "local_timestamp"
                else exchange_timestamp_us
            )
            timestamp_ns = selected_timestamp_us * 1_000
            if (
                previous_timestamp_ns is not None
                and timestamp_ns < previous_timestamp_ns
            ):
                raise MarketDataAdapterError(
                    f"Tardis trade {self.timestamp_source} regressed at row "
                    f"{row_index}: {timestamp_ns} follows {previous_timestamp_ns}"
                )

            aggressor = str(row["side"]).strip().lower()
            if aggressor == "buy":
                resting_side = Side.ASK
            elif aggressor == "sell":
                resting_side = Side.BID
            elif aggressor == "unknown":
                diagnostics["unknown_side_trades_skipped"] += 1
                previous_timestamp_ns = timestamp_ns
                previous_local_timestamp_us = local_timestamp_us
                continue
            else:
                raise MarketDataAdapterError(
                    f"invalid Tardis trade side {row['side']!r} at row {row_index}"
                )

            trades.append(
                _Trade(
                    timestamp_ns,
                    resting_side,
                    ticks_from_price(
                        row["price"],
                        tick_size=self.tick_size,
                        field="price",
                        row_index=row_index,
                    ),
                    scaled_quantity(
                        row["amount"],
                        multiplier=self.quantity_multiplier,
                        field="amount",
                        row_index=row_index,
                        allow_zero=False,
                    ),
                )
            )
            previous_timestamp_ns = timestamp_ns
            previous_local_timestamp_us = local_timestamp_us
        return trades

    def _match_trades(
        self,
        trades: list[_Trade],
        reductions: list[_Reduction],
        diagnostics: Counter[str],
    ) -> None:
        reductions_by_level: dict[tuple[Side, int], list[_Reduction]] = {}
        for reduction in reductions:
            reductions_by_level.setdefault(
                (reduction.side, reduction.price_ticks),
                [],
            ).append(reduction)
        timestamps_by_level = {
            key: [reduction.timestamp_ns for reduction in level_reductions]
            for key, level_reductions in reductions_by_level.items()
        }

        window = self.trade_match_window_ns
        for trade in trades:
            key = (trade.side, trade.price_ticks)
            candidates = reductions_by_level.get(key, [])
            timestamps = timestamps_by_level.get(key, [])
            left = bisect_left(timestamps, trade.timestamp_ns - window)
            right = bisect_right(timestamps, trade.timestamp_ns + window)
            candidate_slice = sorted(
                candidates[left:right],
                key=lambda item: (
                    abs(item.timestamp_ns - trade.timestamp_ns),
                    item.timestamp_ns,
                ),
            )

            remaining = trade.quantity
            for reduction in candidate_slice:
                available = reduction.quantity - reduction.matched_trade_quantity
                matched = min(remaining, available)
                if not matched:
                    continue
                reduction.matched_trade_quantity += matched
                remaining -= matched
                diagnostics["matched_trade_quantity"] += matched
                if not remaining:
                    break
            if remaining:
                diagnostics["unmatched_trade_quantity"] += remaining
                diagnostics["partially_or_unmatched_trade_rows"] += 1

    @staticmethod
    def _instrument(
        row: Mapping[str, object],
        *,
        row_index: int,
    ) -> tuple[str, str]:
        exchange_value = row["exchange"]
        symbol_value = row["symbol"]
        if exchange_value is None or not str(exchange_value).strip():
            raise MarketDataAdapterError(f"exchange is empty at row {row_index}")
        if symbol_value is None or not str(symbol_value).strip():
            raise MarketDataAdapterError(f"symbol is empty at row {row_index}")
        return str(exchange_value).strip(), str(symbol_value).strip()

    @staticmethod
    def _snapshot_flag(value: object, row_index: int) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
        raise MarketDataAdapterError(
            f"invalid is_snapshot value {value!r} at row {row_index}"
        )

    @staticmethod
    def _book_side(value: object, *, row_index: int) -> Side:
        normalized = str(value).strip().lower()
        if normalized == "bid":
            return Side.BID
        if normalized == "ask":
            return Side.ASK
        raise MarketDataAdapterError(
            f"invalid Tardis book side {value!r} at row {row_index}"
        )
