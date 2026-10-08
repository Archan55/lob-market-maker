"""Versioned full-loop research harness; matching and scheduling stay unmodified.

Oracles use hand-derived FIFO boundaries and scalar conservation, never Exchange
or QueueModel. Scoped observers call the original methods exactly once and copy
state after each scheduler handler. This suite is intentionally single-process.
"""

from __future__ import annotations

import json
import random
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import BacktestResult, run_backtest
from lobmm.channels import ChannelDelivery
from lobmm.config import AppConfig, load_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import load_events, write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, QueueAllocation, Side
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest
from lobmm.portfolio import Portfolio
from lobmm.scheduler import ScheduledEvent, Scheduler
from lobmm.strategy_runtime import StrategyRuntime

pytestmark = pytest.mark.integration
VERSION = "latency-stress-v1"
BASE_CONFIG = Path("configs/latency_stress_v1.yaml")
CANONICAL_TAPE = Path("data/fixtures/queue_cancellation.csv")
ZERO = Decimal(0)
TOLERANCE = Decimal("1e-12")
POLICY_AHEAD = {
    QueueAllocation.BACK_OF_QUEUE: 90,  # 100 - (60 - 50)
    QueueAllocation.PRO_RATA: 60,  # 100 - 60 * 100 / 150
    QueueAllocation.FRONT_OF_QUEUE: 40,  # 100 - 60
}


@dataclass(frozen=True)
class ExpectedFill:
    sequence: int
    timestamp: int
    side: int
    quantity: int


@dataclass(frozen=True)
class Scenario:
    name: str
    family: str
    allocation: QueueAllocation
    events: tuple[MarketEvent, ...]
    fills: tuple[ExpectedFill, ...]
    entry_ns: int = 150_000
    cancel_ns: int = 150_000
    report_ns: int = 100_000
    cutoff_ns: int = 3_000_000
    cancel_send_ns: int = 3_100_000
    offset_ns: int | None = None
    seed: int = 7


def _tape(rows: list[tuple[int, str, int, int, int]]) -> tuple[MarketEvent, ...]:
    return tuple(
        MarketEvent(t, sequence, EventType(kind), Side(side), price, quantity)
        for sequence, (t, kind, side, price, quantity) in enumerate(rows, 1)
    )


def _scenarios() -> tuple[Scenario, ...]:
    canonical = tuple(load_events(CANONICAL_TAPE))
    cases: list[Scenario] = []
    # Initial observed book arrives at 100 us. Sweep ONLY entry latency.
    for allocation, ahead in POLICY_AHEAD.items():
        for offset in (-1, 0, 1):
            quantity = min(10, max(0, 65 - ahead)) if offset < 0 else 0
            cases.append(
                Scenario(
                    f"add-arrival__{allocation}__{offset:+d}",
                    "add-arrival",
                    allocation,
                    canonical,
                    tuple(
                        ExpectedFill(7 + index, 3_000_000, side, quantity)
                        for index, side in enumerate((1, -1))
                        if quantity
                    ),
                    entry_ns=900_000 + offset,
                    offset_ns=offset,
                )
            )
    # Quotes at 99/101 initially see zero external depth (book is 98/102).
    # Historical ADD(10) then TRADE(5) at T: own-before-add gets five;
    # own-at/after-T sees the historical events first and gets zero.
    discriminator = _tape(
        [
            (0, "SNAPSHOT", 1, 98, 100),
            (0, "SNAPSHOT", -1, 102, 100),
            (1_000_000, "ADD", 1, 99, 10),
            (1_000_000, "TRADE", 1, 99, 5),
            (1_000_000, "ADD", -1, 101, 10),
            (1_000_000, "TRADE", -1, 101, 5),
            (5_000_000, "ADD", 1, 97, 1),
        ]
    )
    for allocation in QueueAllocation:
        for offset in (-1, 0, 1):
            cases.append(
                Scenario(
                    f"add-trade-arrival__{allocation}__{offset:+d}",
                    "add-trade-arrival",
                    allocation,
                    discriminator,
                    (
                        ExpectedFill(4, 1_000_000, 1, 5),
                        ExpectedFill(6, 1_000_000, -1, 5),
                    )
                    if offset < 0
                    else (),
                    entry_ns=900_000 + offset,
                    cutoff_ns=1_000_000,
                    # Before-T acceptance is reported one ns before the
                    # market-data wake and is already past the quote cutoff.
                    cancel_send_ns=1_100_000 + min(offset, 0),
                    offset_ns=offset,
                )
            )
    for allocation, ahead in POLICY_AHEAD.items():
        for offset in (-1, 0, 1):
            quantity = min(10, max(0, 65 - ahead)) if offset >= 0 else 0
            cases.append(
                Scenario(
                    f"trade-cancel__{allocation}__{offset:+d}",
                    "trade-cancel",
                    allocation,
                    canonical,
                    tuple(
                        ExpectedFill(7 + index, 3_000_000, side, quantity)
                        for index, side in enumerate((1, -1))
                        if quantity
                    ),
                    cancel_ns=900_000 + offset,
                    cutoff_ns=2_100_000,
                    cancel_send_ns=2_100_000,
                    offset_ns=offset,
                )
            )
            cases.append(
                Scenario(
                    f"add-cancel__{allocation}__{offset:+d}",
                    "add-cancel",
                    allocation,
                    canonical,
                    (),
                    cancel_ns=650_000 + offset,
                    cutoff_ns=350_000,
                    cancel_send_ns=350_000,
                    offset_ns=offset,
                )
            )
    # Split own partials: primary side 3+2, opposite side 2+2, residual +/-1.
    # Cancels arrive at 3.65 ms; raw best prices later move to 100/104.
    for primary in (1, -1):
        first = (63, 62) if primary == 1 else (62, 63)
        split = _tape(
            [
                (
                    e.timestamp_ns,
                    e.event_type.value,
                    int(e.side),
                    e.price_ticks,
                    e.quantity,
                )
                for e in canonical[:6]
                if e.side is not None
            ]
            + [
                (3_000_000, "TRADE", 1, 99, first[0]),
                (3_000_000, "TRADE", -1, 101, first[1]),
                (3_200_000, "TRADE", 1, 99, 2),
                (3_200_000, "TRADE", -1, 101, 2),
                (3_400_000, "ADD", 1, 96, 1),
                (4_000_000, "CANCEL", 1, 99, 90 - first[0] - 2),
                (4_000_000, "ADD", 1, 100, 100),
                (4_000_000, "CANCEL", -1, 101, 90 - first[1] - 2),
                (4_000_000, "ADD", -1, 104, 100),
                (5_000_000, "ADD", 1, 95, 1),
            ]
        )
        for report_ns in (0, 100_000, 2_500_000):
            cases.append(
                Scenario(
                    f"split-residual__{primary:+d}__report{report_ns}",
                    "split-residual",
                    QueueAllocation.PRO_RATA,
                    split,
                    (
                        ExpectedFill(7, 3_000_000, 1, first[0] - 60),
                        ExpectedFill(8, 3_000_000, -1, first[1] - 60),
                        ExpectedFill(9, 3_200_000, 1, 2),
                        ExpectedFill(10, 3_200_000, -1, 2),
                    ),
                    report_ns=report_ns,
                    cutoff_ns=3_400_000,
                    cancel_send_ns=3_500_000,
                )
            )
    # Independently seeded trade partitions, not random queue-engine outputs.
    for seed in range(8):
        rng = random.Random(seed)
        rows = [
            (e.timestamp_ns, e.event_type.value, int(e.side), e.price_ticks, e.quantity)
            for e in canonical[:6]
            if e.side is not None
        ]
        quantities: dict[int, list[int]] = {}
        for side in (1, -1):
            total = 60 + rng.randint(3, 10)
            # One cut ahead, two inside own volume: three independently sized
            # partials per side, including seeds that complete an own order.
            cuts = [rng.randint(1, 59), *sorted(rng.sample(range(61, total), 2))]
            quantities[side] = [
                b - a for a, b in zip([0, *cuts], [*cuts, total], strict=True)
            ]
        expected = []
        ahead = {1: 60, -1: 60}
        remaining = {1: 10, -1: 10}
        for index in range(4):
            for side in (1, -1):
                budget = quantities[side][index]
                own = min(remaining[side], max(0, budget - ahead[side]))
                ahead[side] = max(0, ahead[side] - budget)
                remaining[side] -= own
                timestamp = 3_000_000 + index * 100_000
                rows.append(
                    (timestamp, "TRADE", side, 99 if side == 1 else 101, budget)
                )
                if own:
                    expected.append(ExpectedFill(len(rows), timestamp, side, own))
        rows.extend([(3_500_000, "ADD", 1, 96, 1), (5_000_000, "ADD", 1, 95, 1)])
        cases.append(
            Scenario(
                f"seeded-partitions__{seed}",
                "seeded-partitions",
                QueueAllocation.PRO_RATA,
                _tape(rows),
                tuple(expected),
                report_ns=2_500_000,
                cutoff_ns=3_500_000,
                cancel_send_ns=3_600_000,
                seed=seed,
            )
        )
    return tuple(cases)


SCENARIOS = _scenarios()


def _config(case: Scenario, tape_path: Path) -> AppConfig:
    base = load_config(BASE_CONFIG)
    return base.model_copy(
        update={
            "data": base.data.model_copy(
                update={
                    "input_path": Path("tape.csv"),
                    "provenance": base.data.provenance.model_copy(
                        update={
                            "dataset_id": f"{VERSION}/{case.name}",
                            "checksum_sha256": sha256_file(tape_path),
                        }
                    ),
                }
            ),
            "latency": base.latency.model_copy(
                update={
                    "order_entry_ns": case.entry_ns,
                    "cancellation_ns": case.cancel_ns,
                    "fill_report_ns": case.report_ns,
                }
            ),
            "queue_model": base.queue_model.model_copy(
                update={"cancellation_allocation": case.allocation}
            ),
            "risk": base.risk.model_copy(
                update={"prohibit_new_quotes_last_ns": 5_000_000 - case.cutoff_ns}
            ),
            "random_seed": case.seed,
        }
    )


def _observed_run(
    config: AppConfig, case: Scenario, root: Path
) -> tuple[BacktestResult, list[dict[str, Any]]]:
    """Pass-through, scoped test observers; never inject scheduling or commands."""
    instances: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    original_step = Scheduler.run_one

    def observe_init(cls: type[Any], label: str, patch: pytest.MonkeyPatch) -> None:
        original = cls.__init__

        def initialize(instance: Any, *args: Any, **kwargs: Any) -> None:
            original(instance, *args, **kwargs)
            instances[label] = instance

        patch.setattr(cls, "__init__", initialize)

    def step(
        scheduler: Scheduler, handler: Callable[..., Any]
    ) -> ScheduledEvent[Any] | None:
        def observed_handler(event: ScheduledEvent[Any], current: Scheduler) -> None:
            handler(event, current)
            exchange: Exchange = instances["exchange"]
            runtime: StrategyRuntime = instances["runtime"]
            portfolio: Portfolio = instances["portfolio"]
            payload = event.payload
            source: dict[str, Any] = {"kind": type(payload).__name__}
            if isinstance(payload, ChannelDelivery):
                source.update(
                    channel=payload.channel_name,
                    channel_sequence=payload.channel_sequence,
                    send_ns=payload.send_timestamp_ns,
                )
                payload = payload.payload
                source["kind"] = type(payload).__name__
            if isinstance(payload, MarketEvent):
                source.update(payload.as_dict())
            elif isinstance(payload, NewOrderRequest):
                source.update(
                    side=int(payload.side), client_order_id=payload.client_order_id
                )
            elif isinstance(payload, CancelRequest):
                source.update(
                    order_id=payload.order_id, client_order_id=payload.client_order_id
                )
            elif isinstance(payload, ExecutionReport):
                source.update(
                    report_id=payload.report_id,
                    report_type=payload.report_type.value,
                    order_id=payload.order_id,
                    status=payload.order_status.value,
                    remaining=payload.remaining_quantity,
                    exchange_ns=payload.exchange_timestamp_ns,
                    fill_id=payload.fill.fill_id if payload.fill else None,
                    fill_quantity=payload.fill.quantity if payload.fill else 0,
                    side=int(payload.fill.side) if payload.fill else 0,
                    reason=payload.reason,
                )
            orders = []
            for order in exchange.registry.orders:
                queue = (
                    order.queue_position
                    if exchange.queue_model.contains(order.order_id)
                    else None
                )
                orders.append(
                    dict(
                        order_id=order.order_id,
                        side=int(order.side),
                        status=order.status.value,
                        remaining=order.remaining_quantity,
                        filled=order.cumulative_filled_quantity,
                        ahead=queue.total_ahead if queue else None,
                        behind=queue.external_behind if queue else None,
                        historical_external=exchange.book.quantity_at(
                            order.side, order.price_ticks
                        ),
                        overlay_external=(
                            queue.external_ahead + queue.external_behind
                            if queue
                            else None
                        ),
                    )
                )
            trace.append(
                dict(
                    key=list(event.ordering_key),
                    source=source,
                    orders=orders,
                    true_inventory=portfolio.inventory,
                    known_inventory=runtime.known_inventory,
                    observed_sequence=runtime.observed_sequence,
                    trade_cash_ticks=portfolio.trade_cash_ticks,
                    fees=str(portfolio.fees),
                    rebates=str(portfolio.rebates),
                )
            )

        return original_step(scheduler, observed_handler)

    with pytest.MonkeyPatch.context() as patch:
        observe_init(Exchange, "exchange", patch)
        observe_init(StrategyRuntime, "runtime", patch)
        observe_init(Portfolio, "portfolio", patch)
        patch.setattr(Scheduler, "run_one", step)
        result = run_backtest(
            config, list(case.events), run_name=case.name, output_root=root
        )
    return result, trace


def _amount(value: Any) -> Decimal:
    result = Decimal(str(value))
    assert result.is_finite()
    return result


def _ledger(
    prefix: tuple[ExpectedFill, ...], mark: Decimal, config: AppConfig
) -> dict[str, Any]:
    """Independent equal-price closed/open-lot identity, no Portfolio helpers."""
    buy = sum(f.quantity for f in prefix if f.side == 1)
    sell = sum(f.quantity for f in prefix if f.side == -1)
    inventory = buy - sell
    cash_ticks = -99 * buy + 101 * sell
    turnover_ticks = 99 * buy + 101 * sell
    tick = config.instrument.tick_size
    fees = sum(
        (
            Decimal(f.quantity)
            * (
                config.fees.maker_fee_per_unit
                + Decimal(99 if f.side == 1 else 101)
                * tick
                * config.fees.proportional_fee_rate
            )
            for f in prefix
        ),
        ZERO,
    )
    rebates = Decimal(buy + sell) * config.fees.maker_rebate_per_unit
    realized_ticks = Decimal(2 * min(buy, sell))
    gross_ticks = Decimal(cash_ticks) + inventory * mark
    average = Decimal(99 if inventory > 0 else 101) if inventory else None
    return dict(
        inventory=inventory,
        trade_cash_ticks=cash_ticks,
        trade_cash=cash_ticks * tick,
        cash=cash_ticks * tick - fees + rebates,
        realized_pnl_ticks=realized_ticks,
        unrealized_pnl_ticks=gross_ticks - realized_ticks,
        gross_pnl_ticks=gross_ticks,
        realized_pnl=realized_ticks * tick,
        unrealized_pnl=(gross_ticks - realized_ticks) * tick,
        gross_pnl=gross_ticks * tick,
        fees=fees,
        rebates=rebates,
        net_pnl=gross_ticks * tick - fees + rebates,
        turnover_ticks=turnover_ticks,
        turnover=turnover_ticks * tick,
        buy_volume=buy,
        sell_volume=sell,
        fill_count=len(prefix),
        mark_ticks=mark,
        average_cost_ticks=average,
        current_gross_exposure=abs(inventory) * mark * tick,
    )


def _audit(
    case: Scenario,
    result: BacktestResult,
    trace: list[dict[str, Any]],
    config: AppConfig,
) -> dict[str, Any]:
    run = result.run_directory
    assert tuple(load_events(run / "tape.csv")) == case.events, "saved input tape"
    assert load_config(run / "run_config.yaml") == config, "saved run configuration"
    assert sha256_file(run / "tape.csv") == config.data.provenance.checksum_sha256
    fills = pl.read_parquet(run / "fills.parquet").to_dicts()
    expected = [(f.timestamp, f.side, f.quantity) for f in case.fills]
    assert [
        (f["exchange_fill_timestamp_ns"], f["side"], f["quantity"]) for f in fills
    ] == expected, "analytical fill oracle"
    assert len({f["fill_id"] for f in fills}) == len(fills), "unique executions"
    for fill, oracle in zip(fills, case.fills, strict=True):
        price = 99 if oracle.side == 1 else 101
        fees = Decimal(oracle.quantity) * (
            config.fees.maker_fee_per_unit
            + price * config.instrument.tick_size * config.fees.proportional_fee_rate
        )
        assert fill["price_ticks"] == price and fill["liquidity_role"] == "maker"
        assert abs(_amount(fill["fee"]) - fees) <= TOLERANCE
        assert (
            abs(
                _amount(fill["rebate"])
                - oracle.quantity * config.fees.maker_rebate_per_unit
            )
            <= TOLERANCE
        )
        assert fill["decision_timestamp_ns"] == fill["send_timestamp_ns"] == 100_000
        assert fill["exchange_arrival_timestamp_ns"] == 100_000 + case.entry_ns
        assert (
            fill["strategy_notification_timestamp_ns"]
            == oracle.timestamp + case.report_ns
        )

    orders = pl.read_parquet(run / "orders.parquet").to_dicts()
    transition_ids = [row["transition_id"] for row in orders]
    assert len(set(transition_ids)) == len(transition_ids)
    histories = {
        side: [row for row in orders if row["side"] == side] for side in (1, -1)
    }
    assert len({row["order_id"] for row in orders}) == 2
    order_sides = {history[0]["order_id"]: side for side, history in histories.items()}
    expected_cancels_by_side = {
        side: sum(
            fill.quantity
            for fill in case.fills
            if fill.side == side
            and fill.timestamp + case.report_ns <= case.cancel_send_ns
        )
        < 10
        for side in (1, -1)
    }
    # Reports are created at venue time, then delivered on an ordered channel.
    # Stable sorting keeps historical fills before same-time command reports.
    expected_reports = [
        (100_000 + case.entry_ns, "accepted", side, "live", 10, None)
        for side in (1, -1)
    ]
    report_remaining = {1: 10, -1: 10}
    for fill in case.fills:
        report_remaining[fill.side] -= fill.quantity
        residual = report_remaining[fill.side]
        expected_reports.append(
            (
                fill.timestamp,
                "partial_fill" if residual else "fill",
                fill.side,
                "partially_filled" if residual else "filled",
                residual,
                None,
            )
        )
    for side in (1, -1):
        if expected_cancels_by_side[side]:
            residual = report_remaining[side]
            expected_reports.append(
                (
                    case.cancel_send_ns + case.cancel_ns,
                    "cancelled" if residual else "cancel_rejected",
                    side,
                    "cancelled" if residual else "filled",
                    residual,
                    None if residual else "order_not_live",
                )
            )
    expected_reports.sort(key=lambda item: item[0])
    delivered_reports = [
        (
            row["source"]["exchange_ns"],
            row["source"]["report_type"],
            order_sides[row["source"]["order_id"]],
            row["source"]["status"],
            row["source"]["remaining"],
            row["source"]["reason"],
        )
        for row in trace
        if row["source"]["kind"] == "ExecutionReport"
    ]
    assert delivered_reports == expected_reports, "complete execution-report lifecycle"
    for side, history in histories.items():
        executions = [f for f in case.fills if f.side == side]
        statuses = ["pending_arrival", "live"]
        timestamps = [100_000 + case.entry_ns] * 2
        remainder = 10
        residuals = [10, 10]
        for fill in executions:
            remainder -= fill.quantity
            statuses.append("partially_filled" if remainder else "filled")
            timestamps.append(fill.timestamp)
            residuals.append(remainder)
        if remainder:
            statuses.append("cancelled")
            timestamps.append(case.cancel_send_ns + case.cancel_ns)
            residuals.append(remainder)
        assert [row["new_status"] for row in history] == statuses, "lifecycle"
        assert [row["previous_status"] for row in history] == [None, *statuses[:-1]]
        assert [row["timestamp_ns"] for row in history] == timestamps
        assert [row["remaining_quantity"] for row in history] == residuals
        assert all(
            row["original_quantity"] == 10
            and row["cumulative_filled_quantity"] == 10 - remainder
            for row in history
        )
        assert remainder + sum(f.quantity for f in executions) == 10
        for fill in (f for f in fills if f["side"] == side):
            assert all(
                fill[key] == history[0][key]
                for key in (
                    "order_id",
                    "client_order_id",
                    "strategy_id",
                    "side",
                    "price_ticks",
                    "send_timestamp_ns",
                    "exchange_arrival_timestamp_ns",
                )
            ), "fill/order join"

    # Raw-book oracle checks the entire tape without L2Book/validation helpers.
    # Build the expected snapshot path in tape order, including each fill
    # booking and each market mark at the same timestamp.
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    checkpoints = []
    prefix: tuple[ExpectedFill, ...] = ()
    peak = ZERO
    final_mark = ZERO
    for event in case.events:
        assert event.side is not None
        side = int(event.side)
        levels = book[side]
        before = levels.get(event.price_ticks, 0)
        if event.event_type in (EventType.ADD, EventType.SNAPSHOT):
            after = before + event.quantity
        else:
            assert event.quantity <= before, "realizable historical tape"
            after = before - event.quantity
        if after:
            levels[event.price_ticks] = after
        else:
            levels.pop(event.price_ticks, None)
        if not book[1] or not book[-1]:
            continue
        assert max(book[1]) < min(book[-1]), "uncrossed historical tape"
        final_mark = Decimal(max(book[1]) + min(book[-1])) / 2
        for fill in case.fills:
            if fill.sequence == event.sequence_number:
                prefix += (fill,)
                checkpoint = _ledger(prefix, final_mark, config)
                peak = max(peak, checkpoint["current_gross_exposure"])
                checkpoints.append(
                    (event.timestamp_ns, dict(checkpoint, peak_gross_exposure=peak))
                )
        checkpoint = _ledger(prefix, final_mark, config)
        peak = max(peak, checkpoint["current_gross_exposure"])
        checkpoints.append(
            (event.timestamp_ns, dict(checkpoint, peak_gross_exposure=peak))
        )
    pnl = pl.read_parquet(run / "pnl.parquet").to_dicts()
    inventory = pl.read_parquet(run / "inventory.parquet").to_dicts()
    assert len(pnl) == len(inventory) == len(checkpoints), "snapshot count"
    for row, projection, (timestamp, ledger) in zip(
        pnl, inventory, checkpoints, strict=True
    ):
        assert row["timestamp_ns"] == projection["timestamp_ns"] == timestamp
        for key, amount in ledger.items():
            if amount is None:
                assert row[key] is None
            else:
                assert abs(_amount(row[key]) - amount) <= TOLERANCE, (
                    f"intermediate accounting: {key}"
                )
        for key in ("inventory", "current_gross_exposure", "peak_gross_exposure"):
            assert projection[key] == row[key], "inventory projection"

    # Replay independent executions/deliveries over the ACTUAL scheduler keys.
    # This detects double booking on reports, early strategy knowledge and
    # incorrect same-time order even if final inventory happens to match.
    assert [row["key"] for row in trace] == sorted(row["key"] for row in trace)
    prefix = ()
    known = 0
    observed = -1
    report_ids: set[str] = set()
    delivered_fill_ids: list[str] = []
    cancel_commands = []
    for row in trace:
        source = row["source"]
        if source["kind"] == "MarketEvent" and "channel" not in source:
            prefix += tuple(
                f for f in case.fills if f.sequence == source["sequence_number"]
            )
        if source.get("channel") == "market_data":
            assert row["key"][0] == source["timestamp_ns"] + 100_000
            observed = source["sequence_number"]
        if source["kind"] == "ExecutionReport":
            assert source["report_id"] not in report_ids, "unique reports"
            report_ids.add(source["report_id"])
            assert row["key"][0] == source["exchange_ns"] + case.report_ns
            if source["fill_id"]:
                index = len(delivered_fill_ids)
                oracle = case.fills[index]
                assert (
                    source["side"] == oracle.side
                    and source["fill_quantity"] == oracle.quantity
                )
                assert source["exchange_ns"] == oracle.timestamp
                assert source["fill_id"] == fills[index]["fill_id"]
                known += oracle.side * oracle.quantity
                delivered_fill_ids.append(source["fill_id"])
        if source["kind"] == "CancelRequest":
            assert source["send_ns"] == case.cancel_send_ns
            assert row["key"][0] == case.cancel_send_ns + case.cancel_ns
            cancel_commands.append(row)
        ledger = _ledger(prefix, Decimal(100), config)
        assert row["true_inventory"] == ledger["inventory"], "execution-time inventory"
        assert row["trade_cash_ticks"] == ledger["trade_cash_ticks"], (
            "execution-time cash"
        )
        assert (
            _amount(row["fees"]) == ledger["fees"]
            and _amount(row["rebates"]) == ledger["rebates"]
        ), "execution-time costs"
        assert row["known_inventory"] == known, "delayed inventory knowledge"
        assert row["observed_sequence"] == observed, "delayed market knowledge"
        for order in row["orders"]:
            assert order["remaining"] + order["filled"] == 10, "event-time conservation"
            if order["ahead"] is not None:
                assert order["ahead"] >= 0 and order["behind"] >= 0
    assert delivered_fill_ids == [f["fill_id"] for f in fills]
    expected_cancels = sum(expected_cancels_by_side.values())
    assert len(cancel_commands) == expected_cancels
    assert len(report_ids) == 2 + len(case.fills) + expected_cancels
    final = _ledger(case.fills, final_mark, config)
    assert result.diagnostics["end_inventory"] == final["inventory"]
    assert result.diagnostics["open_orders_end"] == 0
    assert result.diagnostics["submitted_order_messages"] == 2
    assert result.diagnostics["input_validation_issue_count"] == 0
    assert result.diagnostics["true_book_diagnostics"] == {}
    assert not result.diagnostics["kill_switch_active"]
    assert (
        abs(_amount(result.metrics["pnl"]["net_pnl"]) - final["net_pnl"]) <= TOLERANCE
    )
    assert result.diagnostics["event_stream_sha256"] == event_stream_sha256(case.events)
    summary = json.loads((run / "summary.json").read_text())
    assert summary["fill_count"] == len(case.fills)
    assert summary["end_inventory"] == final["inventory"]
    assert abs(_amount(summary["net_pnl"]) - final["net_pnl"]) <= TOLERANCE
    assert summary["event_stream_sha256"] == event_stream_sha256(case.events)
    assert result.diagnostics["input_validation_certificate"][
        "event_stream_sha256"
    ] == event_stream_sha256(case.events)
    return dict(
        name=case.name,
        family=case.family,
        allocation=case.allocation.value,
        offset_ns=case.offset_ns,
        market_data_ns=100_000,
        entry_ns=case.entry_ns,
        cancellation_ns=case.cancel_ns,
        report_ns=case.report_ns,
        seed=case.seed,
        event_stream_sha256=event_stream_sha256(case.events),
        filled_bid=final["buy_volume"],
        filled_ask=final["sell_volume"],
        fill_count=len(case.fills),
        final_inventory=final["inventory"],
        final_mark_ticks=str(final_mark),
        net_pnl=str(final["net_pnl"]),
        snapshot_count=len(checkpoints),
        report_count=len(report_ids),
    )


def _boundary_assertions(case: Scenario, trace: list[dict[str, Any]]) -> None:
    """Event-time trajectory assertions use copied state, not final Parquet fields."""
    if case.family in ("add-arrival", "add-trade-arrival"):
        arrivals = [r for r in trace if r["source"]["kind"] == "NewOrderRequest"]
        assert len(arrivals) == 2
        for row in arrivals:
            order = next(o for o in row["orders"] if o["side"] == row["source"]["side"])
            expected_ahead = (
                100
                if case.family == "add-arrival" and case.offset_ns == -1
                else 150
                if case.family == "add-arrival"
                else 0
                if case.offset_ns == -1
                else 5
            )
            assert order["ahead"] == expected_ahead, "arrival-time queue position"
            assert row["key"][0] == 1_000_000 + case.offset_ns
            market = [
                r
                for r in trace
                if r["key"][0] == 1_000_000
                and r["source"]["kind"] == "MarketEvent"
                and "channel" not in r["source"]
            ]
            assert all(
                (row["key"] < event["key"]) == (case.offset_ns == -1)
                for event in market
            ), "historical add/trade tie priority"
    if case.family == "add-arrival" and case.offset_ns == -1:
        for row in trace:
            if (
                row["source"]["kind"] == "MarketEvent"
                and "channel" not in row["source"]
                and row["source"]["event_type"] == "CANCEL"
            ):
                order = next(
                    o for o in row["orders"] if o["side"] == row["source"]["side"]
                )
                assert order["ahead"] == POLICY_AHEAD[case.allocation]
    if case.family in ("trade-cancel", "add-cancel"):
        target = 3_000_000 if case.family == "trade-cancel" else 1_000_000
        relevant = [
            r
            for r in trace
            if r["key"][0] == target
            and r["source"]["kind"] == "MarketEvent"
            and "channel" not in r["source"]
        ]
        commands = [r for r in trace if r["source"]["kind"] == "CancelRequest"]
        assert all(r["key"][0] == target + case.offset_ns for r in commands)
        for command in commands:
            assert all(
                (command["key"] < event["key"]) == (case.offset_ns == -1)
                for event in relevant
            ), "historical event tie priority"
        if (
            case.family == "trade-cancel"
            and case.allocation is QueueAllocation.FRONT_OF_QUEUE
            and case.offset_ns >= 0
        ):
            rejected = [
                r for r in trace if r["source"].get("report_type") == "cancel_rejected"
            ]
            assert len(rejected) == 2
            assert all(
                r["source"]["status"] == "filled"
                and r["source"]["reason"] == "order_not_live"
                for r in rejected
            )


def _save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    destination = request.config.getoption("--latency-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    evidence = []
    for case in SCENARIOS:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(case.events, directory / "tape.csv")
        config = _config(case, directory / "tape.csv")
        result, trace = _observed_run(config, case, root)
        measured = _audit(case, result, trace, config)
        _boundary_assertions(case, trace)
        _save_json(directory / "event_snapshots.json", trace)
        measured["artifact_sha256"] = {
            p.name: sha256_file(p)
            for p in sorted(directory.iterdir())
            if p.suffix == ".parquet"
            or p.name
            in {"tape.csv", "run_config.yaml", "summary.json", "event_snapshots.json"}
        }
        evidence.append(measured)
    _save_json(
        root / "audit.json",
        dict(
            version=VERSION,
            base_config_sha256=sha256_file(BASE_CONFIG),
            case_count=len(evidence),
            cases=evidence,
        ),
    )
    return root, evidence


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case.name)
def test_analytical_boundary_and_partial_fill_case(
    case: Scenario, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, evidence = study
    assert len(evidence) == 50
    config = _config(case, root / case.name / "tape.csv")
    trace = json.loads((root / case.name / "event_snapshots.json").read_text())
    # Re-read saved artifacts; every parametrized test repeats the independent
    # audit so the stored output, not only in-memory observations, is checked.
    run = root / case.name
    result = BacktestResult(
        case.name,
        run,
        json.loads((run / "metrics.json").read_text()),
        json.loads((run / "diagnostics.json").read_text()),
    )
    _audit(case, result, trace, config)
    _boundary_assertions(case, trace)


def test_repeated_suite_has_byte_identical_artifacts(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, evidence = study
    repeated_evidence = []
    for case, measured in zip(SCENARIOS, evidence, strict=True):
        directory = tmp_path / case.name
        directory.mkdir()
        write_csv(case.events, directory / "tape.csv")
        config = _config(case, directory / "tape.csv")
        result, trace = _observed_run(config, case, tmp_path)
        repeat = _audit(case, result, trace, config)
        assert repeat == {k: v for k, v in measured.items() if k != "artifact_sha256"}
        _save_json(directory / "event_snapshots.json", trace)
        for name, digest in measured["artifact_sha256"].items():
            assert sha256_file(directory / name) == digest, name
            assert (directory / name).read_bytes() == (
                root / case.name / name
            ).read_bytes()
        repeated_evidence.append(
            dict(repeat, artifact_sha256=measured["artifact_sha256"])
        )
    _save_json(
        tmp_path / "audit.json",
        dict(
            version=VERSION,
            base_config_sha256=sha256_file(BASE_CONFIG),
            case_count=len(repeated_evidence),
            cases=repeated_evidence,
        ),
    )
    assert (tmp_path / "audit.json").read_bytes() == (root / "audit.json").read_bytes()
    # Report latency changes knowledge only: authoritative execution/cash/P&L
    # tables remain identical for each mirrored residual scenario.
    for primary in (1, -1):
        for artifact in ("orders", "inventory", "pnl"):
            first = (
                root / f"split-residual__{primary:+d}__report0" / f"{artifact}.parquet"
            )
            for report in (100_000, 2_500_000):
                second = (
                    root / f"split-residual__{primary:+d}__report{report}" / first.name
                )
                assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("family", sorted({c.family for c in SCENARIOS}))
def test_observers_preserve_standard_replay(
    family: str, study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, evidence = study
    case = next(c for c in SCENARIOS if c.family == family)
    config = _config(case, root / case.name / "tape.csv")
    result = run_backtest(
        config, list(case.events), run_name=case.name, output_root=tmp_path
    )
    measured = next(row for row in evidence if row["name"] == case.name)
    for name, digest in measured["artifact_sha256"].items():
        if name not in {"tape.csv", "event_snapshots.json"}:
            assert sha256_file(result.run_directory / name) == digest, name


@pytest.mark.parametrize(
    "damage", ["report_booking", "early_knowledge", "tie_order", "wrong_oracle"]
)
def test_independent_oracles_reject_corrupted_evidence(
    study: tuple[Path, list[dict[str, Any]]], damage: str
) -> None:
    root, _ = study
    case = next(c for c in SCENARIOS if c.name == "trade-cancel__pro_rata__+0")
    run = root / case.name
    config = _config(case, run / "tape.csv")
    result = BacktestResult(
        case.name,
        run,
        json.loads((run / "metrics.json").read_text()),
        json.loads((run / "diagnostics.json").read_text()),
    )
    trace = json.loads((run / "event_snapshots.json").read_text())
    if damage == "report_booking":
        row = next(r for r in trace if r["source"].get("fill_id"))
        row["trade_cash_ticks"] += 99
    elif damage == "early_knowledge":
        row = next(
            r
            for r in trace
            if r["key"][0] == 3_000_000 and r["source"].get("event_type") == "TRADE"
        )
        row["known_inventory"] = 5
    elif damage == "tie_order":
        row = next(r for r in trace if r["source"]["kind"] == "CancelRequest")
        row["key"][2] = 5
    else:
        case = replace(
            case, fills=(replace(case.fills[0], quantity=4), *case.fills[1:])
        )
    with pytest.raises(AssertionError):
        _audit(case, result, trace, config)
        _boundary_assertions(case, trace)


@pytest.mark.parametrize("artifact", ["tape.csv", "run_config.yaml"])
def test_audit_binds_saved_inputs(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path, artifact: str
) -> None:
    root, _ = study
    case = SCENARIOS[0]
    original = root / case.name
    config = _config(case, original / "tape.csv")
    damaged = tmp_path / "damaged"
    shutil.copytree(original, damaged)
    path = damaged / artifact
    content = path.read_text()
    if artifact == "tape.csv":
        content = content.replace("99,100", "99,101", 1)
    else:
        content = content.replace("order_entry_ns: 899999", "order_entry_ns: 900000")
    assert content != path.read_text()
    path.write_text(content)
    result = BacktestResult(
        case.name,
        damaged,
        json.loads((damaged / "metrics.json").read_text()),
        json.loads((damaged / "diagnostics.json").read_text()),
    )
    trace = json.loads((damaged / "event_snapshots.json").read_text())
    with pytest.raises(
        AssertionError, match=r"saved input tape|saved run configuration"
    ):
        _audit(case, result, trace, config)
