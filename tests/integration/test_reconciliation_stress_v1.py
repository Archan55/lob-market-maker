"""Independent scalar history, delivery schedule and client-evidence oracle.

No exchange, queue, portfolio or production query helper derives expectations.
Observers copy public handler inputs and client outputs; scalar trade budgets
derive immutable execution records, costs, status snapshots and causal receipts.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.channels import ChannelDelivery
from lobmm.config import AppConfig, dump_config, load_config
from lobmm.data.loaders import load_events, write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.reconciliation import ReconciliationPlan
from lobmm.scheduler import Scheduler
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import ClientKnowledge, TransportFault, TransportFaultPlan

VERSION = "reconciliation-stress-v1"
BASE = Path("configs/client_stop_stress_v1.yaml")
pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Case:
    name: str
    strategy: str = "fixed_spread"
    side: int = 1
    queries: tuple[int, ...] = (180,)
    request: int = 0
    response: int = 20
    lag: int = 0
    limit: int | None = None
    state: bool = True
    horizon: int = 1100
    report: int = 20
    trade: int = 160
    second: int = 2
    loss: str = "all"
    drop_cancel: bool = False
    drop_entries: bool = False
    query_fault: str = ""


MODES = (
    Case("complete"),
    Case("snapshot-before-cancel", queries=(120,), response=300),
    Case("later-query", queries=(120, 180), response=300),
    Case("request-delay", queries=(120,), request=60),
    Case("stale", lag=80),
    Case("absent-stale", lag=1000),
    Case("truncated", limit=1),
    Case("no-detail", limit=0),
    Case("detail-only", state=False),
    Case("response-missing", query_fault="response-drop"),
    Case("request-missing", query_fault="request-drop"),
    Case("duplicate", queries=(180, 220), query_fault="duplicate"),
    Case("before-horizon", horizon=199),
    Case("at-horizon", horizon=200),
    Case("late-original", report=400, loss="none", response=0),
    Case("mixed-late-original", report=400, loss="first", response=0),
    Case("live-order", drop_cancel=True),
    Case("beyond-tape", queries=(1050,), drop_cancel=True),
    Case("absent-order", drop_entries=True),
    Case("before-cancel-trade", trade=159),
    Case("after-cancel-trade", trade=161),
    Case("full-fill-race", second=3),
    Case("zero-response", response=0),
    Case("at-stop", queries=(100,), response=0),
    Case("request-beyond-horizon", queries=(180,), request=30, horizon=200),
    Case("query-at-horizon", queries=(200,), response=0, horizon=200),
    Case("covered-terminal-after-tape", queries=(1050,)),
    Case("stale-terminal", lag=10),
)
CASES = tuple(
    replace(
        mode,
        name=f"{mode.name}__{strategy.value}__{side}",
        strategy=strategy.value,
        side=side,
    )
    for strategy in StrategyName
    for side in (1, -1)
    for mode in MODES
)


def configuration(case: Case) -> AppConfig:
    raw = dump_config(load_config(BASE))
    raw["strategy"]["name"] = case.strategy
    raw["latency"]["fill_report_ns"] = case.report
    raw["backtest"]["observation_end_timestamp_ns"] = case.horizon
    return AppConfig.model_validate(raw)


def tape(case: Case) -> list[MarketEvent]:
    rows = [
        (0, "ADD", 1, 99, 2),
        (0, "ADD", -1, 101, 2),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (50, "ADD", 1, 99, 10),
        (50, "ADD", -1, 101, 10),
        (100, "TRADE", case.side, 99 if case.side == 1 else 101, 3),
        (case.trade, "TRADE", case.side, 99 if case.side == 1 else 101, case.second),
        (1000, "ADD", 1, 90, 1),
    ]
    return [
        MarketEvent(t, i, EventType(k), Side(s), p, q)
        for i, (t, k, s, p, q) in enumerate(rows, 1)
    ]


def faults(case: Case) -> TransportFaultPlan:
    rules = []
    if case.loss != "none":
        for i in range(8) if case.loss == "all" else (2,):
            rules.append(TransportFault("execution_reports", i, "drop"))
    if case.drop_cancel:
        rules.append(
            TransportFault("cancellations", 0 if case.side == 1 else 1, "drop")
        )
    if case.drop_entries:
        rules.extend(TransportFault("new_orders", i, "drop") for i in (0, 1))
    if case.query_fault == "duplicate":
        rules.extend(
            TransportFault(channel, i, "duplicate")
            for channel in ("reconciliation_requests", "reconciliation_responses")
            for i in range(len(case.queries))
        )
    elif case.query_fault:
        channel = (
            "reconciliation_requests"
            if case.query_fault == "request-drop"
            else "reconciliation_responses"
        )
        rules.append(TransportFault(channel, 0, "drop"))
    return TransportFaultPlan(tuple(rules))


def plan(case: Case) -> ReconciliationPlan:
    return ReconciliationPlan(
        case.queries, case.request, case.response, case.lag, case.limit, case.state
    )


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def observe(case: Case, root: Path) -> list[dict[str, Any]]:
    objects: dict[str, Any] = {}
    trace = []
    step = Scheduler.run_one

    def observed(scheduler: Any, handler: Any) -> Any:
        def after(event: Any, current: Any) -> None:
            handler(event, current)
            source = event.payload
            trace.append(
                dict(
                    key=list(event.ordering_key),
                    channel=source.channel_name
                    if isinstance(source, ChannelDelivery)
                    else None,
                    sequence=source.channel_sequence
                    if isinstance(source, ChannelDelivery)
                    else None,
                    known=objects["runtime"].known_inventory,
                    knowledge=copy.deepcopy(objects["knowledge"].artifact()),
                )
            )

        return step(scheduler, after)

    with pytest.MonkeyPatch.context() as patch:
        for cls, key in ((ClientKnowledge, "knowledge"), (StrategyRuntime, "runtime")):

            def wrap(original: Any, label: str) -> Any:
                def init(obj: Any, *args: Any, **kwargs: Any) -> None:
                    original(obj, *args, **kwargs)
                    objects[label] = obj

                return init

            patch.setattr(cls, "__init__", wrap(cls.__init__, key))
        patch.setattr(Scheduler, "run_one", observed)
        run_backtest(
            configuration(case),
            tape(case),
            run_name=case.name,
            output_root=root,
            transport_faults=faults(case),
            reconciliation=plan(case),
        )
    return trace


def history(case: Case) -> tuple[list[str], list[dict[str, Any]]]:
    """Two external units ahead; the first trade consumes 2+1, then own units.

    Cancels at 160 follow historical trades at the same timestamp. No external
    units are ahead after trade 100; ten added at 50 were behind both quotes.
    """
    config = configuration(case)
    cids = [f"{config.strategy.strategy_id}-{i:012d}" for i in (1, 2)]
    reports: list[dict[str, Any]] = []
    price = 99 if case.side == 1 else 101
    target = cids[0 if case.side == 1 else 1]
    remaining = dict.fromkeys(cids, 4)
    status = dict.fromkeys(cids, "live")

    def report(
        cid: str, kind: str, time: int, fill: dict[str, Any] | None = None
    ) -> None:
        reports.append(
            dict(
                cid=cid,
                type=kind,
                time=time,
                status=status[cid],
                remaining=remaining[cid],
                fill=fill,
            )
        )

    if case.drop_entries:
        for cid in cids:
            reports.append(
                dict(
                    cid=cid,
                    type="cancel_rejected",
                    time=160,
                    status="rejected",
                    remaining=0,
                    fill=None,
                    reason="unknown_order",
                )
            )
        return cids, reports
    for cid in cids:
        report(cid, "accepted", 20)

    def fill(time: int, quantity: int) -> None:
        remaining[target] -= quantity
        status[target] = "filled" if remaining[target] == 0 else "partially_filled"
        number = 1 + sum(r["fill"] is not None for r in reports)
        fee = Decimal(quantity) * (
            Decimal("0.002") + Decimal(price) * Decimal("0.01") * Decimal("0.0001")
        )
        detail = dict(
            fill_id=f"F{number:012d}",
            order_id=f"O{1 if case.side == 1 else 2:012d}",
            client_order_id=target,
            strategy_id=config.strategy.strategy_id,
            side=case.side,
            quantity=quantity,
            price_ticks=price,
            liquidity_role="maker",
            exchange_fill_timestamp_ns=time,
            fee=float(fee),
            rebate=float(Decimal(quantity) * Decimal("0.0005")),
        )
        report(
            target,
            "fill" if status[target] == "filled" else "partial_fill",
            time,
            detail,
        )

    fill(100, 1)
    if case.trade <= 160 or case.drop_cancel:
        fill(case.trade, min(3, case.second))
    for cid in cids:
        if case.drop_cancel and cid == target:
            continue
        if status[cid] != "filled":
            status[cid] = "cancelled"
            report(cid, "cancelled", 160)
        else:
            report(cid, "cancel_rejected", 160)
    # The selected tapes contain no later executions. These histories are
    # independently derived inputs to the query oracle, not engine snapshots.
    return cids, reports


def expected_response(case: Case, sequence: int) -> dict[str, Any]:
    cids, reports = history(case)
    request_ns = case.queries[sequence]
    arrival = request_ns + case.request
    as_of = min(max(0, arrival - case.lag), 1000)
    orders = []
    for cid in cids:
        candidates = [
            r
            for r in reports
            if r["cid"] == cid
            and r["time"] <= as_of
            and r.get("reason") != "unknown_order"
        ]
        state = candidates[-1] if candidates and case.state else None
        details = [r["fill"] for r in candidates if r["fill"] is not None]
        selected = details[: case.limit]
        orders.append(
            dict(
                client_order_id=cid,
                state_timestamp_ns=state["time"] if state else None,
                status=state["status"] if state else None,
                remaining_quantity=state["remaining"] if state else None,
                fills=selected,
                execution_detail_complete_as_of=bool(candidates)
                and len(selected) == len(details),
            )
        )
    return dict(
        query_id=f"Q{sequence:04d}",
        request_timestamp_ns=request_ns,
        exchange_snapshot_timestamp_ns=arrival,
        as_of_timestamp_ns=as_of,
        tape_end_timestamp_ns=1000,
        snapshot_within_tape=arrival <= 1000,
        orders=orders,
    )


def audit(case: Case, directory: Path, trace: list[dict[str, Any]]) -> dict[str, Any]:
    assert load_events(directory / "tape.csv") == tape(case)
    assert dump_config(load_config(directory / "run_config.yaml")) == dump_config(
        configuration(case)
    )
    evidence = json.loads((directory / "reconciliation.json").read_text())
    assert {k: evidence[k] for k in plan(case).artifact()} == json.loads(
        json.dumps(plan(case).artifact())
    )
    cids, reports = history(case)
    responses = []
    receipts = []
    expected_envelopes = []
    expected_requests = [
        dict(query_id=f"Q{i:04d}", request_timestamp_ns=q, client_order_ids=cids)
        for i, q in enumerate(case.queries)
    ]
    assert evidence["requests"] == expected_requests
    for i, q in enumerate(case.queries):
        arrival = q + case.request
        if case.query_fault == "request-drop" and i == 0:
            continue
        copies = 2 if case.query_fault == "duplicate" else 1
        if arrival <= case.horizon:
            expected_envelopes.extend(
                (
                    "reconciliation_requests",
                    i,
                    arrival,
                    1 if case.request == 0 else 0,
                    20,
                )
                for _ in range(copies)
            )
            response = expected_response(case, i)
            responses.append(response)
            if case.query_fault == "response-drop" and i == 0:
                continue
            notification = arrival + case.response
            if notification <= case.horizon:
                wave = (1 if case.request == 0 else 0) if case.response == 0 else 0
                expected_envelopes.extend(
                    ("reconciliation_responses", i, notification, wave, 31)
                    for _ in range(copies)
                )
                receipts.append((i, notification, response))
    assert evidence["responses"] == responses
    actual_envelopes = [
        (r["channel"], r["sequence"], r["key"][0], r["key"][1], r["key"][2])
        for r in trace
        if r["channel"] and r["channel"].startswith("reconciliation_")
    ]
    assert sorted(actual_envelopes) == sorted(expected_envelopes)

    filled: dict[str, dict[str, Any]] = {}
    remaining = dict.fromkeys(cids, 4)
    terminal: set[str] = set()
    seen: set[tuple[str, int]] = set()
    notification_times = {}
    expected_receipts = []
    scheduled_reports = {
        i: r
        for i, r in enumerate(reports)
        if case.loss == "none" or (case.loss == "first" and i != 2)
    }
    scheduled_responses = {
        i: (notification, response) for i, notification, response in receipts
    }

    def learn(detail: dict[str, Any], notification: int) -> bool:
        identity = detail["fill_id"]
        if identity in filled:
            assert filled[identity] == detail
            return False
        filled[identity] = detail
        notification_times[identity] = notification
        cid = detail["client_order_id"]
        notified = sum(
            f["quantity"] for f in filled.values() if f["client_order_id"] == cid
        )
        remaining[cid] = min(remaining[cid], 4 - notified)
        return True

    def knowledge() -> dict[str, Any]:
        known = minimum = maximum = 0
        orders = []
        for cid, side in zip(cids, (1, -1), strict=True):
            notified = sum(
                f["quantity"] for f in filled.values() if f["client_order_id"] == cid
            )
            lower = 4 - remaining[cid]
            upper = lower if cid in terminal else 4
            known += side * notified
            minimum += side * (lower if side == 1 else upper)
            maximum += side * (upper if side == 1 else lower)
            orders.append(
                dict(
                    client_order_id=cid,
                    side=side,
                    sent_quantity=4,
                    notified_fill_quantity=notified,
                    executed_quantity_min=lower,
                    executed_quantity_max=upper,
                    terminal_report_received=cid in terminal,
                    reservation_quantity=0 if cid in terminal else 4 - notified,
                    missing_fill_quantity_min=max(0, lower - notified),
                )
            )
        return dict(
            known_inventory=known,
            inventory_min=minimum,
            inventory_max=maximum,
            complete=all(
                o["terminal_report_received"] and o["missing_fill_quantity_min"] == 0
                for o in orders
            ),
            orders=orders,
        )

    # Verify all original report receipts from the independently derived history,
    # including losses and late originals. Query copies are independently listed.
    actual_original = [
        (r["sequence"], r["key"][0])
        for r in trace
        if r["channel"] == "execution_reports"
    ]
    expected_original = [
        (i, r["time"] + case.report)
        for i, r in scheduled_reports.items()
        if r["time"] + case.report <= case.horizon
    ]
    assert actual_original == expected_original
    for row in trace:
        channel, sequence, time = row["channel"], row["sequence"], row["key"][0]
        identity = (channel, sequence)
        if channel == "execution_reports":
            report = scheduled_reports[sequence]
            if report.get("reason") != "unknown_order":
                if report["fill"] is not None:
                    learn(report["fill"], time)
                remaining[report["cid"]] = min(
                    remaining[report["cid"]], report["remaining"]
                )
                if report["status"] in {"filled", "cancelled", "expired", "rejected"}:
                    terminal.add(report["cid"])
        elif channel == "reconciliation_responses" and identity not in seen:
            seen.add(identity)
            notification, response = scheduled_responses[sequence]
            assert notification == time
            recovered = []
            for order in response["orders"]:
                for detail in order["fills"]:
                    if learn(detail, time):
                        recovered.append(detail["fill_id"])
                if order["status"] is not None:
                    cid = order["client_order_id"]
                    remaining[cid] = min(remaining[cid], order["remaining_quantity"])
                    if order["status"] in {
                        "filled",
                        "cancelled",
                        "expired",
                        "rejected",
                    }:
                        terminal.add(cid)
            expected_receipts.append(
                dict(
                    query_id=response["query_id"],
                    response_timestamp_ns=time,
                    as_of_timestamp_ns=response["as_of_timestamp_ns"],
                    recovered_fill_ids=recovered,
                    client_knowledge=knowledge(),
                )
            )
        expected = knowledge()
        # Before strategy sends its two entries, knowledge is empty. Sending is
        # one handler action at time zero, after the initial historical depth.
        if row["knowledge"]["orders"]:
            assert row["knowledge"] == expected
        else:
            assert row["known"] == 0 and time == 0
        assert row["known"] == expected["known_inventory"]
    assert evidence["receipts"] == expected_receipts
    stop = json.loads((directory / "summary.json").read_text())["client_stop"]
    assert stop["client_knowledge"] == knowledge()
    assert set(stop["unresolved_client_order_ids"]) == set(cids) - terminal
    assert stop["known_inventory"] == knowledge()["known_inventory"]
    ledger = evidence["client_execution_ledger"]
    assert ledger["fills"] == list(filled.values())
    assert ledger["known_inventory"] == sum(
        f["side"] * f["quantity"] for f in filled.values()
    )
    assert ledger["trade_cash_ticks"] == sum(
        -f["side"] * f["quantity"] * f["price_ticks"] for f in filled.values()
    )
    price = 99 if case.side == 1 else 101
    units = sum(f["quantity"] for f in filled.values())
    assert Decimal(ledger["fees"]) == Decimal(units) * (
        Decimal("0.002") + Decimal(price) * Decimal("0.000001")
    )
    assert Decimal(ledger["rebates"]) == Decimal(units) * Decimal("0.0005")
    expected_fills = [
        r["fill"]
        for r in reports
        if r["fill"] is not None and r["time"] <= case.horizon
    ]
    actual_fills = pl.read_parquet(directory / "fills.parquet").to_dicts()
    assert len(actual_fills) == len(expected_fills)
    for actual, detail in zip(actual_fills, expected_fills, strict=True):
        assert {key: actual[key] for key in detail} == detail
        assert actual["strategy_notification_timestamp_ns"] == notification_times.get(
            detail["fill_id"]
        )
    pnl = pl.read_parquet(directory / "pnl.parquet").to_dicts()[-1]
    executed = sum(f["quantity"] for f in expected_fills)
    assert pnl["inventory"] == case.side * executed
    assert pnl["trade_cash_ticks"] == -case.side * executed * price
    assert (
        abs(
            pnl["net_pnl"]
            - float(
                Decimal(executed)
                * (
                    Decimal("0.01")
                    - Decimal("0.002")
                    - Decimal(price) * Decimal("0.000001")
                    + Decimal("0.0005")
                )
            )
        )
        < 1e-12
    )
    return dict(
        name=case.name,
        checkpoints=len(trace),
        executed_units=executed,
        known_units=units,
        recovered_units=sum(
            filled[f]["quantity"]
            for receipt in expected_receipts
            for f in receipt["recovered_fill_ids"]
        ),
        client_complete=knowledge()["complete"],
        venue_complete=stop["accounting_complete"],
        responses_received=len(expected_receipts),
        requests=len(case.queries),
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    destination = request.config.getoption("--reconciliation-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(tape(case), directory / "tape.csv")
        save(directory / "case.json", asdict(case))
        trace = observe(case, root)
        save(directory / "snapshots.json", trace)
        result = audit(case, directory, trace)
        baseline = run_backtest(
            configuration(case),
            tape(case),
            run_name="no-recovery",
            output_root=directory,
            transport_faults=faults(case),
        )
        for filename in (
            "orders.parquet",
            "inventory.parquet",
            "pnl.parquet",
            "market.parquet",
            "quotes.parquet",
            "risk_events.parquet",
            "markouts.parquet",
            "run_config.yaml",
        ):
            assert (directory / filename).read_bytes() == (
                baseline.run_directory / filename
            ).read_bytes()
        assert (
            pl.read_parquet(directory / "fills.parquet")
            .drop("strategy_notification_timestamp_ns", strict=False)
            .equals(
                pl.read_parquet(baseline.run_directory / "fills.parquet").drop(
                    "strategy_notification_timestamp_ns", strict=False
                )
            )
        )
        result["baseline_known_inventory"] = baseline.diagnostics["client_stop"][
            "known_inventory"
        ]
        result["baseline_client_complete"] = baseline.diagnostics["client_stop"][
            "client_knowledge_complete"
        ]
        for folder in (directory, baseline.run_directory):
            for name in ("metrics", "diagnostics"):
                data = json.loads((folder / f"{name}.json").read_text())
                engineering = data["engineering"] if name == "metrics" else data
                for key in (
                    "wall_clock_seconds",
                    "events_per_second",
                    "peak_memory_bytes",
                ):
                    engineering.pop(key, None)
                save(folder / f"deterministic_{name}.json", data)
        result["hashes"] = {
            str(p.relative_to(directory)): sha256_file(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and (
                p.suffix == ".parquet"
                or p.name
                in {
                    "case.json",
                    "tape.csv",
                    "snapshots.json",
                    "run_config.yaml",
                    "summary.json",
                    "transport_faults.json",
                    "reconciliation.json",
                    "deterministic_metrics.json",
                    "deterministic_diagnostics.json",
                }
            )
        }
        results.append(result)
    save(
        root / "audit.json",
        dict(version=VERSION, case_count=len(results), cases=results),
    )
    return root, results


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_independent_reconciliation(
    case: Case, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    audit(
        case,
        root / case.name,
        json.loads((root / case.name / "snapshots.json").read_text()),
    )


@pytest.mark.parametrize("field", ("known", "knowledge", "key", "sequence"))
def test_reject_corrupt_trace(
    field: str, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    case = CASES[0]
    trace = json.loads((root / case.name / "snapshots.json").read_text())
    row = next(r for r in trace if r["channel"] == "reconciliation_responses")
    if field == "knowledge":
        row[field]["inventory_min"] -= 1
    elif field == "key":
        row[field][0] -= 1
    else:
        row[field] += 1
    with pytest.raises((AssertionError, KeyError)):
        audit(case, root / case.name, trace)


@pytest.mark.parametrize(
    "field",
    (
        "future-fill",
        "as-of",
        "missing-detail",
        "state",
        "duplicate-id",
        "cash",
        "absence-terminal",
    ),
)
def test_reject_corrupt_response(
    field: str, study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    root, _ = study
    case = next(
        c
        for c in CASES
        if c.name.startswith(
            "absent-order" if field == "absence-terminal" else "complete"
        )
    )
    import shutil

    directory = tmp_path / "corrupt"
    shutil.copytree(root / case.name, directory)
    document = json.loads((directory / "reconciliation.json").read_text())
    response = document["responses"][0]
    if field == "future-fill":
        response["orders"][0]["fills"][0]["exchange_fill_timestamp_ns"] = 181
    elif field == "as-of":
        response["as_of_timestamp_ns"] += 1
    elif field == "missing-detail":
        response["orders"][0]["fills"].pop()
    elif field == "state":
        response["orders"][0]["remaining_quantity"] += 1
    elif field == "duplicate-id":
        response["orders"][0]["fills"][1]["fill_id"] = response["orders"][0]["fills"][
            0
        ]["fill_id"]
    elif field == "cash":
        document["client_execution_ledger"]["trade_cash_ticks"] += 1
    else:
        response["orders"][0].update(status="rejected", remaining_quantity=4)
    save(directory / "reconciliation.json", document)
    with pytest.raises(AssertionError):
        audit(case, directory, json.loads((directory / "snapshots.json").read_text()))
