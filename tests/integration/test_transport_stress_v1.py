"""Full replay oracle from independent FIFO budgets, sends and delivered evidence.

No exchange, queue, portfolio, risk or transport helper supplies expected math.
Observers only copy actual state after the original handler. Every generated
report and every transport delivery is checked, including absent deliveries.
"""

from __future__ import annotations

import copy
import itertools
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from lobmm.backtest import run_backtest
from lobmm.channels import OrderedChannel
from lobmm.config import AppConfig, dump_config, load_config
from lobmm.data.loaders import load_events, write_csv
from lobmm.data.manifest import sha256_file
from lobmm.enums import EventType, Side, StrategyName
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.portfolio import Portfolio
from lobmm.scheduler import Scheduler
from lobmm.strategy_runtime import StrategyRuntime
from lobmm.transport import ClientKnowledge, TransportFault, TransportFaultPlan

VERSION = "transport-stress-v1"
BASE = Path("configs/client_stop_stress_v1.yaml")
pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Case:
    name: str
    faults: tuple[tuple[str, int, str], ...] = ()
    strategy: str = "fixed_spread"
    side: int = 1
    entry: int = 20
    cancel: int = 60
    report: int = 20
    horizon: int = 1100
    trade_ns: int = 160
    first_units: int = 3
    second_units: int = 1


def cases() -> tuple[Case, ...]:
    result = []
    for strategy in StrategyName:
        for side in (1, -1):
            result.append(
                Case(
                    f"reliable__{strategy.value}__{side}",
                    strategy=strategy.value,
                    side=side,
                )
            )
            for channel, count in (
                ("new_orders", 2),
                ("cancellations", 2),
                ("execution_reports", 6),
            ):
                for sequence in range(count):
                    for action in ("drop", "duplicate"):
                        result.append(
                            Case(
                                f"{channel}-{sequence}-{action}__{strategy.value}__{side}",
                                ((channel, sequence, action),),
                                strategy.value,
                                side,
                            )
                        )
    rng = random.Random(742019)
    # Explicit paired witness: the second trade is one ns after cancellation.
    # A lost cancel exposes two extra fills which reliable transport prevents.
    for strategy in StrategyName:
        for side in (1, -1):
            for dropped in (False, True):
                result.append(
                    Case(
                        f"after-cancel-{'drop' if dropped else 'reliable'}__{strategy.value}__{side}",
                        (("cancellations", 0 if side == 1 else 1, "drop"),)
                        if dropped
                        else (),
                        strategy.value,
                        side,
                        trade_ns=161,
                        second_units=2,
                    )
                )
    for index in range(32):
        faults = tuple(
            (channel, sequence, rng.choice(("drop", "duplicate")))
            for channel, count in (
                ("new_orders", 2),
                ("cancellations", 2),
                ("execution_reports", 8),
            )
            for sequence in range(count)
            if rng.random() < 0.35
        )
        result.append(
            Case(
                f"generated-{index:02d}",
                faults,
                rng.choice(tuple(StrategyName)).value,
                rng.choice((1, -1)),
                entry=rng.choice((0, 20, 200, 1200)),
                cancel=rng.choice((0, 60, 1200)),
                report=rng.choice((0, 20, 400)),
                horizon=rng.choice((100, 119, 120, 159, 160, 180, 1100, 1400)),
                trade_ns=rng.choice((159, 160, 161, 280)),
                first_units=rng.choice((3, 4, 6)),
                second_units=rng.choice((1, 2)),
            )
        )
    return tuple(result)


CASES = cases()


def configuration(case: Case) -> AppConfig:
    raw = dump_config(load_config(BASE))
    raw["strategy"]["name"] = case.strategy
    raw["latency"].update(
        order_entry_ns=case.entry,
        cancellation_ns=case.cancel,
        fill_report_ns=case.report,
    )
    raw["backtest"]["observation_end_timestamp_ns"] = case.horizon
    return AppConfig.model_validate(raw)


def plan(case: Case) -> TransportFaultPlan:
    return TransportFaultPlan(tuple(TransportFault(*fault) for fault in case.faults))


def tape(case: Case) -> list[MarketEvent]:
    rows = [
        (0, "ADD", 1, 99, 2),
        (0, "ADD", -1, 101, 2),
        (0, "ADD", 1, 98, 20),
        (0, "ADD", -1, 102, 20),
        (50, "ADD", 1, 99, 10),
        (50, "ADD", -1, 101, 10),
        (100, "TRADE", case.side, 99 if case.side == 1 else 101, case.first_units),
        (
            case.trade_ns,
            "TRADE",
            case.side,
            99 if case.side == 1 else 101,
            case.second_units,
        ),
        (1000, "ADD", 1, 90, 1),
    ]
    return [
        MarketEvent(t, i, EventType(kind), Side(side), p, q)
        for i, (t, kind, side, p, q) in enumerate(rows, 1)
    ]


def serial(value: Any) -> Any:
    if is_dataclass(value):
        return serial(asdict(value))
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    return value


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(serial(value), sort_keys=True, indent=2) + "\n")


def observe(case: Case, root: Path) -> list[dict[str, Any]]:
    objects: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    original_send, original_step = OrderedChannel.send, Scheduler.run_one

    def send(channel: Any, payload: Any, **kwargs: Any) -> Any:
        delivery = original_send(channel, payload, **kwargs)
        sends.append(
            dict(
                channel=channel.name,
                payload=serial(payload),
                timestamp_ns=kwargs["send_timestamp_ns"],
                arrival_ns=delivery.timestamp_ns if delivery else None,
            )
        )
        return delivery

    def step(scheduler: Any, handler: Any) -> Any:
        def after(event: Any, current: Any) -> None:
            sends.clear()
            handler(event, current)
            portfolio, exchange, runtime = (
                objects[x] for x in ("portfolio", "exchange", "runtime")
            )
            state = portfolio.snapshot(timestamp_ns=event.timestamp_ns)
            rows.append(
                dict(
                    key=list(event.ordering_key),
                    phase=event.phase.name,
                    source=serial(event.payload),
                    sends=copy.deepcopy(sends),
                    inventory=portfolio.inventory,
                    cash=portfolio.trade_cash_ticks,
                    fees=str(state.fees),
                    rebates=str(state.rebates),
                    net=str(state.net_pnl),
                    known=runtime.known_inventory,
                    knowledge=objects["knowledge"].artifact(),
                    orders={
                        o.client_order_id: dict(
                            side=int(o.side),
                            remaining=o.remaining_quantity,
                            filled=o.cumulative_filled_quantity,
                            status=o.status.value,
                        )
                        for o in exchange.registry.orders
                    },
                )
            )

        return original_step(scheduler, after)

    with pytest.MonkeyPatch.context() as patch:
        for cls, label in (
            (Portfolio, "portfolio"),
            (Exchange, "exchange"),
            (StrategyRuntime, "runtime"),
            (ClientKnowledge, "knowledge"),
        ):

            def wrap(original: Any, key: str) -> Any:
                def init(obj: Any, *args: Any, **kwargs: Any) -> None:
                    original(obj, *args, **kwargs)
                    objects[key] = obj

                return init

            patch.setattr(cls, "__init__", wrap(cls.__init__, label))
        patch.setattr(OrderedChannel, "send", send)
        patch.setattr(Scheduler, "run_one", step)
        run_backtest(
            configuration(case),
            tape(case),
            run_name=case.name,
            output_root=root,
            transport_faults=plan(case),
        )
    return rows


def audit(case: Case, directory: Path, trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate scalar FIFO, exact cash and exhaustive uncertainty model."""
    assert load_events(directory / "tape.csv") == tape(case)
    assert dump_config(load_config(directory / "run_config.yaml")) == dump_config(
        configuration(case)
    )
    faults = {(ch, seq): action for ch, seq, action in case.faults}
    sent: dict[str, dict[str, Any]] = {}
    orders: dict[str, dict[str, Any]] = {}
    queues: dict[int, list[list[Any]]] = {}
    depths = {1: {}, -1: {}}
    terminal: set[str] = set()
    notified: Counter[str] = Counter()
    observed_remaining: dict[str, int] = {}
    client_unresolved: set[str] = set()
    pending_cancels: set[str] = set()
    sequence: Counter[str] = Counter()
    expected_envelopes: list[dict[str, Any]] = []
    actual_envelopes: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    expected_deliveries: list[dict[str, Any]] = []
    transport_sends: list[dict[str, Any]] = []
    latency = {
        "new_orders": case.entry,
        "cancellations": case.cancel,
        "execution_reports": case.report,
        "market_data": 0,
    }
    phases = {
        "new_orders": 20,
        "cancellations": 20,
        "market_data": 30,
        "execution_reports": 31,
    }
    last_delivery: dict[str, int] = {}
    fills = []
    inventory = cash = known = 0
    fees = rebates = Decimal(0)
    report_count = 0
    stopped = False
    checkpoints = bounds_checked = 0

    def report(
        cid: str,
        kind: str,
        time: int,
        *,
        reason: str | None = None,
        fill: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal report_count
        report_count += 1
        order = orders.get(cid)
        return dict(
            report_id=f"R{report_count:012d}",
            report_type=kind,
            exchange_timestamp_ns=time,
            order_id=order["id"] if order else "",
            client_order_id=cid,
            order_status=order["status"] if order else "rejected",
            remaining_quantity=order["remaining"] if order else 0,
            fill=fill,
            reason=reason,
        )

    def knowledge() -> dict[str, Any]:
        states = []
        possibilities = {0}
        for cid, request in sent.items():
            lo = request["quantity"] - observed_remaining[cid]
            hi = lo if cid in terminal else request["quantity"]
            possibilities = {
                v + request["side"] * q
                for v in possibilities
                for q in range(lo, hi + 1)
            }
            states.append(
                dict(
                    client_order_id=cid,
                    side=request["side"],
                    sent_quantity=request["quantity"],
                    notified_fill_quantity=notified[cid],
                    executed_quantity_min=lo,
                    executed_quantity_max=hi,
                    terminal_report_received=cid in terminal,
                    reservation_quantity=0
                    if cid in terminal
                    else request["quantity"] - notified[cid],
                    missing_fill_quantity_min=max(0, lo - notified[cid]),
                )
            )
        return dict(
            known_inventory=known,
            inventory_min=min(possibilities),
            inventory_max=max(possibilities),
            orders=states,
            complete=all(
                o["terminal_report_received"] and o["missing_fill_quantity_min"] == 0
                for o in states
            ),
        )

    assert [r["key"] for r in trace] == sorted(r["key"] for r in trace)
    for row in trace:
        time = row["key"][0]
        phase, source = row["phase"], row["source"]
        generated_reports: list[dict[str, Any]] = []
        expected_cancels: list[str] = []
        duplicate = False
        if "channel_name" in source:
            ch, seq = source["channel_name"], source["channel_sequence"]
            envelope = dict(
                channel=ch,
                sequence=seq,
                time=time,
                payload=source["payload"],
                wave=row["key"][1],
                phase=row["key"][2],
            )
            assert row["key"][2:4] == [phases[ch], seq]
            actual_envelopes.append(envelope)
            assert (
                source["delivery_timestamp_ns"] == time >= source["send_timestamp_ns"]
            )
            duplicate = (ch, seq) in seen
            seen.add((ch, seq))
            if ch != "market_data":
                expected_deliveries.append(
                    dict(
                        channel=ch,
                        sequence=seq,
                        timestamp_ns=time,
                        duplicate_ignored=duplicate,
                    )
                )
            source = source["payload"]
        if phase == "MARKET":
            assert row["key"][1:4] == [0, 10, source["sequence_number"]]
            side, price, q = source["side"], source["price_ticks"], source["quantity"]
            if source["event_type"] == "ADD":
                depths[side][price] = depths[side].get(price, 0) + q
                if side in queues and price == (99 if side == 1 else 101):
                    queues[side].append([None, q])
            else:
                assert depths[side][price] >= q
                depths[side][price] -= q
                if depths[side][price] == 0:
                    del depths[side][price]
                queue = queues.get(side, [])
                while q and queue:
                    cid, available = queue[0]
                    used = min(q, available)
                    q -= used
                    queue[0][1] -= used
                    if cid is not None:
                        order = orders[cid]
                        order["remaining"] -= used
                        order["filled"] += used
                        order["status"] = (
                            "filled" if not order["remaining"] else "partially_filled"
                        )
                        cost = Decimal(used) * (
                            Decimal("0.002")
                            + Decimal(price) * Decimal("0.01") * Decimal("0.0001")
                        )
                        rebate = Decimal(used) * Decimal("0.0005")
                        fill = dict(
                            fill_id=f"F{len(fills) + 1:012d}",
                            order_id=order["id"],
                            client_order_id=cid,
                            strategy_id=sent[cid]["strategy_id"],
                            side=side,
                            quantity=used,
                            price_ticks=price,
                            liquidity_role="maker",
                            exchange_fill_timestamp_ns=time,
                            fee=str(cost),
                            rebate=str(rebate),
                        )
                        fills.append(fill)
                        inventory += side * used
                        cash -= side * used * price
                        fees += cost
                        rebates += rebate
                        generated_reports.append(
                            report(
                                cid,
                                "fill" if order["remaining"] == 0 else "partial_fill",
                                time,
                                fill=fill,
                            )
                        )
                    if queue[0][1] == 0:
                        queue.pop(0)
                if not any(cid is not None for cid, _ in queue):
                    queues.pop(side, None)
        elif phase == "EXCHANGE_COMMAND" and not duplicate and time <= 1000:
            cid = source["client_order_id"]
            if "quantity" in source:
                assert cid not in orders
                side = source["side"]
                orders[cid] = dict(
                    id=f"O{len(orders) + 1:012d}",
                    side=side,
                    remaining=source["quantity"],
                    filled=0,
                    status="live",
                )
                queues[side] = [
                    [None, depths[side].get(source["price_ticks"], 0)],
                    [cid, source["quantity"]],
                ]
                generated_reports.append(report(cid, "accepted", time))
            elif cid not in orders:
                generated_reports.append(
                    report(cid, "cancel_rejected", time, reason="unknown_order")
                )
            elif orders[cid]["status"] in {"cancelled", "filled"}:
                generated_reports.append(
                    report(cid, "cancel_rejected", time, reason="order_not_live")
                )
            else:
                orders[cid]["status"] = "cancelled"
                queues.pop(orders[cid]["side"], None)
                generated_reports.append(report(cid, "cancelled", time))
        elif phase == "EXECUTION_REPORT_DELIVERY" and not duplicate:
            cid = source["client_order_id"]
            if source["fill"]:
                units = source["fill"]["quantity"]
                notified[cid] += units
                known += source["fill"]["side"] * units
            if source["report_type"] == "cancel_rejected":
                pending_cancels.discard(cid)
            if source["reason"] != "unknown_order":
                observed_remaining[cid] = min(
                    observed_remaining[cid], source["remaining_quantity"]
                )
                if source["order_status"] in {
                    "filled",
                    "cancelled",
                    "expired",
                    "rejected",
                }:
                    terminal.add(cid)
                    client_unresolved.discard(cid)
                    pending_cancels.discard(cid)
                elif (
                    source["report_type"] == "accepted"
                    and stopped
                    and cid not in pending_cancels
                ):
                    expected_cancels.append(cid)
        elif phase == "SESSION":
            assert time == 100
            stopped = True
            expected_cancels.extend(
                cid
                for cid in sent
                if cid in client_unresolved and cid not in pending_cancels
            )

        actual_reports = [
            s["payload"] for s in row["sends"] if s["channel"] == "execution_reports"
        ]
        assert actual_reports == generated_reports, (
            case.name,
            time,
            actual_reports,
            generated_reports,
        )
        actual_cancels = [
            s["payload"]["client_order_id"]
            for s in row["sends"]
            if s["channel"] == "cancellations"
        ]
        assert actual_cancels == expected_cancels
        pending_cancels.update(expected_cancels)
        for send in row["sends"]:
            ch = send["channel"]
            seq = sequence[ch]
            sequence[ch] += 1
            payload = send["payload"]
            if ch == "new_orders":
                cid = payload["client_order_id"]
                assert time < 100 and cid not in sent and len(sent) < 2
                assert payload["quantity"] == 4 and payload["price_ticks"] == (
                    99 if payload["side"] == 1 else 101
                )
                sent[cid] = payload
                observed_remaining[cid] = payload["quantity"]
                client_unresolved.add(cid)
            arrival = max(time + latency[ch], last_delivery.get(ch, 0))
            last_delivery[ch] = arrival
            action = faults.get((ch, seq), "deliver")
            copies = 0 if action == "drop" else 2 if action == "duplicate" else 1
            assert send["arrival_ns"] == (arrival if copies else None)
            if ch != "market_data":
                transport_sends.append(
                    dict(
                        channel=ch,
                        sequence=seq,
                        action=action,
                        copies=copies,
                        send_timestamp_ns=time,
                        nominal_delivery_timestamp_ns=arrival,
                        client_order_id=payload.get("client_order_id"),
                        report_id=payload.get("report_id"),
                    )
                )
            expected_envelopes.extend(
                dict(
                    channel=ch,
                    sequence=seq,
                    time=arrival,
                    payload=payload,
                    phase=phases[ch],
                    wave=(row["key"][1] + int(phases[ch] <= row["key"][2]))
                    if arrival == time
                    else 0,
                )
                for _ in range(copies)
            )
        state = {
            cid: {k: v for k, v in order.items() if k != "id"}
            for cid, order in orders.items()
        }
        assert row["orders"] == state
        assert (row["inventory"], row["cash"], row["known"]) == (inventory, cash, known)
        assert Decimal(row["fees"]) == fees and Decimal(row["rebates"]) == rebates
        expected_knowledge = knowledge()
        assert row["knowledge"] == expected_knowledge
        assert (
            expected_knowledge["inventory_min"]
            <= inventory
            <= expected_knowledge["inventory_max"]
        )
        bounds_checked += (
            expected_knowledge["inventory_max"]
            - expected_knowledge["inventory_min"]
            + 1
        )
        mark = (
            Decimal(max(depths[1]) + min(depths[-1])) / 2
            if depths[1] and depths[-1]
            else Decimal(0)
        )
        assert (
            Decimal(row["net"])
            == (Decimal(cash) + Decimal(inventory) * mark) * Decimal("0.01")
            - fees
            + rebates
        )
        checkpoints += 1

    def counted(items: list[dict[str, Any]]) -> Counter[str]:
        return Counter(json.dumps(i, sort_keys=True) for i in items)

    assert counted(actual_envelopes) == counted(
        [e for e in expected_envelopes if e["time"] <= case.horizon]
    )
    transport = json.loads((directory / "transport_faults.json").read_text())
    assert (
        transport["sends"] == transport_sends
        and transport["deliveries"] == expected_deliveries
    )
    assert transport["faults"] == [
        dict(channel=ch, sequence=seq, action=a) for ch, seq, a in case.faults
    ]
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["transport_faults"] == dict(
        version="transport-faults-v1", faults=transport["faults"]
    )
    assert transport["unused_faults"] == [
        f for f in transport["faults"] if f["sequence"] >= sequence[f["channel"]]
    ]
    diagnostics = json.loads((directory / "diagnostics.json").read_text())
    stop = diagnostics["client_stop"]
    assert diagnostics["transport_faults"] == transport
    assert stop["known_inventory"] == known and stop["client_knowledge"] == knowledge()
    assert stop["client_knowledge_complete"] == knowledge()["complete"]
    assert set(stop["unresolved_client_order_ids"]) == client_unresolved
    assert diagnostics["end_inventory"] == inventory
    outstanding = {
        cid: o
        for cid, o in orders.items()
        if o["status"] in {"live", "partially_filled"}
    }
    dropped = {
        s["client_order_id"]
        for s in transport_sends
        if s["channel"] == "new_orders" and s["copies"] == 0
    }
    assert set(stop["dropped_entries_at_venue"]) == dropped
    pending_entries = {
        cid: r for cid, r in sent.items() if cid not in orders and cid not in dropped
    }
    exposures = [(o["side"], o["remaining"]) for o in outstanding.values()] + [
        (r["side"], r["quantity"]) for r in pending_entries.values()
    ]
    reachable = {
        inventory + sum(s * q for (s, _), q in zip(exposures, amounts, strict=True))
        for amounts in itertools.product(*(range(q + 1) for _, q in exposures))
    }
    assert (stop["reachable_inventory_min"], stop["reachable_inventory_max"]) == (
        min(reachable),
        max(reachable),
    )
    assert stop["accounting_complete"] == (not outstanding and not pending_entries)
    pending_counts = Counter(
        e["channel"] for e in expected_envelopes if e["time"] > case.horizon
    )
    assert stop["pending_channels"] == dict(pending_counts)
    saved_fills = pl.read_parquet(directory / "fills.parquet").to_dicts()
    assert len(saved_fills) == len(fills)
    for actual, expected in zip(saved_fills, fills, strict=True):
        for key, value in expected.items():
            assert actual[key] == (float(value) if key in {"fee", "rebate"} else value)
        delivered = [
            e["time"]
            for e in actual_envelopes
            if e["channel"] == "execution_reports"
            and e["payload"].get("fill")
            and e["payload"]["fill"]["fill_id"] == expected["fill_id"]
        ]
        assert actual["strategy_notification_timestamp_ns"] == (
            min(delivered) if delivered else None
        )
    pnl = pl.read_parquet(directory / "pnl.parquet").to_dicts()[-1]
    assert abs(pnl["net_pnl"] - float(trace[-1]["net"])) < 1e-12
    return dict(
        name=case.name,
        checkpoints=checkpoints,
        inventory_values_checked=bounds_checked,
        fills=len(fills),
        inventory=inventory,
        known_inventory=known,
        cash_ticks=cash,
        client_complete=knowledge()["complete"],
        venue_complete=stop["accounting_complete"],
        reachable_min=min(reachable),
        reachable_max=max(reachable),
        duplicate_deliveries=sum(d["duplicate_ignored"] for d in expected_deliveries),
        missing_fill_units=sum(
            o["missing_fill_quantity_min"] for o in knowledge()["orders"]
        ),
    )


@pytest.fixture(scope="module")
def study(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, list[dict[str, Any]]]:
    destination = request.config.getoption("--transport-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        directory = root / case.name
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(tape(case), directory / "tape.csv")
        trace = observe(case, root)
        save(directory / "snapshots.json", trace)
        result = audit(case, directory, trace)
        for name in ("metrics", "diagnostics"):
            data = json.loads((directory / f"{name}.json").read_text())
            engineering = data["engineering"] if name == "metrics" else data
            for key in ("wall_clock_seconds", "events_per_second", "peak_memory_bytes"):
                engineering.pop(key, None)
            save(directory / f"deterministic_{name}.json", data)
        result["hashes"] = {
            p.name: sha256_file(p)
            for p in sorted(directory.iterdir())
            if p.suffix == ".parquet"
            or p.name
            in {
                "tape.csv",
                "snapshots.json",
                "run_config.yaml",
                "summary.json",
                "transport_faults.json",
                "deterministic_metrics.json",
                "deterministic_diagnostics.json",
            }
        }
        results.append(result)
    save(
        root / "audit.json",
        dict(
            version=VERSION,
            case_count=len(results),
            base_config_sha256=sha256_file(BASE),
            cases=results,
        ),
    )
    return root, results


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_independent_transport_replay(
    case: Case, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    audit(
        case,
        root / case.name,
        json.loads((root / case.name / "snapshots.json").read_text()),
    )


@pytest.mark.parametrize(
    "field", ("cash", "known", "orders", "knowledge", "sends", "key")
)
def test_oracle_rejects_corruption(
    field: str, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    case = CASES[0]
    trace = json.loads((root / case.name / "snapshots.json").read_text())
    row = next(r for r in trace if r["inventory"] != 0)
    if field in {"cash", "known"}:
        row[field] += 1
    elif field == "orders":
        next(iter(row[field].values()))["remaining"] += 1
    elif field == "knowledge":
        row[field]["complete"] = True
    elif field == "sends":
        row[field] = []
    else:
        row[field][0] -= 1
    with pytest.raises(AssertionError):
        audit(case, root / case.name, trace)


@pytest.mark.parametrize(
    "corruption", ("missing-accept", "duplicate-new-identity", "causal-wave")
)
def test_oracle_checks_delivery_completeness_and_causality(
    corruption: str, study: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, _ = study
    case = CASES[0]
    trace = json.loads((root / case.name / "snapshots.json").read_text())
    index = next(
        i
        for i, r in enumerate(trace)
        if r["phase"] == "EXECUTION_REPORT_DELIVERY"
        and r["source"]["payload"]["report_type"] == "accepted"
    )
    if corruption == "missing-accept":
        # ACCEPTED changes no quantity, cash or bounds: the remaining snapshots
        # are internally consistent, but delivery completeness must fail.
        trace.pop(index)
    elif corruption == "duplicate-new-identity":
        extra = copy.deepcopy(trace[index])
        extra["source"]["channel_sequence"] = 999
        extra["source"]["payload"]["report_id"] = "R999999999999"
        extra["key"][3] = 999
        extra["key"][4] += 1
        trace.insert(index + 2, extra)
    else:
        trace[index]["key"][1] += 1
    with pytest.raises(AssertionError):
        audit(case, root / case.name, trace)
