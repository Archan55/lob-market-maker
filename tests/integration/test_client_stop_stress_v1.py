"""Independent sent-quantity, exchange-time cash and signed cost-pool study.

Hand budgets define expected fills without reading engine fills. The audit
replays commands/sends/delivered reports and those budgets, then compares venue
and client snapshots, public tables and summaries. It imports no engine risk,
queue, book or portfolio arithmetic into the oracle.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.channels import ChannelDelivery, OrderedChannel
from lobmm.config import AppConfig, dump_config, load_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import load_events, write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest
from lobmm.portfolio import Portfolio
from lobmm.scheduler import Scheduler
from lobmm.strategy_runtime import StrategyRuntime

pytestmark = pytest.mark.integration
BASE = Path("configs/client_stop_stress_v1.yaml")
VERSION = "client-stop-stress-v1"
TABLES = (
    "orders",
    "fills",
    "pnl",
    "inventory",
    "quotes",
    "market",
    "risk_events",
    "markouts",
)


@dataclass(frozen=True)
class Case:
    family: str
    strategy: str = "fixed_spread"
    side: int = 1
    entry: int = 20
    cancel: int = 60
    report: int = 20
    horizon: int = 1100
    mark: str = "midpoint"

    @property
    def name(self) -> str:
        return f"{self.family}__{self.strategy}__{self.side:+d}__{self.mark}"


CASES = tuple(
    Case(family, strategy.value, side, **kwargs)
    for family, kwargs in (
        ("before-cancel", {}),
        ("at-cancel", {}),
        ("after-cancel", {}),
        ("full-fill", {}),
        ("round-trip", {}),
        ("late-entry", {"entry": 200}),
        ("unresolved-live", {"report": 400, "horizon": 140}),
        ("unresolved-ack", {"report": 400, "horizon": 160}),
        ("unresolved-entry", {"entry": 200, "horizon": 190}),
        ("past-tape-entry", {"entry": 1200, "horizon": 1400}),
        ("past-tape-cancel", {"cancel": 1200, "horizon": 1400}),
    )
    for strategy in StrategyName
    for side in (1, -1)
) + tuple(
    Case("at-cancel", side=side, mark=mark)
    for side in (1, -1)
    for mark in ("conservative", "microprice")
)


def tape(case: Case) -> list[MarketEvent]:
    price = 99 if case.side == 1 else 101
    rows = [
        (0, "ADD", 1, 99, 2),
        (0, "ADD", -1, 101, 2),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
    ]
    behind_ns = 201 if case.family == "late-entry" else 50
    if case.family not in {"unresolved-entry", "past-tape-entry"}:
        rows += [(behind_ns, "ADD", 1, 99, 10), (behind_ns, "ADD", -1, 101, 10)]
    if case.family == "late-entry":
        rows += [
            (210, "TRADE", case.side, price, 3),
            (280, "TRADE", case.side, price, 1),
        ]
    elif case.family not in {"unresolved-entry", "past-tape-entry"}:
        rows += [
            (100, "TRADE", case.side, price, 6 if case.family == "full-fill" else 3)
        ]
        if case.family in {"before-cancel", "at-cancel", "after-cancel"}:
            when = {"before-cancel": 159, "at-cancel": 160, "after-cancel": 161}[
                case.family
            ]
            rows += [(when, "TRADE", case.side, price, 1)]
        if case.family == "round-trip":
            rows += [(150, "TRADE", -case.side, 101 if case.side == 1 else 99, 3)]
    rows += [(1000, "ADD", 1, 90, 1)]
    return [
        MarketEvent(t, i, EventType(kind), Side(s), p, q)
        for i, (t, kind, s, p, q) in enumerate(rows, 1)
    ]


def config(case: Case, *, baseline: bool = False) -> AppConfig:
    raw = dump_config(load_config(BASE))
    raw["strategy"]["name"] = case.strategy
    raw["latency"].update(
        order_entry_ns=case.entry,
        cancellation_ns=case.cancel,
        fill_report_ns=case.report,
    )
    raw["backtest"].update(
        observation_end_timestamp_ns=case.horizon, mark_price=case.mark
    )
    if baseline:
        for key in (
            "shutdown_policy",
            "client_stop_timestamp_ns",
            "observation_end_timestamp_ns",
        ):
            raw["backtest"].pop(key)
        # Same observed historical horizon, but the venue is forced closed at
        # the client's stop to make the lifecycle counterfactual explicit.
        raw["backtest"]["end_timestamp_ns"] = 100
    return AppConfig.model_validate(raw)


def budgets(case: Case) -> list[tuple[int, int]]:
    """Two external units ahead, four own units, historical-first ties."""
    if case.family == "late-entry":
        return [(210, 1), (280, 1)]
    if case.family == "round-trip":
        return [(100, 1), (150, 1)]
    if case.family in {"unresolved-entry", "past-tape-entry"}:
        return []
    first = 4 if case.family == "full-fill" else 1
    return [(100, first)] + (
        [(159 if case.family == "before-cancel" else 160, 1)]
        if case.family in {"before-cancel", "at-cancel"}
        else []
    )


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def normalize_outputs(directory: Path) -> None:
    for name in ("metrics", "diagnostics"):
        raw = json.loads((directory / f"{name}.json").read_text())
        engineering = raw["engineering"] if name == "metrics" else raw
        for key in ("wall_clock_seconds", "events_per_second", "peak_memory_bytes"):
            engineering.pop(key, None)
        save(directory / f"deterministic_{name}.json", raw)


def observe(case: Case, root: Path) -> list[dict[str, Any]]:
    objects: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    original_send, original_step = OrderedChannel.send, Scheduler.run_one

    def capture(cls: type[Any], label: str, patch: pytest.MonkeyPatch) -> None:
        original = cls.__init__

        def initialize(obj: Any, *args: Any, **kwargs: Any) -> None:
            original(obj, *args, **kwargs)
            objects[label] = obj

        patch.setattr(cls, "__init__", initialize)

    def message(value: Any) -> dict[str, Any]:
        row = {"kind": type(value).__name__}
        if isinstance(value, (NewOrderRequest, CancelRequest, MarketEvent)):
            row.update(asdict(value))
        elif isinstance(value, ExecutionReport):
            row.update(
                client_order_id=value.client_order_id,
                report_id=value.report_id,
                type=value.report_type.value,
                reason=value.reason,
                signed_quantity=value.fill.signed_quantity if value.fill else 0,
                exchange_timestamp_ns=value.exchange_timestamp_ns,
                fill_id=value.fill.fill_id if value.fill else None,
            )
        return row

    def send(channel: Any, payload: Any, **kwargs: Any) -> Any:
        delivery = original_send(channel, payload, **kwargs)
        if isinstance(payload, (NewOrderRequest, CancelRequest)):
            sends.append(dict(message(payload), arrival_ns=delivery.timestamp_ns))
        return delivery

    def step(scheduler: Scheduler, handler: Callable[..., Any]) -> Any:
        def after(event: Any, current: Scheduler) -> None:
            sends.clear()
            handler(event, current)
            exchange, portfolio, runtime = (
                objects[x] for x in ("exchange", "portfolio", "runtime")
            )
            source = (
                event.payload.payload
                if isinstance(event.payload, ChannelDelivery)
                else event.payload
            )
            snapshot = portfolio.snapshot(timestamp_ns=event.timestamp_ns)
            trace.append(
                dict(
                    key=list(event.ordering_key),
                    source=message(source),
                    sends=list(sends),
                    inventory=portfolio.inventory,
                    known=runtime.known_inventory,
                    cash=portfolio.trade_cash_ticks,
                    fees=str(snapshot.fees),
                    rebates=str(snapshot.rebates),
                    mark=str(snapshot.mark_ticks),
                    realized=str(snapshot.realized_pnl_ticks),
                    gross=str(snapshot.gross_pnl_ticks),
                    net=str(snapshot.net_pnl),
                    live={
                        o.client_order_id: o.remaining_quantity
                        for o in exchange.live_orders
                    },
                    fills=[
                        (
                            f.exchange_fill_timestamp_ns,
                            int(f.side),
                            f.quantity,
                            f.price_ticks,
                            str(f.fee),
                            str(f.rebate),
                        )
                        for f in exchange.registry.fills
                    ],
                    managed=[q.client_order_id for q in runtime.strategy.active_quotes],
                )
            )

        return original_step(scheduler, after)

    with pytest.MonkeyPatch.context() as patch:
        for cls, label in (
            (Exchange, "exchange"),
            (Portfolio, "portfolio"),
            (StrategyRuntime, "runtime"),
        ):
            capture(cls, label, patch)
        patch.setattr(OrderedChannel, "send", send)
        patch.setattr(Scheduler, "run_one", step)
        run_backtest(config(case), tape(case), run_name=case.name, output_root=root)
    return trace


def audit(case: Case, directory: Path, trace: list[dict[str, Any]]) -> dict[str, Any]:
    assert event_stream_sha256(
        load_events(directory / "tape.csv")
    ) == event_stream_sha256(tape(case))
    assert dump_config(load_config(directory / "run_config.yaml")) == dump_config(
        config(case)
    )
    sent: dict[str, tuple[int, int]] = {}
    live: dict[str, int] = {}
    pending: set[str] = set()
    inventory = cash = known = 0
    signed_cost = Fraction(0)
    realized = Fraction(0)
    fees = rebates = Decimal(0)
    fills: list[tuple[Any, ...]] = []
    depths = {1: {}, -1: {}}
    mark = Fraction(0)
    max_gap = reachable_checks = 0
    pool_checks = 0
    terminal_ids: set[str] = set()
    observed_reports: set[str] = set()
    expected_reports: list[dict[str, Any]] = []
    client_unresolved: set[str] = set()
    cancel_pending: set[str] = set()
    cancel_sends: list[dict[str, Any]] = []
    hand = dict(budgets(case))

    def expect_report(
        timestamp: int,
        cid: str,
        kind: str,
        *,
        reason: str | None = None,
        units: int = 0,
        fill_id: str | None = None,
    ) -> None:
        expected_reports.append(
            dict(
                client_order_id=cid,
                type=kind,
                reason=reason,
                signed_quantity=units,
                fill_id=fill_id,
                exchange_timestamp_ns=timestamp,
            )
        )

    def close(actual: Any, expected: Any) -> None:
        assert abs(float(actual) - float(expected)) < 1e-10, (actual, expected)

    for row in trace:
        timestamp = row["key"][0]
        source = row["source"]
        kind = source["kind"]
        for request in row["sends"]:
            if request["kind"] == "NewOrderRequest":
                cid = request["client_order_id"]
                assert cid not in sent and request["send_timestamp_ns"] < 100
                assert request["quantity"] == 4 and request["price_ticks"] == (
                    99 if request["side"] == 1 else 101
                )
                sent[cid] = (request["side"], request["quantity"])
                pending.add(cid)
                client_unresolved.add(cid)
            elif request["kind"] == "CancelRequest":
                cancel_sends.append(
                    {
                        "client_order_id": request["client_order_id"],
                        "send_timestamp_ns": request["send_timestamp_ns"],
                    }
                )
        cancellations = {
            r["client_order_id"] for r in row["sends"] if r["kind"] == "CancelRequest"
        }
        if kind == "SessionEnd":
            assert cancellations == client_unresolved
        if kind == "NewOrderRequest" and timestamp <= 1000:
            cid = source["client_order_id"]
            pending.remove(cid)
            live[cid] = 4
            expect_report(timestamp, cid, "accepted")
        if kind == "CancelRequest":
            cid = source["client_order_id"]
            if timestamp <= 1000:
                if cid in live:
                    live.pop(cid)
                    terminal_ids.add(cid)
                    expect_report(timestamp, cid, "cancelled")
                else:
                    expect_report(
                        timestamp,
                        cid,
                        "cancel_rejected",
                        reason="order_not_live"
                        if cid in terminal_ids
                        else "unknown_order",
                    )
        if kind == "MarketEvent" and row["key"][2] == 10:
            side, price, quantity = (
                source["side"],
                source["price_ticks"],
                source["quantity"],
            )
            level = depths[side]
            if source["event_type"] == "ADD":
                level[price] = level.get(price, 0) + quantity
            else:
                level[price] = max(0, level.get(price, 0) - quantity)
                if timestamp in hand:
                    quantity = hand[timestamp]
                    fill_side = (
                        -case.side
                        if case.family == "round-trip" and timestamp == 150
                        else case.side
                    )
                    cid = next(cid for cid in live if sent[cid][0] == fill_side)
                    live[cid] -= quantity
                    if not live[cid]:
                        live.pop(cid)
                        terminal_ids.add(cid)
                    previous = inventory
                    units = fill_side * quantity
                    if inventory and inventory * units < 0:
                        closing = min(abs(inventory), quantity)
                        average = signed_cost / inventory
                        realized += (
                            closing * (price - average) * (1 if inventory > 0 else -1)
                        )
                        signed_cost -= closing * (1 if inventory > 0 else -1) * average
                        inventory += units
                        if previous * inventory < 0:
                            signed_cost = Fraction(inventory * price)
                    else:
                        inventory += units
                        signed_cost += units * price
                    cash -= units * price
                    fee = Decimal(quantity) * (
                        Decimal("0.002")
                        + Decimal(price) * Decimal("0.01") * Decimal("0.0001")
                    )
                    rebate = Decimal(quantity) * Decimal("0.0005")
                    fees += fee
                    rebates += rebate
                    fills.append(
                        (timestamp, fill_side, quantity, price, str(fee), str(rebate))
                    )
                    expect_report(
                        timestamp,
                        cid,
                        "partial_fill" if cid in live else "fill",
                        units=units,
                        fill_id=f"F{len(fills):012d}",
                    )
            bids = [p for p, q in depths[1].items() if q]
            asks = [p for p, q in depths[-1].items() if q]
            if bids and asks:
                bid, ask = max(bids), min(asks)
                if case.mark == "conservative" and inventory:
                    mark = Fraction(bid if inventory > 0 else ask)
                elif case.mark == "microprice":
                    bq, aq = depths[1][bid], depths[-1][ask]
                    mark = Fraction(ask * bq + bid * aq, bq + aq)
                else:
                    mark = Fraction(bid + ask, 2)
        if kind == "ExecutionReport":
            assert source["report_id"] not in observed_reports
            observed_reports.add(source["report_id"])
            assert expected_reports, "unexpected or duplicate execution report"
            expected_report = expected_reports.pop(0)
            assert {key: source[key] for key in expected_report} == expected_report
            assert timestamp == expected_report["exchange_timestamp_ns"] + case.report
            known += expected_report["signed_quantity"]
            cid = expected_report["client_order_id"]
            if expected_report["type"] in {"cancel_rejected", "cancelled"}:
                cancel_pending.discard(cid)
            if expected_report["type"] in {"cancelled", "fill"} or (
                expected_report["type"] == "cancel_rejected"
                and expected_report["reason"] == "order_not_live"
            ):
                client_unresolved.discard(cid)
                cancel_pending.discard(cid)
            if (
                expected_report["type"] == "accepted"
                and timestamp >= 100
                and cid not in cancel_pending
            ):
                assert cid in cancellations, (
                    "shutdown must retry after an early unknown cancel"
                )
            # The early unknown cancel is not terminal client knowledge.
            if source["reason"] == "unknown_order":
                assert source["client_order_id"] in row["managed"]
        cancel_pending.update(cancellations)
        expected = [list(f) for f in fills]
        assert row["fills"] == expected or row["fills"] == fills
        assert row["inventory"] == inventory and row["cash"] == cash
        assert row["known"] == known and row["live"] == live
        close(row["fees"], fees)
        close(row["rebates"], rebates)
        close(row["realized"], realized)
        close(row["mark"], mark)
        close(row["gross"], cash + inventory * mark)
        close(
            row["net"],
            Decimal(str(float((cash + inventory * mark) / 100))) - fees + rebates,
        )
        # Signed rational cost-pool identity, independent of Portfolio averages.
        close(realized + inventory * mark - signed_cost, cash + inventory * mark)
        pool_checks += 1
        reservations = [(sent[cid][0], q) for cid, q in live.items()] + [
            sent[cid] for cid in pending
        ]
        reachable = {
            inventory
            + sum(
                side * units
                for (side, _), units in zip(reservations, quantities, strict=True)
            )
            for quantities in itertools.product(
                *(range(q + 1) for _, q in reservations)
            )
        }
        assert all(-4 <= outcome <= 4 for outcome in reachable)
        reachable_checks += len(reachable)
        max_gap = max(max_gap, abs(inventory - known))
    assert all(
        report["exchange_timestamp_ns"] + case.report > case.horizon
        for report in expected_reports
    ), "expected report omitted within observation horizon"
    diagnostics = json.loads((directory / "diagnostics.json").read_text())
    metrics = json.loads((directory / "metrics.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    stop = diagnostics["client_stop"]
    assert diagnostics["end_inventory"] == inventory
    assert stop["known_inventory"] == known
    assert {
        x["client_order_id"]: x["remaining_quantity"]
        for x in stop["outstanding_orders"]
    } == live
    assert {x["client_order_id"] for x in stop["in_flight_entries"]} == pending
    assert stop["reachable_inventory_min"] == min(reachable)
    assert stop["reachable_inventory_max"] == max(reachable)
    assert stop["accounting_complete"] == (not live and not pending)
    assert set(stop["unresolved_client_order_ids"]) == client_unresolved
    assert stop["client_knowledge_complete"] == (not client_unresolved)
    assert stop["cancel_requests"] == cancel_sends
    assert summary["client_stop"] == stop == metrics["client_stop"]
    close(
        metrics["pnl"]["net_pnl"],
        (cash + inventory * mark) / 100 - Fraction(fees) + Fraction(rebates),
    )
    actual_fills = pl.read_parquet(directory / "fills.parquet").to_dicts()
    assert [(f["exchange_fill_timestamp_ns"], f["quantity"]) for f in actual_fills] == [
        (t, q) for t, q in budgets(case) if t <= case.horizon
    ]
    final = pl.read_parquet(directory / "pnl.parquet").to_dicts()[-1]
    assert (
        final["timestamp_ns"] == case.horizon
        and final["inventory"] == inventory
        and final["trade_cash_ticks"] == cash
    )
    market = pl.read_parquet(directory / "market.parquet").to_dicts()
    observed_tape = [
        event for event in tape(case) if event.timestamp_ns <= case.horizon
    ]
    assert len(market) == len(observed_tape) == diagnostics["events_processed"]
    assert diagnostics["event_stream_sha256"] == event_stream_sha256(observed_tape)
    assert all(row["timestamp_ns"] <= min(1000, case.horizon) for row in market)
    quotes = pl.read_parquet(directory / "quotes.parquet").to_dicts()
    assert all(q["timestamp_ns"] < 100 for q in quotes)
    assert all(
        row["future_timestamp_ns"] is None
        for row in pl.read_parquet(directory / "markouts.parquet").to_dicts()
    )
    return dict(
        name=case.name,
        family=case.family,
        strategy=case.strategy,
        side=case.side,
        snapshots=len(trace),
        reachable_checks=reachable_checks,
        cost_pool_checks=pool_checks,
        inventory=inventory,
        known_inventory=known,
        cash_ticks=cash,
        fees=str(fees),
        rebates=str(rebates),
        realized_ticks=str(realized),
        mark_ticks=str(mark),
        maximum_knowledge_gap=max_gap,
        remaining_live_quantity=sum(live.values()),
        pending_entry_quantity=sum(sent[cid][1] for cid in pending),
        reachable_min=min(reachable),
        reachable_max=max(reachable),
        cancel_send_count=len(stop["cancel_requests"]),
        accounting_complete=stop["accounting_complete"],
        client_knowledge_complete=stop["client_knowledge_complete"],
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    destination = request.config.getoption("--client-stop-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(tape(case), directory / "tape.csv")
        trace = observe(case, root)
        save(directory / "snapshots.json", trace)
        measured = audit(case, directory, trace)
        normalize_outputs(directory)
        measured["hashes"] = {
            path.name: sha256_file(path)
            for path in sorted(directory.iterdir())
            if path.suffix == ".parquet"
            or path.name
            in {
                "snapshots.json",
                "tape.csv",
                "run_config.yaml",
                "summary.json",
                "deterministic_metrics.json",
                "deterministic_diagnostics.json",
            }
        }
        results.append(measured)
    save(
        root / "audit.json",
        dict(
            version=VERSION,
            case_count=len(CASES),
            base_config_sha256=sha256_file(BASE),
            cases=results,
        ),
    )
    return root, results


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_independent_client_stop_audit(
    case: Case, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    audit(
        case,
        root / case.name,
        json.loads((root / case.name / "snapshots.json").read_text()),
    )


def test_full_repeat_and_saved_input_replay(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, evidence = study
    for case, original in zip(CASES, evidence, strict=True):
        trace = observe(case, tmp_path)
        directory = tmp_path / case.name
        write_csv(tape(case), directory / "tape.csv")
        save(directory / "snapshots.json", trace)
        normalize_outputs(directory)
        assert json.loads(json.dumps(trace)) == json.loads(
            (root / case.name / "snapshots.json").read_text()
        )
        assert audit(case, tmp_path / case.name, trace) == {
            k: v for k, v in original.items() if k != "hashes"
        }
        for name in original["hashes"]:
            assert sha256_file(directory / name) == original["hashes"][name]
            assert (tmp_path / case.name / name).read_bytes() == (
                root / case.name / name
            ).read_bytes()
    case = CASES[0]
    run_backtest(
        load_config(root / case.name / "run_config.yaml"),
        load_events(root / case.name / "tape.csv"),
        run_name="saved-replay",
        output_root=tmp_path,
    )
    for name in [*(f"{t}.parquet" for t in TABLES), "run_config.yaml"]:
        assert (tmp_path / "saved-replay" / name).read_bytes() == (
            root / case.name / name
        ).read_bytes()


@pytest.mark.parametrize(
    "corruption",
    [
        "cash",
        "known",
        "live",
        "fee",
        "fill",
        "late_decision",
        "missing_report",
        "duplicate_report",
    ],
)
def test_auditor_rejects_corruption(
    corruption: str, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    case = CASES[0]
    trace = copy.deepcopy(json.loads((root / case.name / "snapshots.json").read_text()))
    row = next(row for row in trace if row["inventory"])
    if corruption in {"cash", "known"}:
        row[corruption] += 1
    elif corruption == "live":
        row["live"] = {}
    elif corruption == "fee":
        row["fees"] = "123"
    elif corruption == "fill":
        row["fills"][0][2] += 1
    elif corruption == "late_decision":
        request = next(
            request
            for row in trace
            for request in row["sends"]
            if request["kind"] == "NewOrderRequest"
        )
        request["send_timestamp_ns"] = 100
    elif corruption == "missing_report":
        trace[:] = [
            row
            for row in trace
            if not (
                row["source"]["kind"] == "ExecutionReport"
                and row["source"]["signed_quantity"]
            )
        ]
        for row in trace:
            row["known"] = 0
    else:
        index = next(
            i
            for i, row in enumerate(trace)
            if row["source"]["kind"] == "ExecutionReport"
            and row["source"]["signed_quantity"]
        )
        duplicate = copy.deepcopy(trace[index])
        duplicate["source"]["report_id"] = "bad-new-report-id"
        trace.insert(index + 1, duplicate)
    with pytest.raises(AssertionError):
        audit(case, root / case.name, trace)


def test_forced_expiry_counterfactual(tmp_path: Path) -> None:
    case = Case("at-cancel")
    result = run_backtest(
        config(case, baseline=True),
        tape(case),
        run_name="forced-expiry",
        output_root=tmp_path,
    )
    assert "client_stop" not in result.diagnostics
    fills = pl.read_parquet(result.run_directory / "fills.parquet").to_dicts()
    assert [
        (fill["exchange_fill_timestamp_ns"], fill["quantity"]) for fill in fills
    ] == [(100, 1)]
    orders = pl.read_parquet(result.run_directory / "orders.parquet").to_dicts()
    assert sum(row["new_status"] == "expired" for row in orders) == 2


def test_reports_expose_unresolved_state_and_guard_shutdown_comparisons(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    from lobmm.report import ReportError, compare_runs, human_readable_summary

    root, _ = study
    case = Case("unresolved-live", report=400, horizon=140)
    text = human_readable_summary(root / case.name)
    assert "Unresolved venue orders / entries: 2 / 0" in text
    assert "Reachable inventory: [-3, 4]" in text
    cases = [Case("at-cancel", strategy=name.value) for name in StrategyName]
    assert (
        compare_runs(
            [root / case.name for case in cases],
            output_directory=tmp_path / "comparison",
        ).height
        == 3
    )
    raw = dump_config(config(cases[0]))
    raw["backtest"]["observation_end_timestamp_ns"] += 1
    changed = run_backtest(
        AppConfig.model_validate(raw),
        tape(cases[0]),
        run_name="changed-horizon",
        output_root=tmp_path,
    )
    with pytest.raises(ReportError, match="shutdown policy"):
        compare_runs(
            [root / cases[0].name, changed.run_directory],
            output_directory=tmp_path / "invalid",
        )
