"""Independent partial-order evidence and scalar accounting, through full replay.

Expected venue history is derived from 4 external units ahead of 6 own units.
Trade 6 consumes 4+2; trade 2 at the cancel boundary consumes 2 more own units.
The oracle intersects evidence intervals and unions fill identities, rather than
calling the production exchange, journal, client or portfolio for expectations.
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
from lobmm.config import AppConfig, dump_config
from lobmm.data.loaders import write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.reconciliation import ReconciliationPlan
from lobmm.scheduler import Scheduler
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import (
    ClientKnowledge,
    TransportDelay,
    TransportFault,
    TransportFaultPlan,
)

VERSION = "reordering-stress-v1"
PINNED = "3da58b38b49108cc116fa4910d77da689a1ab922"


@dataclass(frozen=True)
class Case:
    name: str
    strategy: str = "fixed_spread"
    side: int = 1
    queries: tuple[int, ...] = (60, 80)
    response: int = 20
    lag: int = 0
    limit: int | None = None
    state: bool = True
    horizon: int = 300
    loss: bool = True
    cancel_loss: bool = False
    full: bool = False
    delays: tuple[tuple[str, int, int], ...] = (("reconciliation_responses", 0, 80),)
    duplicate: bool = False


MODES = (
    Case("newer-terminal-before-old-live"),
    Case("truncated-old-after-new-fill", limit=1, loss=False),
    Case(
        "absent-old-after-complete",
        queries=(60, 180),
        lag=80,
        delays=(("reconciliation_responses", 0, 200),),
    ),
    Case(
        "late-original-after-recovery",
        loss=False,
        delays=(("execution_reports", 2, 120), ("execution_reports", 3, 120)),
    ),
    Case(
        "partial-reports-reversed",
        loss=False,
        state=False,
        limit=0,
        horizon=200,
        delays=(("execution_reports", 2, 100), ("execution_reports", 4, 200)),
    ),
    Case(
        "acceptance-after-terminal",
        loss=False,
        delays=(("execution_reports", 0, 160), ("execution_reports", 1, 160)),
    ),
    Case("duplicate-reordered-response", duplicate=True),
    Case("delayed-response-before-horizon", horizon=159),
    Case("delayed-response-at-horizon", horizon=160),
    Case(
        "live-beyond-tape",
        queries=(60, 270),
        cancel_loss=True,
        delays=(("reconciliation_responses", 0, 220),),
    ),
    Case(
        "full-fill-reversed",
        full=True,
        loss=False,
        delays=(("execution_reports", 2, 100),),
    ),
    Case(
        "equal-time-response-order",
        response=0,
        delays=(("reconciliation_responses", 0, 20),),
    ),
)
CASES = tuple(
    replace(c, name=f"{c.name}__{s.value}__{side}", strategy=s.value, side=side)
    for c in MODES
    for s in StrategyName
    for side in (1, -1)
)


def config(case: Case) -> AppConfig:
    raw = dump_config(AppConfig())
    raw["latency"].update(
        market_data_ns=0, order_entry_ns=20, cancellation_ns=20, fill_report_ns=0
    )
    raw["strategy"].update(
        name=case.strategy,
        order_size=6,
        inventory_penalty_ticks=0,
        imbalance_coefficient_ticks=0,
        volatility_multiplier=0,
        minimum_quote_lifetime_ns=10000,
        refresh_interval_ns=10000,
        stale_after_ns=10000,
    )
    raw["risk"].update(
        max_abs_inventory=12,
        max_order_size=6,
        max_total_open_quantity=12,
        max_quote_age_ns=10000,
    )
    raw["backtest"].update(
        shutdown_policy="client_stop",
        client_stop_timestamp_ns=50,
        observation_end_timestamp_ns=case.horizon,
        warmup_events=4,
        timer_interval_ns=10000,
    )
    raw["fees"].update(
        maker_fee_per_unit="0.003",
        maker_rebate_per_unit="0.0007",
        proportional_fee_rate="0.0002",
    )
    raw["queue_model"]["price_through_fills"] = False
    raw["output"]["write_plots"] = False
    return AppConfig.model_validate(raw)


def tape(case: Case) -> list[MarketEvent]:
    rows = [
        (0, "ADD", 1, 99, 4),
        (0, "ADD", -1, 101, 4),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (30, "ADD", 1, 99, 12),
        (30, "ADD", -1, 101, 12),
        (40, "TRADE", case.side, 99 if case.side == 1 else 101, 6),
        (70, "TRADE", case.side, 99 if case.side == 1 else 101, 4 if case.full else 2),
        (250, "ADD", 1, 90, 1),
    ]
    return [
        MarketEvent(t, i, EventType(k), Side(s), p, q)
        for i, (t, k, s, p, q) in enumerate(rows, 1)
    ]


def plans(case: Case) -> tuple[TransportFaultPlan, ReconciliationPlan]:
    faults = (
        [TransportFault("execution_reports", i, "drop") for i in range(6)]
        if case.loss
        else []
    )
    if case.cancel_loss:
        faults.append(
            TransportFault("cancellations", 0 if case.side == 1 else 1, "drop")
        )
    if case.duplicate:
        faults.append(TransportFault("reconciliation_responses", 0, "duplicate"))
    # Report 4 is always the target terminal response. Same-time cancel ordering
    # is bid then ask, so swap to report 5 when the target is ask.
    delays = tuple(
        TransportDelay(
            ch,
            (5 if case.side == -1 and ch == "execution_reports" and seq == 4 else seq),
            d,
        )
        for ch, seq, d in case.delays
    )
    return TransportFaultPlan(tuple(faults), delays), ReconciliationPlan(
        case.queries,
        response_latency_ns=case.response,
        snapshot_lag_ns=case.lag,
        fill_limit_per_order=case.limit,
        include_order_state=case.state,
    )


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def observe(case: Case, root: Path) -> tuple[Any, list[dict[str, Any]]]:
    objects: dict[str, Any] = {}
    trace = []
    original_step = Scheduler.run_one

    def observed(scheduler: Any, handler: Any) -> Any:
        def after(event: Any, current: Any) -> None:
            handler(event, current)
            p = event.payload
            trace.append(
                dict(
                    key=list(event.ordering_key),
                    channel=p.channel_name if isinstance(p, ChannelDelivery) else None,
                    sequence=p.channel_sequence
                    if isinstance(p, ChannelDelivery)
                    else None,
                    knowledge=copy.deepcopy(objects["knowledge"].artifact()),
                    known=objects["runtime"].known_inventory,
                    managed={
                        q.client_order_id: q.remaining_quantity
                        for q in objects["runtime"].strategy.active_quotes
                    },
                )
            )

        return original_step(scheduler, after)

    with pytest.MonkeyPatch.context() as patch:
        for cls, label in (
            (ClientKnowledge, "knowledge"),
            (StrategyRuntime, "runtime"),
        ):

            def wrap(original: Any, key: str) -> Any:
                def init(obj: Any, *args: Any, **kwargs: Any) -> None:
                    original(obj, *args, **kwargs)
                    objects[key] = obj

                return init

            patch.setattr(cls, "__init__", wrap(cls.__init__, label))
        patch.setattr(Scheduler, "run_one", observed)
        faults, query = plans(case)
        result = run_backtest(
            config(case),
            tape(case),
            run_name=case.name,
            output_root=root,
            transport_faults=faults,
            reconciliation=query,
        )
    return result, trace


def history(case: Case) -> tuple[list[str], list[dict[str, Any]]]:
    cfg = config(case)
    ids = [f"{cfg.strategy.strategy_id}-{i:012d}" for i in (1, 2)]
    target = ids[0 if case.side == 1 else 1]
    reports = [
        dict(cid=cid, time=20, remaining=6, status="live", fill=None) for cid in ids
    ]
    price = 99 if case.side == 1 else 101
    total = 0
    for i, (time, quantity) in enumerate(((40, 2), (70, 4 if case.full else 2)), 1):
        total += quantity
        detail = dict(
            fill_id=f"F{i:012d}",
            order_id=f"O{1 if case.side == 1 else 2:012d}",
            client_order_id=target,
            strategy_id=cfg.strategy.strategy_id,
            side=case.side,
            quantity=quantity,
            price_ticks=price,
            liquidity_role="maker",
            exchange_fill_timestamp_ns=time,
            fee=float(
                Decimal(quantity)
                * (Decimal("0.003") + Decimal(price) * Decimal("0.000002"))
            ),
            rebate=float(Decimal(quantity) * Decimal("0.0007")),
        )
        reports.append(
            dict(
                cid=target,
                time=time,
                remaining=6 - total,
                status="filled" if total == 6 else "partially_filled",
                fill=detail,
            )
        )
    for cid in ids:
        if case.cancel_loss and cid == target:
            continue
        reports.append(
            dict(
                cid=cid,
                time=70,
                remaining=6 - total if cid == target else 6,
                status="filled" if cid == target and total == 6 else "cancelled",
                fill=None,
            )
        )
    return ids, reports


def expected_snapshots(case: Case) -> list[dict[str, Any]]:
    ids, reports = history(case)
    snapshots = []
    for i, time in enumerate(case.queries):
        cutoff = min(max(0, time - case.lag), 250)
        orders = []
        for cid in ids:
            past = [r for r in reports if r["cid"] == cid and r["time"] <= cutoff]
            state = past[-1] if case.state and past else None
            details = [r["fill"] for r in past if r["fill"] is not None]
            selected = details[: case.limit]
            orders.append(
                dict(
                    client_order_id=cid,
                    state_timestamp_ns=state["time"] if state else None,
                    status=state["status"] if state else None,
                    remaining_quantity=state["remaining"] if state else None,
                    fills=selected,
                    execution_detail_complete_as_of=bool(past)
                    and len(details) == len(selected),
                )
            )
        snapshots.append(
            dict(
                query_id=f"Q{i:04d}",
                request_timestamp_ns=time,
                exchange_snapshot_timestamp_ns=time,
                as_of_timestamp_ns=cutoff,
                tape_end_timestamp_ns=250,
                snapshot_within_tape=time <= 250,
                orders=orders,
            )
        )
    return snapshots


def audit(case: Case, directory: Path, trace: list[dict[str, Any]]) -> dict[str, Any]:
    ids, reports = history(case)
    faults, _query = plans(case)
    snapshots = expected_snapshots(case)
    doc = json.loads((directory / "reconciliation.json").read_text())
    assert doc["responses"] == snapshots
    transport = json.loads((directory / "transport_faults.json").read_text())
    assert {k: transport[k] for k in faults.artifact()} == faults.artifact()
    delays = {(d.channel, d.sequence): d.delay_ns for d in faults.delays}
    lost = {(f.channel, f.sequence) for f in faults.faults if f.action == "drop"}
    doubled = {
        (f.channel, f.sequence) for f in faults.faults if f.action == "duplicate"
    }
    expected = []
    # Construct delivery partial order from immutable send history; equal times
    # use scheduler phase and source ordinal. Queries originate in timer wave 1.
    for ch, items in (
        ("execution_reports", reports),
        ("reconciliation_responses", snapshots),
    ):
        for seq, item in enumerate(items):
            nominal = (
                item["time"]
                if ch == "execution_reports"
                else item["exchange_snapshot_timestamp_ns"] + case.response
            )
            time = nominal + delays.get((ch, seq), 0)
            if (ch, seq) in lost or time > case.horizon:
                continue
            wave = (
                1
                if ch == "reconciliation_responses"
                and time == item["request_timestamp_ns"]
                else 0
            )
            for _ in range(2 if (ch, seq) in doubled else 1):
                expected.append((time, wave, 31, seq, ch))
    actual = [
        (r["key"][0], r["key"][1], r["key"][2], r["sequence"], r["channel"])
        for r in trace
        if r["channel"] in {"execution_reports", "reconciliation_responses"}
    ]
    assert sorted(actual) == sorted(expected)
    # Cross-channel source ordinal ties use scheduling insertion only. Selected
    # cases avoid ambiguous same-wave ties; each single-channel order is exact.
    assert actual == sorted(expected, key=lambda e: e[:4])
    for send in transport["sends"]:
        key = (send["channel"], send["sequence"])
        assert send["extra_delay_ns"] == delays.get(key, 0)
        assert send["effective_delivery_timestamp_ns"] == send[
            "nominal_delivery_timestamp_ns"
        ] + delays.get(key, 0)
    constraints: dict[str, list[tuple[int, int]]] = {cid: [(0, 6)] for cid in ids}
    details: dict[str, dict[str, Any]] = {}
    notification: dict[str, int] = {}
    originals: dict[str, list[int]] = {cid: [] for cid in ids}
    terminal: set[str] = set()
    received: set[tuple[str, int]] = set()
    receipts = []
    previous = {cid: (0, 6) for cid in ids}

    def learn(fill: dict[str, Any], time: int) -> bool:
        fid = fill["fill_id"]
        if fid in details:
            assert details[fid] == fill
            return False
        details[fid] = fill
        notification[fid] = time
        return True

    def state(cid: str, remaining: int, status: str) -> None:
        count = 6 - remaining
        upper = count if status in {"filled", "cancelled"} else 6
        constraints[cid].append((count, upper))
        if upper == count:
            terminal.add(cid)

    def bounds(cid: str) -> tuple[int, int]:
        notified = sum(
            f["quantity"] for f in details.values() if f["client_order_id"] == cid
        )
        return max(notified, *(lo for lo, hi in constraints[cid])), min(
            hi for lo, hi in constraints[cid]
        )

    for row in trace:
        ch, seq, time = row["channel"], row["sequence"], row["key"][0]
        key = (ch, seq)
        new_receipt = None
        if key not in received:
            received.add(key)
            if ch == "execution_reports":
                report = reports[seq]
                if report["fill"] is not None:
                    learn(report["fill"], time)
                    originals[report["cid"]].append(report["remaining"])
                state(report["cid"], report["remaining"], report["status"])
            elif ch == "reconciliation_responses":
                snap = snapshots[seq]
                recovered = []
                for order in snap["orders"]:
                    for fill in order["fills"]:
                        if learn(fill, time):
                            recovered.append(fill["fill_id"])
                    if order["status"] is not None:
                        state(
                            order["client_order_id"],
                            order["remaining_quantity"],
                            order["status"],
                        )
                new_receipt = dict(
                    query_id=snap["query_id"],
                    response_timestamp_ns=time,
                    as_of_timestamp_ns=snap["as_of_timestamp_ns"],
                    recovered_fill_ids=recovered,
                    client_knowledge=row["knowledge"],
                )
        known = sum(f["side"] * f["quantity"] for f in details.values())
        assert row["known"] == known == row["knowledge"]["known_inventory"]
        if row["knowledge"]["orders"]:
            assert [o["client_order_id"] for o in row["knowledge"]["orders"]] == ids
            ranges = [bounds(cid) for cid in ids]
            assert row["knowledge"]["inventory_min"] == ranges[0][0] - ranges[1][1]
            assert row["knowledge"]["inventory_max"] == ranges[0][1] - ranges[1][0]
            for order in row["knowledge"]["orders"]:
                cid = order["client_order_id"]
                lo, hi = bounds(cid)
                assert lo >= previous[cid][0] and hi <= previous[cid][1]
                previous[cid] = lo, hi
                assert (
                    order["executed_quantity_min"],
                    order["executed_quantity_max"],
                ) == (lo, hi)
                n = sum(
                    f["quantity"]
                    for f in details.values()
                    if f["client_order_id"] == cid
                )
                assert order["notified_fill_quantity"] == n
                assert order["reservation_quantity"] == (
                    0 if cid in terminal else 6 - n
                )
                assert order["missing_fill_quantity_min"] == lo - n
                assert order["terminal_report_received"] == (cid in terminal)
                if cid in row["managed"] and originals[cid]:
                    assert row["managed"][cid] == min(originals[cid]), (
                        case.name,
                        time,
                        row["managed"],
                        originals,
                    )
            assert row["knowledge"]["complete"] == all(
                cid in terminal
                and bounds(cid)[0]
                == sum(
                    f["quantity"]
                    for f in details.values()
                    if f["client_order_id"] == cid
                )
                for cid in ids
            )
        if new_receipt is not None:
            receipts.append(new_receipt)
    assert doc["receipts"] == receipts
    stop = json.loads((directory / "summary.json").read_text())["client_stop"]
    assert stop["client_knowledge"] == trace[-1]["knowledge"]
    assert set(stop["unresolved_client_order_ids"]) == set(ids) - terminal
    assert len(stop["cancel_requests"]) == 2
    ledger = doc["client_execution_ledger"]
    assert ledger["fills"] == list(details.values())
    assert ledger["known_inventory"] == known
    assert ledger["trade_cash_ticks"] == sum(
        -f["side"] * f["quantity"] * f["price_ticks"] for f in details.values()
    )
    units = sum(f["quantity"] for f in details.values())
    price = 99 if case.side == 1 else 101
    assert Decimal(ledger["fees"]) == Decimal(units) * (
        Decimal("0.003") + Decimal(price) * Decimal("0.000002")
    )
    assert Decimal(ledger["rebates"]) == Decimal(units) * Decimal("0.0007")
    fills = pl.read_parquet(directory / "fills.parquet").to_dicts()
    assert [f["quantity"] for f in fills] == [2, 4 if case.full else 2]
    for fill in fills:
        expected_fill = next(
            r["fill"]
            for r in reports
            if r["fill"] and r["fill"]["fill_id"] == fill["fill_id"]
        )
        assert {k: fill[k] for k in expected_fill} == expected_fill
        assert fill["strategy_notification_timestamp_ns"] == notification.get(
            fill["fill_id"]
        )
    pnl = pl.read_parquet(directory / "pnl.parquet").to_dicts()
    for row in pnl:
        executed = sum(
            f["quantity"]
            for f in fills
            if f["exchange_fill_timestamp_ns"] <= row["timestamp_ns"]
        )
        assert row["inventory"] == case.side * executed
        assert row["trade_cash_ticks"] == -case.side * price * executed
        net = Decimal(executed) * (
            Decimal("0.01")
            - Decimal("0.003")
            - Decimal(price) * Decimal("0.000002")
            + Decimal("0.0007")
        )
        assert abs(row["net_pnl"] - float(net)) < 1e-12
    return dict(
        name=case.name,
        checkpoints=len(trace),
        known_units=units,
        client_complete=stop["client_knowledge_complete"],
        venue_complete=stop["accounting_complete"],
        receipt_order=[r["query_id"] for r in receipts],
        pending_channels=stop["pending_channels"],
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    root = Path(
        request.config.getoption("--reordering-study-output")
        or tmp_path_factory.mktemp(VERSION)
    )
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(tape(case), directory / "tape.csv")
        save(directory / "case.json", asdict(case))
        _result, trace = observe(case, root)
        save(directory / "snapshots.json", trace)
        entry = audit(case, directory, trace)
        faults, _ = plans(case)
        baseline = run_backtest(
            config(case),
            tape(case),
            run_name="no-recovery",
            output_root=directory,
            transport_faults=faults,
        )
        # Query evidence never changes venue execution or economics in these stopped cases.
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
            .drop("strategy_notification_timestamp_ns")
            .equals(
                pl.read_parquet(baseline.run_directory / "fills.parquet").drop(
                    "strategy_notification_timestamp_ns"
                )
            )
        )
        entry["baseline_complete"] = baseline.diagnostics["client_stop"][
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
        entry["hashes"] = {
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
        results.append(entry)
    save(
        root / "audit.json",
        dict(version=VERSION, baseline=PINNED, case_count=len(results), cases=results),
    )
    return root, results


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_partial_order_accounting(
    case: Case, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    audit(
        case,
        root / case.name,
        json.loads((root / case.name / "snapshots.json").read_text()),
    )


@pytest.mark.parametrize(
    "field", ("known", "interval", "reservation", "managed", "delivery")
)
def test_oracle_rejects_corruption(
    field: str, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    case = next(c for c in CASES if c.name.startswith("partial-reports-reversed"))
    trace = json.loads((root / case.name / "snapshots.json").read_text())
    row = next(
        r for r in trace if r["channel"] == "execution_reports" and r["sequence"] == 2
    )
    if field == "known":
        row["known"] += 1
    elif field == "delivery":
        row["key"][0] += 1
    elif field == "managed":
        row["managed"][next(iter(row["managed"]))] += 1
    else:
        row["knowledge"]["orders"][0][
            "executed_quantity_min" if field == "interval" else "reservation_quantity"
        ] += 1
    with pytest.raises(AssertionError):
        audit(case, root / case.name, trace)


@pytest.mark.parametrize("strategy", tuple(StrategyName))
@pytest.mark.parametrize("side", (1, -1))
def test_stale_partial_defect(
    tmp_path: Path, strategy: StrategyName, side: int
) -> None:
    # The original cached quote regressed from remaining 2 to 4 at time 140.
    case = replace(
        next(c for c in MODES if c.name == "partial-reports-reversed"),
        strategy=strategy.value,
        side=side,
    )
    _, trace = observe(case, tmp_path)
    cid = f"{config(case).strategy.strategy_id}-{1 if side == 1 else 2:012d}"
    newer = next(
        r for r in trace if r["channel"] == "execution_reports" and r["sequence"] == 3
    )
    older = next(
        r for r in trace if r["channel"] == "execution_reports" and r["sequence"] == 2
    )
    assert newer["key"][0] == 70 and older["key"][0] == 140
    assert newer["managed"][cid] == older["managed"][cid] == 2
    assert older["known"] == side * 4
    assert (
        older["knowledge"]["orders"][0 if side == 1 else 1]["executed_quantity_min"]
        == 4
    )


def test_comparison_rejects_different_delay_plans(
    study: tuple[Path, list[dict[str, Any]]], tmp_path: Path
) -> None:
    import shutil

    from lobmm.report import ReportError, compare_runs

    root, _ = study
    source = root / CASES[0].name
    copy_dir = tmp_path / "different-delay"
    shutil.copytree(source, copy_dir)
    summary = json.loads((copy_dir / "summary.json").read_text())
    summary["transport_faults"]["delays"][0]["delay_ns"] += 1
    save(copy_dir / "summary.json", summary)
    with pytest.raises(ReportError, match="same research transport fault plan"):
        compare_runs([source, copy_dir], output_directory=tmp_path / "comparison")
