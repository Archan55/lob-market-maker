"""Reservation-to-close study with independent reachable exposure and cost pools.

No engine risk projection, portfolio, book or queue method is used by the audit.
Pass-through observers record sends, gate inputs and complete scheduler states.
The small quantities permit exhaustive hypothetical partial-fill combinations.
"""

from __future__ import annotations

import itertools
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import BacktestResult, run_backtest
from lobmm.channels import ChannelDelivery, OrderedChannel
from lobmm.config import AppConfig, dump_config, load_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import load_events, write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest
from lobmm.portfolio import Portfolio
from lobmm.risk import RiskManager
from lobmm.scheduler import ScheduledEvent, Scheduler
from lobmm.strategy_runtime import StrategyRuntime

pytestmark = pytest.mark.integration
VERSION = "exposure-session-stress-v1"
BASE = Path("configs/exposure_session_stress_v1.yaml")
TABLES = (
    "orders",
    "fills",
    "inventory",
    "pnl",
    "quotes",
    "risk_events",
    "market",
    "markouts",
)


@dataclass(frozen=True)
class Case:
    name: str
    family: str
    primary: int
    report_ns: int = 100_000
    mark: str = "midpoint"
    policy: str = "mark"
    cap: int = 6
    open_quantity: int = 8
    open_count: int = 2
    entry_ns: int = 300_000
    cancel_ns: int = 1_000_000
    refresh_ns: int = 100_000_000


CASES = tuple(
    [
        Case(f"capacity__{side:+d}__report{lag}", "capacity", side, lag)
        for side in (1, -1)
        for lag in (0, 100_000, 5_000_000)
    ]
    + [
        Case(
            f"cancel-race__{side:+d}",
            "cancel-race",
            side,
            refresh_ns=1_000_000,
            cancel_ns=1_500_000,
        )
        for side in (1, -1)
    ]
    + [
        Case(f"shutdown-inflight__{side:+d}", "shutdown-inflight", side, cap=8)
        for side in (1, -1)
    ]
    + [
        Case(f"open-quantity__{side:+d}", "open-quantity", side, open_quantity=4)
        for side in (1, -1)
    ]
    + [
        Case(f"open-count__{side:+d}", "open-count", side, open_count=1)
        for side in (1, -1)
    ]
    + [
        Case(
            "session-pending",
            "session-pending",
            1,
            entry_ns=8_000_000,
            report_ns=5_000_000,
        )
    ]
    + [
        Case(
            f"terminal__{side:+d}__{mark}__{policy}",
            "terminal",
            side,
            report_ns=5_000_000,
            mark=mark,
            policy=policy,
        )
        for side in (1, -1)
        for mark in ("midpoint", "microprice", "conservative")
        for policy in ("mark", "liquidate")
    ]
)


def tape(case: Case) -> list[MarketEvent]:
    p = case.primary
    price = 99 if p == 1 else 101
    rows: list[tuple[int, str, int, int, int]] = [
        (0, "SNAPSHOT", 1, 99, 2),
        (0, "SNAPSHOT", -1, 101, 2),
        (0, "SNAPSHOT", 1, 97, 20),
        (0, "SNAPSHOT", -1, 103, 20),
        (1_000_000, "ADD", 1, 99, 6),
        (1_000_000, "ADD", -1, 101, 6),
    ]
    if case.family == "shutdown-inflight":
        rows += [
            (2_000_000, "TRADE", p, price, 6),
            (2_200_000, "CANCEL", p, price, 2),
            (2_200_000, "ADD", p, 95 if p == 1 else 105, 20),
        ]
        # Remove the deeper level too: the terminal true best moves adversely.
        rows += [(2_200_000, "CANCEL", p, 97 if p == 1 else 103, 20)]
    elif case.family == "terminal":
        rows += [(2_000_000, "TRADE", p, price, 6), (5_000_000, "CANCEL", p, price, 1)]
    else:
        rows += [
            (2_000_000, "TRADE", p, price, 4),
            (2_200_000, "TRADE", p, price, 2),
            (3_000_000, "TRADE", -p, 101 if p == 1 else 99, 6),
        ]
    rows += [(6_000_000, "ADD", 1, 94, 1)]
    return [
        MarketEvent(t, i, EventType(kind), Side(side), px, qty)
        for i, (t, kind, side, px, qty) in enumerate(rows, 1)
    ]


def config(case: Case) -> AppConfig:
    value = dump_config(load_config(BASE))
    value["data"]["input_path"] = "tape.csv"
    value["latency"].update(
        fill_report_ns=case.report_ns,
        order_entry_ns=case.entry_ns,
        cancellation_ns=case.cancel_ns,
    )
    value["strategy"]["refresh_interval_ns"] = case.refresh_ns
    if case.family == "cancel-race":
        value["strategy"]["minimum_quote_lifetime_ns"] = 0
    value["risk"].update(
        max_abs_inventory=case.cap,
        max_total_open_quantity=case.open_quantity,
        max_open_orders=case.open_count,
    )
    if case.family == "shutdown-inflight":
        value["risk"]["max_loss"] = "0.03"
    value["backtest"].update(mark_price=case.mark, session_end_policy=case.policy)
    return AppConfig.model_validate(value)


def save(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def save_deterministic_outputs(directory: Path) -> None:
    for name in ("metrics", "diagnostics"):
        document = json.loads((directory / f"{name}.json").read_text())
        timing = document["engineering"] if name == "metrics" else document
        for field in ("wall_clock_seconds", "events_per_second", "peak_memory_bytes"):
            timing.pop(field)
        save(directory / f"deterministic_{name}.json", document)


def observed_run(case: Case, root: Path) -> tuple[BacktestResult, list[dict[str, Any]]]:
    instances: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    original_step, original_send = Scheduler.run_one, OrderedChannel.send
    original_check = RiskManager.check_order

    def init(cls: type[Any], label: str, patch: pytest.MonkeyPatch) -> None:
        original = cls.__init__

        def initialize(obj: Any, *args: Any, **kwargs: Any) -> None:
            original(obj, *args, **kwargs)
            instances[label] = obj

        patch.setattr(cls, "__init__", initialize)

    def send(channel: Any, payload: Any, **kwargs: Any) -> Any:
        delivery = original_send(channel, payload, **kwargs)
        if isinstance(payload, (NewOrderRequest, CancelRequest)):
            data = asdict(payload)
            if isinstance(payload, NewOrderRequest):
                data["side"] = int(payload.side)
            emitted.append(dict(kind=type(payload).__name__, **data))
        return delivery

    def check(manager: RiskManager, **kwargs: Any) -> Any:
        decision = original_check(manager, **kwargs)
        gates.append(
            dict(
                side=int(kwargs["side"]),
                quantity=kwargs["quantity"],
                inventory=kwargs["inventory"],
                open=[
                    dict(
                        order_id=o.order_id,
                        side=int(o.side),
                        quantity=o.remaining_quantity,
                    )
                    for o in kwargs["open_orders"]
                ],
                approved=decision.approved,
                reason=decision.reason.value if decision.reason else None,
            )
        )
        return decision

    def step(scheduler: Scheduler, handler: Callable[..., Any]) -> Any:
        def after(event: ScheduledEvent[Any], current: Scheduler) -> None:
            emitted.clear()
            gates.clear()
            handler(event, current)
            exchange, portfolio, runtime = (
                instances[x] for x in ("exchange", "portfolio", "runtime")
            )
            payload = event.payload
            channel = None
            if isinstance(payload, ChannelDelivery):
                channel = payload.channel_name
                payload = payload.payload
            source: dict[str, Any] = dict(kind=type(payload).__name__, channel=channel)
            if isinstance(payload, (NewOrderRequest, CancelRequest)):
                source.update(asdict(payload))
                if isinstance(payload, NewOrderRequest):
                    source["side"] = int(payload.side)
            elif isinstance(payload, MarketEvent):
                source.update(payload.as_dict())
            elif isinstance(payload, ExecutionReport):
                source.update(
                    report_id=payload.report_id,
                    client_order_id=payload.client_order_id,
                    type=payload.report_type.value,
                    exchange_ns=payload.exchange_timestamp_ns,
                    fill_id=payload.fill.fill_id if payload.fill else None,
                    signed_quantity=payload.fill.signed_quantity if payload.fill else 0,
                )
            orders = [
                dict(
                    order_id=o.order_id,
                    client_order_id=o.client_order_id,
                    strategy_id=o.strategy_id,
                    side=int(o.side),
                    quantity=o.original_quantity,
                    remaining=o.remaining_quantity,
                    filled=o.cumulative_filled_quantity,
                    status=o.status.value,
                    live=o.is_fillable,
                )
                for o in exchange.registry.orders
            ]
            fills = [
                dict(
                    fill_id=f.fill_id,
                    order_id=f.order_id,
                    side=int(f.side),
                    quantity=f.quantity,
                    price=f.price_ticks,
                    fee=str(f.fee),
                    rebate=str(f.rebate),
                    timestamp=f.exchange_fill_timestamp_ns,
                    role=f.liquidity_role.value,
                )
                for f in exchange.registry.fills
            ]
            # Scheduler snapshot is observed independently of the backtest's pending map.
            pending = [
                dict(
                    client_order_id=e.payload.payload.client_order_id,
                    side=int(e.payload.payload.side),
                    quantity=e.payload.payload.quantity,
                )
                for e in current.pending()
                if isinstance(e.payload, ChannelDelivery)
                and isinstance(e.payload.payload, NewOrderRequest)
            ]
            trace.append(
                dict(
                    key=list(event.ordering_key),
                    source=source,
                    sends=list(emitted),
                    gates=list(gates),
                    orders=orders,
                    fills=fills,
                    pending=pending,
                    inventory=portfolio.inventory,
                    known=runtime.known_inventory,
                    cash_ticks=portfolio.trade_cash_ticks,
                    fees=str(portfolio.fees),
                    rebates=str(portfolio.rebates),
                    kill=instances["risk"].kill_switch_active,
                )
            )

        return original_step(scheduler, after)

    with pytest.MonkeyPatch.context() as patch:
        for cls, label in (
            (Exchange, "exchange"),
            (Portfolio, "portfolio"),
            (StrategyRuntime, "runtime"),
            (RiskManager, "risk"),
        ):
            init(cls, label, patch)
        patch.setattr(Scheduler, "run_one", step)
        patch.setattr(OrderedChannel, "send", send)
        patch.setattr(RiskManager, "check_order", check)
        result = run_backtest(
            config(case), tape(case), run_name=case.name, output_root=root
        )
    return result, trace


def reachable(inventory: int, orders: list[dict[str, Any]]) -> set[int]:
    """Enumerate all partial-fill choices; do not net opposing reservations."""
    return {
        inventory + sum(o["side"] * q for o, q in zip(orders, quantities, strict=True))
        for quantities in itertools.product(*(range(o["quantity"] + 1) for o in orders))
    }


def expected_makers(case: Case) -> list[tuple[int, int, int, int]]:
    """Hand-derived initial external ahead=2; six subsequently add behind own4."""
    p = case.primary
    px = 99 if p == 1 else 101
    if case.family == "session-pending":
        return []
    if case.family in {"terminal", "shutdown-inflight"}:
        return [(2_000_000, p, 4, px)]
    fills = [(2_000_000, p, 2, px), (2_200_000, p, 2, px)]
    if case.family == "cancel-race":
        # Refresh cancellation is sent at 1.1 ms, arrives at 2.6 ms:
        # both primary fills win, opposite-side quote is removed before 3 ms.
        return fills
    if case.family in {"open-count", "open-quantity"}:
        # Bid is evaluated before ask; only bid4 can reserve the one available slot.
        return fills if p == 1 else [(3_000_000, 1, 4, 99)]
    return [*fills, (3_000_000, -p, 4, 101 if p == 1 else 99)]


def close(actual: Any, expected: Fraction | Decimal | int, label: str) -> None:
    value = Decimal(str(actual))
    target = (
        Decimal(expected.numerator) / Decimal(expected.denominator)
        if isinstance(expected, Fraction)
        else Decimal(expected)
    )
    assert abs(value - target) <= Decimal("1e-12"), (label, value, target)


def audit(case: Case, directory: Path, trace: list[dict[str, Any]]) -> dict[str, Any]:
    saved_config = load_config(directory / "run_config.yaml")
    assert dump_config(saved_config) == dump_config(config(case)), "saved configuration"
    events = load_events(directory / "tape.csv")
    assert events == tape(case), "saved tape"
    stream = event_stream_sha256(events)
    diagnostics = json.loads((directory / "diagnostics.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    metrics = json.loads((directory / "metrics.json").read_text())
    for name, original in (("metrics", metrics), ("diagnostics", diagnostics)):
        stable = json.loads(json.dumps(original))
        timing = stable["engineering"] if name == "metrics" else stable
        for field in ("wall_clock_seconds", "events_per_second", "peak_memory_bytes"):
            timing.pop(field)
        assert stable == json.loads(
            (directory / f"deterministic_{name}.json").read_text()
        ), "deterministic public outputs"
    assert (
        summary["event_stream_sha256"] == diagnostics["event_stream_sha256"] == stream
    )
    assert diagnostics["input_validation_issue_count"] == 0
    assert not diagnostics["true_book_diagnostics"]
    fills = pl.read_parquet(directory / "fills.parquet").to_dicts()
    pnl = pl.read_parquet(directory / "pnl.parquet").to_dicts()
    inventory_rows = pl.read_parquet(directory / "inventory.parquet").to_dicts()
    orders_rows = pl.read_parquet(directory / "orders.parquet").to_dicts()
    actual_makers = [
        (f["exchange_fill_timestamp_ns"], f["side"], f["quantity"], f["price_ticks"])
        for f in fills
        if f["liquidity_role"] == "maker"
    ]
    assert actual_makers == expected_makers(case), "independent maker budgets"
    expected = expected_makers(case).copy()
    if case.policy == "liquidate":
        # The best-price limit deliberately cannot sweep the deeper level.
        expected.append(
            (
                6_000_000 + case.entry_ns,
                -case.primary,
                1,
                99 if case.primary == 1 else 101,
            )
        )
    assert [
        (f["exchange_fill_timestamp_ns"], f["side"], f["quantity"], f["price_ticks"])
        for f in fills
    ] == expected, "terminal fill budget"
    assert len({f["fill_id"] for f in fills}) == len(fills), "unique executions"
    terminal: dict[str, dict[str, Any]] = {}
    for row in orders_rows:
        terminal[row["order_id"]] = row
    assert all(
        r["new_status"] in {"filled", "cancelled", "expired", "rejected"}
        for r in terminal.values()
    ), "terminal lifecycles"
    for order_id, row in terminal.items():
        quantity = sum(f["quantity"] for f in fills if f["order_id"] == order_id)
        assert quantity + row["remaining_quantity"] == row["original_quantity"], (
            "order conservation"
        )
        assert row["cumulative_filled_quantity"] == quantity
    for f in fills:
        assert f["order_id"] in terminal
        assert (
            f["decision_timestamp_ns"]
            <= f["send_timestamp_ns"]
            <= f["exchange_arrival_timestamp_ns"]
            <= f["exchange_fill_timestamp_ns"]
            <= f["strategy_notification_timestamp_ns"]
        )
        rate = Decimal("0.002") if f["liquidity_role"] == "maker" else Decimal("0.003")
        close(
            f["fee"],
            f["quantity"] * (rate + Decimal(f["price_ticks"]) * Decimal("0.000001")),
            "fill fee",
        )
        close(
            f["rebate"],
            Decimal(f["quantity"]) * Decimal("0.0005")
            if f["liquidity_role"] == "maker"
            else 0,
            "fill rebate",
        )

    pending: dict[str, dict[str, Any]] = {}
    alive: dict[str, dict[str, Any]] = {}
    executed_count = 0
    ended = False
    known, used_reports = 0, set()
    enumerations = 0
    largest, smallest, max_knowledge_gap = 0, 0, 0
    cancellation_reservations = 0
    cancel_ids: set[str] = set()
    post_kill_accepts = 0
    late_session_rejects = 0
    previous_key: tuple[int, ...] | None = None
    previous_inventory = 0
    for row in trace:
        key = tuple(row["key"])
        assert previous_key is None or key > previous_key, "scheduler ordering"
        previous_key = key
        source = row["source"]
        # Gates precede sends and receive the previous authoritative state.
        reserved = [
            dict(order_id=oid, side=o["side"], quantity=o["quantity"])
            for oid, o in alive.items()
        ]
        reserved += [
            dict(order_id=f"pending:{cid}", side=o["side"], quantity=o["quantity"])
            for cid, o in pending.items()
        ]
        for gate in row["gates"]:
            assert gate["inventory"] == previous_inventory, "gate true inventory"
            assert sorted(gate["open"], key=lambda o: o["order_id"]) == sorted(
                reserved, key=lambda o: o["order_id"]
            ), "gate reservations"
            candidate = reachable(gate["inventory"], [*reserved, gate])
            if gate["approved"]:
                assert min(candidate) >= -case.cap and max(candidate) <= case.cap, (
                    "approved reachable cap"
                )
                assert (
                    sum(o["quantity"] for o in reserved) + gate["quantity"]
                    <= case.open_quantity
                ), "approved open quantity"
                assert len(reserved) + 1 <= case.open_count, "approved open count"
                # Correlate approvals with same-handler sends in their stable side order.
                sent = next(
                    s
                    for s in row["sends"]
                    if s["kind"] == "NewOrderRequest" and s["side"] == gate["side"]
                )
                reserved.append(
                    dict(
                        order_id=f"pending:{sent['client_order_id']}",
                        side=gate["side"],
                        quantity=gate["quantity"],
                    )
                )
            elif gate["reason"] == "position_limit":
                assert max(candidate) > case.cap or min(candidate) < -case.cap, (
                    "position rejection oracle"
                )
            elif gate["reason"] == "max_open_quantity":
                assert (
                    sum(o["quantity"] for o in reserved) + gate["quantity"]
                    > case.open_quantity
                )
            elif gate["reason"] == "max_open_orders":
                assert len(reserved) + 1 > case.open_count

        if source["kind"] == "NewOrderRequest":
            assert (
                pending.pop(source["client_order_id"])["quantity"] == source["quantity"]
            ), "pending arrival transfer"
            if key[0] > 6_000_000 and source["strategy_id"] != "session-policy":
                assert not any(
                    o["client_order_id"] == source["client_order_id"]
                    for o in row["orders"]
                ), "closed-session entry"
                late_session_rejects += 1
            if not ended and source["strategy_id"] != "session-policy":
                # These passive requests cannot cross this constructed book.
                # Their reservation moves to the venue, never vanishes at arrival.
                order = next(
                    o
                    for o in row["orders"]
                    if o["client_order_id"] == source["client_order_id"]
                )
                assert (
                    order["quantity"] == source["quantity"]
                    and order["side"] == source["side"]
                )
                alive[order["order_id"]] = dict(
                    client_order_id=source["client_order_id"],
                    side=source["side"],
                    quantity=source["quantity"],
                )
            if row["kill"]:
                post_kill_accepts += sum(
                    o["client_order_id"] == source["client_order_id"] and o["live"]
                    for o in row["orders"]
                )
        for sent in row["sends"]:
            if sent["kind"] == "NewOrderRequest":
                assert sent["client_order_id"] not in pending, "unique pending send"
                pending[sent["client_order_id"]] = sent
            else:
                cid = sent.get("client_order_id")
                order_id = sent.get("order_id")
                cancel_ids.update(
                    o["order_id"]
                    for o in row["orders"]
                    if o["order_id"] == order_id
                    or (cid and o["client_order_id"] == cid)
                )
        assert sorted(row["pending"], key=lambda o: o["client_order_id"]) == sorted(
            [
                dict(client_order_id=cid, side=s["side"], quantity=s["quantity"])
                for cid, s in pending.items()
            ],
            key=lambda o: o["client_order_id"],
        ), "independent pending ledger"
        authoritative = row["fills"]
        for f in authoritative[executed_count:]:
            if f["order_id"] in alive:
                alive[f["order_id"]]["quantity"] -= f["quantity"]
                assert alive[f["order_id"]]["quantity"] >= 0, "reserved fill budget"
                if not alive[f["order_id"]]["quantity"]:
                    del alive[f["order_id"]]
            else:
                assert (
                    ended
                    and source["kind"] == "NewOrderRequest"
                    and source["strategy_id"] == "session-policy"
                ), "execution requires reservation"
        executed_count = len(authoritative)
        if source["kind"] == "CancelRequest":
            for oid, order in list(alive.items()):
                if oid == source.get("order_id") or order[
                    "client_order_id"
                ] == source.get("client_order_id"):
                    del alive[oid]
        if source["kind"] == "SessionEnd":
            alive.clear()
            ended = True
        actual_alive = {
            o["order_id"]: dict(
                client_order_id=o["client_order_id"],
                side=o["side"],
                quantity=o["remaining"],
            )
            for o in row["orders"]
            if o["live"]
        }
        assert actual_alive == alive, "authoritative reservation lifecycle"
        position = sum(f["side"] * f["quantity"] for f in authoritative)
        assert row["inventory"] == position, "fill inventory conservation"
        assert row["cash_ticks"] == -sum(
            f["side"] * f["quantity"] * f["price"] for f in authoritative
        ), "execution-time cash"
        close(
            row["fees"],
            sum((Decimal(f["fee"]) for f in authoritative), Decimal(0)),
            "fees booked once",
        )
        close(
            row["rebates"],
            sum((Decimal(f["rebate"]) for f in authoritative), Decimal(0)),
            "rebates booked once",
        )
        if source["kind"] == "ExecutionReport":
            assert source["report_id"] not in used_reports, "unique delivered report"
            used_reports.add(source["report_id"])
            if source["fill_id"]:
                assert source["exchange_ns"] <= key[0], "report causality"
                known += source["signed_quantity"]
        assert row["known"] == known, "delayed fill knowledge"
        max_knowledge_gap = max(max_knowledge_gap, abs(position - known))
        live = []
        for o in row["orders"]:
            assert o["live"] == (o["status"] in {"live", "partially_filled"}), (
                "live membership agrees with status"
            )
            execution = sum(
                f["quantity"] for f in authoritative if f["order_id"] == o["order_id"]
            )
            assert (
                o["remaining"] == o["quantity"] - execution and o["filled"] == execution
            ), "live conservation"
            if o["live"]:
                assert o["client_order_id"] not in pending, "live/pending disjoint"
                live.append(dict(side=o["side"], quantity=o["quantity"] - execution))
                if o["order_id"] in cancel_ids:
                    cancellation_reservations += 1
        projected = reachable(position, [*live, *pending.values()])
        enumerations += len(projected)
        assert min(projected) >= -case.cap and max(projected) <= case.cap, (
            "all partial-fill cap"
        )
        largest, smallest = max(largest, max(projected)), min(smallest, min(projected))
        previous_inventory = position
    assert not pending and known == summary["end_inventory"]
    if case.family == "capacity" and case.report_ns < 5_000_000:
        assert any(
            g["reason"] == "position_limit" for row in trace for g in row["gates"]
        ), "headroom rejection exercised"
    if case.family == "cancel-race":
        assert cancellation_reservations > 0, "cancel reservation interval exercised"
    if case.family == "shutdown-inflight":
        assert post_kill_accepts == 1, "inflight shutdown window"
        assert diagnostics["kill_switch_active"]
    if case.family == "session-pending":
        assert late_session_rejects == 2 and not terminal, "late entries rejected"

    # Independent rational cost-pool ledger. Remove average-cost *notional* on
    # closes; derive realized profit from cash plus remaining cost, rather than
    # Portfolio's price-difference realized accumulator.
    position, cash, pool = 0, 0, Fraction(0)
    fees, rebates = Decimal(0), Decimal(0)
    buy, sell, turnover, count = 0, 0, 0, 0
    levels: dict[int, dict[int, int]] = {1: {}, -1: {}}
    checkpoints: list[dict[str, Any]] = []
    checkpoint_keys: list[tuple[int, ...]] = []
    peak = Fraction(0)

    def ledger(mark: Fraction, timestamp: int) -> None:
        nonlocal peak
        checkpoint_keys.append(tuple(row["key"]))
        realized = cash + pool
        unrealized = position * mark - pool
        exposure = abs(position) * mark / 100
        peak = max(peak, exposure)
        gross_usd = (
            Decimal((realized + unrealized).numerator)
            / Decimal((realized + unrealized).denominator)
            * Decimal("0.01")
        )
        checkpoints.append(
            dict(
                timestamp_ns=timestamp,
                inventory=position,
                trade_cash_ticks=cash,
                average_cost_ticks=abs(pool / position) if position else None,
                realized_pnl_ticks=realized,
                unrealized_pnl_ticks=unrealized,
                gross_pnl_ticks=realized + unrealized,
                trade_cash=Fraction(cash, 100),
                cash=Decimal(cash) * Decimal("0.01") - fees + rebates,
                realized_pnl=realized / 100,
                unrealized_pnl=unrealized / 100,
                gross_pnl=gross_usd,
                turnover=Fraction(turnover, 100),
                current_gross_exposure=exposure,
                peak_gross_exposure=peak,
                mark_ticks=mark,
                fees=fees,
                rebates=rebates,
                net_pnl=Decimal((realized + unrealized).numerator)
                / Decimal((realized + unrealized).denominator)
                * Decimal("0.01")
                - fees
                + rebates,
                turnover_ticks=turnover,
                fill_count=count,
                buy_volume=buy,
                sell_volume=sell,
            )
        )

    def mark() -> Fraction:
        bid, ask = max(levels[1]), min(levels[-1])
        if case.mark == "conservative" and position:
            return Fraction(bid if position > 0 else ask)
        if case.mark == "microprice":
            return Fraction(
                ask * levels[1][bid] + bid * levels[-1][ask],
                levels[1][bid] + levels[-1][ask],
            )
        return Fraction(bid + ask, 2)

    for row in trace:
        source = row["source"]
        if source["kind"] == "MarketEvent" and source["channel"] is None:
            side, px, qty = source["side"], source["price_ticks"], source["quantity"]
            if source["event_type"] in {"SNAPSHOT", "ADD"}:
                levels[side][px] = levels[side].get(px, 0) + qty
            else:
                levels[side][px] -= qty
                assert levels[side][px] >= 0, "raw historical budget"
                if not levels[side][px]:
                    del levels[side][px]
        new_fills = row["fills"][count:]
        # Taker depth consumes the true book before the result's fill snapshots.
        for f in new_fills:
            if f["role"] == "taker":
                levels[-f["side"]][f["price"]] -= f["quantity"]
                if not levels[-f["side"]][f["price"]]:
                    del levels[-f["side"]][f["price"]]
        for f in new_fills:
            signed = f["side"] * f["quantity"]
            if position == 0 or position * signed > 0:
                pool += signed * f["price"]
            else:
                closing = min(abs(position), abs(signed))
                pool -= pool * Fraction(closing, abs(position))
                excess = abs(signed) - closing
                pool += f["side"] * excess * f["price"]
            position += signed
            cash -= signed * f["price"]
            fees += Decimal(f["fee"])
            rebates += Decimal(f["rebate"])
            turnover += abs(signed) * f["price"]
            buy += f["quantity"] if signed > 0 else 0
            sell += f["quantity"] if signed < 0 else 0
            count += 1
            ledger(mark(), f["timestamp"])
        if (
            source["kind"] == "MarketEvent"
            and source["channel"] is None
            and levels[1]
            and levels[-1]
        ):
            ledger(mark(), row["key"][0])
    assert len(checkpoints) == len(pnl) == len(inventory_rows), (
        "full accounting trajectory"
    )
    for expected_row, actual, inv in zip(checkpoints, pnl, inventory_rows, strict=True):
        for field, value in expected_row.items():
            if value is None:
                assert actual[field] is None, field
            else:
                close(actual[field], value, field)
        assert inv["inventory"] == expected_row["inventory"]
        close(
            inv["current_gross_exposure"],
            expected_row["current_gross_exposure"],
            "inventory exposure",
        )
        close(
            inv["peak_gross_exposure"],
            expected_row["peak_gross_exposure"],
            "inventory peak exposure",
        )
        close(
            actual["cash"],
            Decimal(actual["trade_cash_ticks"]) * Decimal("0.01")
            - Decimal(str(actual["fees"]))
            + Decimal(str(actual["rebates"])),
            "cash currency identity",
        )
        close(
            actual["net_pnl"],
            Decimal(str(actual["cash"]))
            + Decimal(actual["inventory"])
            * Decimal(str(actual["mark_ticks"]))
            * Decimal("0.01"),
            "marked cash identity",
        )
    assert position == summary["end_inventory"]
    first_loss = next(
        (
            i
            for i, point in enumerate(checkpoints)
            if point["net_pnl"] <= -saved_config.risk.max_loss
        ),
        None,
    )
    kill_key = checkpoint_keys[first_loss] if first_loss is not None else None
    for frame in trace:
        assert frame["kill"] == (
            kill_key is not None and tuple(frame["key"]) >= kill_key
        ), "independent sticky loss trigger"
    loss_events = [
        r
        for r in pl.read_parquet(directory / "risk_events.parquet").to_dicts()
        if r["reason"] == "max_loss"
    ]
    assert [r["timestamp_ns"] for r in loss_events] == (
        [kill_key[0]] if kill_key else []
    ), "loss event timing"
    close(summary["net_pnl"], checkpoints[-1]["net_pnl"], "summary final pnl")
    final = checkpoints[-1]
    assert metrics["inventory"]["end_of_session"] == position, "metrics final inventory"
    for name in ("realized_pnl", "unrealized_pnl", "gross_pnl"):
        close(
            metrics["pnl"][name],
            final[f"{name}_ticks"] * Fraction(1, 100),
            f"metrics {name}",
        )
    for name in ("fees", "rebates", "net_pnl"):
        close(metrics["pnl"][name], final[name], f"metrics {name}")
    assert metrics["trading_activity"]["buy_volume"] == buy
    assert metrics["trading_activity"]["sell_volume"] == sell
    close(
        metrics["trading_activity"]["turnover"],
        Fraction(turnover, 100),
        "metrics turnover",
    )
    assert diagnostics["open_orders_end"] == 0
    return dict(
        name=case.name,
        family=case.family,
        event_stream_sha256=stream,
        maker_volume=sum(q for _, _, q, _ in actual_makers),
        fill_count=len(fills),
        end_inventory=position,
        net_pnl=str(checkpoints[-1]["net_pnl"]),
        realized_ticks=str(checkpoints[-1]["realized_pnl_ticks"]),
        final_mark=str(checkpoints[-1]["mark_ticks"]),
        minimum_reachable_inventory=smallest,
        maximum_reachable_inventory=largest,
        reachable_state_checks=enumerations,
        maximum_knowledge_gap=max_knowledge_gap,
        cancellation_reservation_snapshots=cancellation_reservations,
        accepts_after_kill=post_kill_accepts,
        late_session_rejections=late_session_rejects,
        snapshot_count=len(checkpoints),
        loss_trigger_timestamp_ns=kill_key[0] if kill_key else None,
        session_timestamp_ns=6_000_000,
        last_economic_snapshot_ns=checkpoints[-1]["timestamp_ns"],
        last_channel_drain_ns=trace[-1]["key"][0],
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    destination = request.config.getoption("--exposure-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    measurements = []
    for case in CASES:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(tape(case), directory / "tape.csv")
        _, trace = observed_run(case, root)
        save(directory / "event_snapshots.json", trace)
        save_deterministic_outputs(directory)
        measured = audit(case, directory, trace)
        measured["artifact_sha256"] = {
            p.name: sha256_file(p)
            for p in sorted(directory.iterdir())
            if p.suffix == ".parquet"
            or p.name
            in {
                "tape.csv",
                "run_config.yaml",
                "summary.json",
                "event_snapshots.json",
                "deterministic_metrics.json",
                "deterministic_diagnostics.json",
            }
        }
        measurements.append(measured)
    save(
        root / "audit.json",
        dict(
            version=VERSION,
            case_count=len(CASES),
            base_config_sha256=sha256_file(BASE),
            cases=measurements,
        ),
    )
    return root, measurements


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_reservation_to_close(
    case: Case, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    audit(
        case,
        root / case.name,
        json.loads((root / case.name / "event_snapshots.json").read_text()),
    )


def test_complete_repeat(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, evidence = study
    repeat = []
    for case, original in zip(CASES, evidence, strict=True):
        directory = tmp_path / case.name
        directory.mkdir()
        write_csv(tape(case), directory / "tape.csv")
        _, trace = observed_run(case, tmp_path)
        save(directory / "event_snapshots.json", trace)
        save_deterministic_outputs(directory)
        measured = audit(case, directory, trace)
        assert measured == {k: v for k, v in original.items() if k != "artifact_sha256"}
        for name, digest in original["artifact_sha256"].items():
            assert sha256_file(directory / name) == digest
            assert (directory / name).read_bytes() == (
                root / case.name / name
            ).read_bytes()
        repeat.append(dict(measured, artifact_sha256=original["artifact_sha256"]))
    save(
        tmp_path / "audit.json",
        dict(
            version=VERSION,
            case_count=len(CASES),
            base_config_sha256=sha256_file(BASE),
            cases=repeat,
        ),
    )
    assert (tmp_path / "audit.json").read_bytes() == (root / "audit.json").read_bytes()


@pytest.mark.parametrize("family", sorted({c.family for c in CASES}))
def test_observation_is_pass_through(
    family: str, study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, _ = study
    case = next(c for c in CASES if c.family == family)
    result = run_backtest(
        config(case), tape(case), run_name=case.name, output_root=tmp_path
    )
    for name in [*(f"{t}.parquet" for t in TABLES), "summary.json", "run_config.yaml"]:
        assert (result.run_directory / name).read_bytes() == (
            root / case.name / name
        ).read_bytes()


@pytest.mark.parametrize(
    "damage",
    [
        "release_on_cancel_send",
        "net_opposite",
        "report_cash",
        "early_knowledge",
        "saved_tape",
        "saved_config",
        "metrics",
        "marked_pnl",
    ],
)
def test_corruption_controls(
    damage: str, study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, _ = study
    family = "cancel-race" if damage == "release_on_cancel_send" else "capacity"
    case = next(c for c in CASES if c.family == family and c.report_ns > 0)
    directory = tmp_path / case.name
    shutil.copytree(root / case.name, directory)
    trace = json.loads((directory / "event_snapshots.json").read_text())
    if damage == "release_on_cancel_send":
        row = next(
            r for r in trace if any(s["kind"] == "CancelRequest" for s in r["sends"])
        )
        row["orders"][0]["live"] = False
    elif damage == "net_opposite":
        row = next(r for r in trace if len(r["gates"]) == 2)
        row["gates"][1]["open"] = []
    elif damage == "report_cash":
        row = next(r for r in trace if r["fills"] and r["inventory"])
        row["cash_ticks"] = 0
    elif damage == "early_knowledge":
        row = next(r for r in trace if r["inventory"] != r["known"])
        row["known"] = row["inventory"]
    elif damage == "saved_tape":
        (directory / "tape.csv").write_text(
            (directory / "tape.csv").read_text().replace(",99,2", ",99,3", 1)
        )
    elif damage == "saved_config":
        (directory / "run_config.yaml").write_text(
            (directory / "run_config.yaml")
            .read_text()
            .replace("max_abs_inventory: 6", "max_abs_inventory: 7")
        )
    elif damage == "metrics":
        value = json.loads((directory / "metrics.json").read_text())
        value["pnl"]["net_pnl"] = 12345
        save(directory / "metrics.json", value)
        save_deterministic_outputs(directory)
    else:
        frame = pl.read_parquet(directory / "pnl.parquet")
        frame.with_columns(
            (pl.col("realized_pnl_ticks") + 1).alias("realized_pnl_ticks")
        ).write_parquet(directory / "pnl.parquet")
    with pytest.raises(AssertionError):
        audit(case, directory, trace)
