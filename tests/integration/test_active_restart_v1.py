"""Actual client death with a surviving synthetic venue and independent scalars.

The venue lives in this parent process. Children own only the active client.
Expected trade quantity/cash and reservation bounds are derived from the ten
external units ahead and six-unit own order, never from a gate verdict.
"""

from __future__ import annotations

import hashlib
import json
import selectors
import signal
import subprocess
import sys
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lobmm.active_restart import GuardedStrategySession
from lobmm.config import FeeConfig, StrategyConfig
from lobmm.durable_recovery import (
    DurableClient,
    Receipt,
    _wire,
    write_inbox,
)
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse
from lobmm.strategies import make_strategy
from lobmm.strategy_runtime import StrategyRuntime

ROOT = Path(__file__).resolve().parents[2]
HOOKS = (
    "before_temp",
    "partial_write",
    "after_write",
    "after_file_fsync",
    "after_replace",
    "after_directory_fsync",
    "before_sink",
    "sink_effect",
    "after_sink",
    "after_ack",
)
OLD = {"before_temp", "partial_write", "after_write", "after_file_fsync"}
EFFECT = {"sink_effect", "after_sink", "after_ack"}
pytestmark = pytest.mark.integration


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def strategy() -> Any:
    return make_strategy(
        StrategyConfig(
            strategy_id="restart-witness",
            order_size=6,
            base_half_spread_ticks=2,
            minimum_quote_lifetime_ns=0,
        )
    )


def event(
    t: int, seq: int, kind: EventType, side: Side, price: int, qty: int
) -> MarketEvent:
    return MarketEvent(t, seq, kind, side, price, qty)


def venue() -> Exchange:
    result = Exchange(fee_config=FeeConfig(maker_rebate_per_unit=Decimal("0.003")))
    result.handle_market_event(event(1, 1, EventType.ADD, Side.BID, 100, 10))
    result.handle_market_event(event(2, 2, EventType.ADD, Side.ASK, 104, 10))
    return result


def read_line(child: subprocess.Popen[str]) -> dict[str, Any]:
    assert child.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(20), "client child did not reach selected boundary"
    line = child.stdout.readline()
    assert line, "client unexpectedly exited"
    return json.loads(line)


def wire_action(kind: str, payload: dict[str, Any]) -> NewOrderRequest | CancelRequest:
    # Deliberately distinct decoding from production's strict journal parser.
    return (
        NewOrderRequest(**(payload | {"side": Side(payload["side"])}))
        if kind == "new"
        else CancelRequest(**payload)
    )


def recovered(root: Path, *, evidence: bool = False) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "tools/active_restart_worker.py"),
        str(root),
        "recover",
    ]
    if evidence:
        command.append("--evidence")
    completed = subprocess.run(command, capture_output=True, text=True, timeout=25)
    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["effects"] == [] and len(result["blocked"]) == 4
    assert result["snapshot"]["quarantined"] is True
    return result


def evidence(
    root: Path, intents: list[NewOrderRequest], receipts: list[Receipt]
) -> DurableClient:
    inbox, checkpoint = root / "inbox.json", root / "checkpoint.json"
    digest = write_inbox(inbox, "active-witness", intents, receipts)
    save(root / "evidence-identity.json", dict(inbox_sha256=digest))
    client = DurableClient.create(checkpoint, inbox, "active-witness", digest)
    while client.advance():
        pass
    return DurableClient.open(checkpoint, inbox, "active-witness", digest)


def kill_case(root: Path, kind: str, hook: str, side_value: int) -> dict[str, Any]:
    root.mkdir(parents=True)
    exchange = venue()
    sent: list[Any] = []
    reports: list[ExecutionReport] = []
    queued: list[CancelRequest] = []
    child = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools/active_restart_worker.py"),
            str(root),
            kind,
            "--side",
            str(side_value),
            "--kill-hook",
            hook,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while True:
            message = read_line(child)
            if "ready" in message:
                assert message["ready"] == hook
                break
            action = wire_action(message["command"], message["payload"])
            sent.append(action)
            if isinstance(action, NewOrderRequest):
                answer = exchange.handle_command(
                    action, exchange_arrival_timestamp_ns=11
                )
                reports.extend(answer.reports)
                response = answer.reports
            else:
                # The client sink entered a transport queue, not the venue.
                # Cancellation remains delayed through process death.
                queued.append(action)
                response = ()
            assert child.stdin is not None
            child.stdin.write(
                json.dumps(dict(reports=[_wire(asdict(r)) for r in response])) + "\n"
            )
            child.stdin.flush()
        child.kill()
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        assert child.stderr is not None
        errors = child.stderr.read()
        child.stderr.close()
        if child.stdout is not None:
            child.stdout.close()
        if child.stdin is not None:
            child.stdin.close()
    assert not errors, errors
    journal = json.loads((root / "session.json").read_text())["journal"]
    reserved = len(journal["commands"])
    expected_reserved = (0 if kind == "new" else 1) + int(hook not in OLD)
    assert reserved == expected_reserved
    expected_sends = (0 if kind == "new" else 1) + int(hook in EFFECT)
    assert len(sent) == expected_sends
    before_recovery = (root / "session.json").read_bytes()
    first_recovery = recovered(root)
    assert (root / "session.json").read_bytes() == before_recovery

    # The parent venue is still alive; execute while the client is dead.
    side = Side(side_value)
    price = 100 if side is Side.BID else 104
    actual_new = any(isinstance(a, NewOrderRequest) for a in sent)
    fills = []
    if actual_new:
        exchange.handle_market_event(event(35, 3, EventType.ADD, side, price, 10))
        first = exchange.handle_market_event(
            event(40, 4, EventType.TRADE, side, price, 12)
        )
        fills.extend(first.fills)
        reports.extend(first.reports)
        for cancel in queued:
            answer = exchange.handle_command(cancel, exchange_arrival_timestamp_ns=50)
            reports.extend(answer.reports)
        last = exchange.handle_market_event(
            event(60, 5, EventType.TRADE, side, price, 4)
        )
        fills.extend(last.fills)
        reports.extend(last.reports)
    # Ten external ahead: first trade fills two; delayed cancel stops four.
    expected_qty = (2 if queued else 6) if actual_new else 0
    assert sum(f.quantity for f in fills) == expected_qty
    assert sum(f.signed_quantity for f in fills) == side_value * expected_qty
    assert (
        sum(-f.signed_quantity * f.price_ticks for f in fills)
        == -side_value * price * expected_qty
    )
    assert not exchange.live_orders

    intents = [
        wire_action(r["kind"], r["payload"])
        for r in journal["commands"]
        if r["kind"] == "new"
    ]
    assessment = None
    if intents:
        assert all(isinstance(a, NewOrderRequest) for a in intents)
        # No report has survived in the separate durable evidence prefix yet.
        evidence(root, intents, [])
        assessment = recovered(root, evidence=True)["assessment"]
        assert assessment["known_ledger"]["known_inventory"] == 0
        assert assessment["inventory_min"] == (0 if side_value == 1 else -6)
        assert assessment["inventory_max"] == (6 if side_value == 1 else 0)
        assert assessment["potentially_outstanding"][0]["reservation_quantity"] == 6
        assert not assessment["resume_allowed"]
    save(
        root / "scenario.json",
        dict(
            strategy_config=strategy().config.model_dump(mode="json"),
            max_abs_inventory=12,
            selected_side=side_value,
            initial_market=[
                event(1, 1, EventType.ADD, Side.BID, 100, 10).as_dict(),
                event(2, 2, EventType.ADD, Side.ASK, 104, 10).as_dict(),
            ],
            downtime_market=[
                event(35, 3, EventType.ADD, side, price, 10).as_dict(),
                event(40, 4, EventType.TRADE, side, price, 12).as_dict(),
                event(60, 5, EventType.TRADE, side, price, 4).as_dict(),
            ]
            if actual_new
            else [],
            new_arrival_ns=11 if actual_new else None,
            cancellation_arrivals_ns=[50 for _ in queued],
            kill_hook=hook,
            command_kind=kind,
        ),
    )
    save(
        root / "venue-evidence.json",
        dict(
            commands=[_wire(asdict(a)) for a in sent],
            reports=[_wire(asdict(r)) for r in reports],
            fills=[_wire(asdict(f)) for f in fills],
            delayed_cancel_count=len(queued),
            scalar_expected_quantity=expected_qty,
            scalar_expected_inventory=side_value * expected_qty,
            scalar_expected_cash_ticks=-side_value * price * expected_qty,
        ),
    )
    result = dict(
        kind=kind,
        hook=hook,
        side=side_value,
        actual_process_death=True,
        fresh_recovery_process=True,
        child_returncode=child.returncode,
        reserved_commands=reserved,
        sink_calls=len(sent),
        venue_fill_quantity=expected_qty,
        venue_inventory=side_value * expected_qty,
        venue_trade_cash_ticks=-side_value * price * expected_qty,
        returned_to_caller=hook == "after_ack",
        recovery=first_recovery,
        assessment=assessment,
        journal_sha256=hashlib.sha256(before_recovery).hexdigest(),
    )
    save(root / "death-evidence.json", result)
    return result


def naive_witness(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    old_strategy = strategy()
    original = StrategyRuntime(strategy=old_strategy, max_abs_inventory=12)
    exchange = venue()
    market = (
        event(1, 1, EventType.ADD, Side.BID, 100, 10),
        event(2, 2, EventType.ADD, Side.ASK, 104, 10),
    )
    for e in market:
        original.deliver_market(e, notification_timestamp_ns=e.timestamp_ns)
    initial = original.decide(timestamp_ns=10)
    bid = initial[0]
    assert isinstance(bid, NewOrderRequest)
    accepted = exchange.handle_command(bid, exchange_arrival_timestamp_ns=11)
    for report in accepted.reports:
        original.deliver_report(report, notification_timestamp_ns=20)
    cancel = original.decide(timestamp_ns=30, allow_new_quotes=False)[0]
    assert (
        isinstance(cancel, CancelRequest)
        and old_strategy.active_quotes[0].pending_cancel
    )
    exchange.handle_market_event(event(35, 3, EventType.ADD, Side.BID, 100, 10))
    downtime = exchange.handle_market_event(
        event(40, 4, EventType.TRADE, Side.BID, 100, 12)
    )
    # Fresh runtime pretending to resume the same session has no prior state.
    fresh_strategy = strategy()
    fresh = StrategyRuntime(strategy=fresh_strategy, max_abs_inventory=12)
    for e in market:
        fresh.deliver_market(e, notification_timestamp_ns=100)
    repeated = fresh.decide(timestamp_ns=100)
    assert [a.client_order_id for a in repeated] == [a.client_order_id for a in initial]
    assert fresh.known_inventory == 0
    assert not any(q.pending_cancel for q in fresh_strategy.active_quotes)
    rejection = exchange.handle_command(repeated[0], exchange_arrival_timestamp_ns=101)
    assert rejection.command_rejection_reason == "duplicate_client_order_id"
    remaining_before = len(fresh_strategy.active_quotes)
    fresh.deliver_report(downtime.reports[0], notification_timestamp_ns=102)
    assert (
        fresh.known_inventory == 2
        and len(fresh_strategy.active_quotes) == remaining_before
    )
    result = dict(
        scope="unsupported naive restart; existing durable fold never claimed active restart",
        reused_client_ids=[a.client_order_id for a in repeated],
        fresh_inventory_before_late_report=0,
        venue_inventory=2,
        lost_pending_cancel=True,
        exchange_rejected_reused_id=True,
        duplicate_exchange_fills_claimed=False,
    )
    save(root / "naive-restart-witness.json", result)
    return result


def risk_case(root: Path, mode: str, side_value: int) -> dict[str, Any]:
    """Delayed/lost evidence prefixes; every assessment runs in a new process."""
    root.mkdir(parents=True)
    side = Side(side_value)
    price = 100 if side is Side.BID else 104
    intent = NewOrderRequest(
        "restart-witness-risk", side, price, 6, "restart-witness", 10, 10
    )
    cancel = CancelRequest(30, 30, client_order_id=intent.client_order_id)
    session = GuardedStrategySession.create(
        root / "session.json", "active-witness", strategy(), 12
    )
    # These callbacks stand for a channel, not venue acknowledgments.
    session.dispatch(intent, timestamp_ns=10, sink=lambda _: None)
    if mode != "live-residual":
        session.dispatch(cancel, timestamp_ns=30, sink=lambda _: None)
    exchange = venue()
    unknown = None
    if mode == "new-delayed-unknown":
        unknown = exchange.handle_command(
            cancel, exchange_arrival_timestamp_ns=31
        ).reports[0]
        assert unknown.reason == "unknown_order"
    accepted = exchange.handle_command(
        intent, exchange_arrival_timestamp_ns=35 if unknown else 11
    ).reports[0]
    exchange.handle_market_event(event(36, 3, EventType.ADD, side, price, 10))
    first = exchange.handle_market_event(event(40, 4, EventType.TRADE, side, price, 12))
    assert len(first.fills) == 1 and first.fills[0].quantity == 2
    final_report = first.reports[0]
    last = None
    if mode == "cancel-delayed":
        final_report = exchange.handle_command(
            cancel, exchange_arrival_timestamp_ns=50
        ).reports[0]
        after_cancel = exchange.handle_market_event(
            event(60, 5, EventType.TRADE, side, price, 4)
        )
        assert after_cancel.fills == () and after_cancel.reports == ()
    elif mode != "live-residual":
        last = exchange.handle_market_event(
            event(60, 5, EventType.TRADE, side, price, 4)
        )
        final_report = last.reports[0]
    actual_quantity = 2 if mode in {"cancel-delayed", "live-residual"} else 6
    all_fills = [*first.fills, *(last.fills if last else ())]
    assert sum(f.quantity for f in all_fills) == actual_quantity
    assert (
        sum(-f.signed_quantity * f.price_ticks for f in all_fills)
        == -side_value * price * actual_quantity
    )
    if mode == "live-residual":
        assert exchange.live_orders[0].remaining_quantity == 4
    else:
        assert not exchange.live_orders
    absent = ReconciliationResponse(
        "absent-query",
        32,
        32,
        32,
        100,
        True,
        (OrderEvidence(intent.client_order_id, None, None, None, (), False),),
    )
    frozen = ReconciliationResponse(
        "frozen-query",
        90,
        90,
        90,
        100,
        True,
        (
            OrderEvidence(
                intent.client_order_id,
                final_report.exchange_timestamp_ns,
                final_report.order_status,
                final_report.remaining_quantity,
                (),
                False,
            ),
        ),
    )
    receipts: list[Receipt] = []
    # Each independent expected tuple is known quantity, lower executed, upper,
    # reservation, missing detail. Only the synthetic input controls these.
    expected = [(0, 0, 6, 6, 0)]
    if unknown:
        receipts.append(Receipt("unknown", 70, unknown))
        expected.append((0, 0, 6, 6, 0))
    receipts.append(Receipt("absent", 80, absent))
    expected.append((0, 0, 6, 6, 0))
    terminal = mode != "live-residual"
    upper = actual_quantity if terminal else 6
    reservation = 0 if terminal else 6
    receipts.append(Receipt("frozen", 100, frozen))
    expected.append((0, actual_quantity, upper, reservation, actual_quantity))
    receipts.append(Receipt("late-accept", 110, accepted))
    expected.append(expected[-1])
    receipts.append(Receipt("first-fill", 120, first.reports[0]))
    expected.append(
        (2, actual_quantity, upper, 0 if terminal else 4, actual_quantity - 2)
    )
    receipts.append(Receipt("first-copy", 130, first.reports[0]))
    expected.append(expected[-1])
    if last:
        receipts.append(Receipt("last-fill", 140, last.reports[0]))
        expected.append((6, 6, 6, 0, 0))
        receipts.append(
            Receipt(
                "last-copy", 150, replace(last.reports[0], report_id="last-report-copy")
            )
        )
        expected.append(expected[-1])
    digest = write_inbox(root / "inbox.json", "active-witness", [intent], receipts)
    save(root / "evidence-identity.json", dict(inbox_sha256=digest))
    client = DurableClient.create(
        root / "checkpoint.json", root / "inbox.json", "active-witness", digest
    )
    prefixes = []
    for offset, (known, lower, upper, reserved, missing) in enumerate(expected):
        if offset:
            assert client.advance()
        assessment = recovered(root, evidence=True)["assessment"]
        assert assessment["known_ledger"]["known_inventory"] == side_value * known
        assert (
            assessment["known_ledger"]["trade_cash_ticks"]
            == -side_value * price * known
        )
        assert Decimal(assessment["known_ledger"]["fees"]) == 0
        assert (
            Decimal(assessment["known_ledger"]["rebates"]) == Decimal("0.003") * known
        )
        assert assessment["inventory_min"] == side_value * (
            lower if side_value == 1 else upper
        )
        assert assessment["inventory_max"] == side_value * (
            upper if side_value == 1 else lower
        )
        assert (
            sum(
                o["reservation_quantity"] for o in assessment["potentially_outstanding"]
            )
            == reserved
        )
        assert (
            sum(
                o["missing_fill_quantity_min"]
                for o in assessment["missing_execution_detail"]
            )
            == missing
        )
        assert len(assessment["cancellation_reservations"]) == int(
            mode != "live-residual"
        )
        assert assessment["saved_order_evidence_complete"] == (
            terminal and offset >= (3 if unknown else 2) and missing == 0
        )
        assert assessment["supplied_inbox_drained"] == (offset == len(receipts))
        assert assessment["resume_allowed"] is False
        prefixes.append(
            dict(
                offset=offset,
                expected=[known, lower, upper, reserved, missing],
                assessment=assessment,
            )
        )
    assert not client.advance()
    save(
        root / "scenario.json",
        dict(
            mode=mode,
            side=side_value,
            strategy_config=strategy().config.model_dump(mode="json"),
            new_intent=_wire(asdict(intent)),
            cancel_intent=_wire(asdict(cancel)) if mode != "live-residual" else None,
            new_arrival_ns=35 if unknown else 11,
            cancel_arrival_ns=31
            if unknown
            else (50 if mode == "cancel-delayed" else None),
            initial_market=[
                event(1, 1, EventType.ADD, Side.BID, 100, 10).as_dict(),
                event(2, 2, EventType.ADD, Side.ASK, 104, 10).as_dict(),
            ],
            downtime_market=[
                event(36, 3, EventType.ADD, side, price, 10).as_dict(),
                event(40, 4, EventType.TRADE, side, price, 12).as_dict(),
            ]
            + (
                []
                if mode == "live-residual"
                else [event(60, 5, EventType.TRADE, side, price, 4).as_dict()]
            ),
        ),
    )
    save(root / "prefixes.json", prefixes)
    save(
        root / "venue-evidence.json",
        dict(
            fills=[_wire(asdict(f)) for f in all_fills],
            true_inventory=side_value * actual_quantity,
            true_cash_ticks=-side_value * price * actual_quantity,
            residual_quantity=4 if mode == "live-residual" else 0,
            cancellation_work_restored=False,
        ),
    )
    return dict(
        mode=mode,
        side=side_value,
        prefixes=len(prefixes),
        true_inventory=side_value * actual_quantity,
        final_assessment=prefixes[-1]["assessment"],
    )


def account_case(root: Path, flat: bool) -> dict[str, Any]:
    """Two-sided hand-derived oracle, proposed by a separate read-only reviewer."""
    root.mkdir(parents=True)
    bid = NewOrderRequest(
        "restart-witness-bid", Side.BID, 100, 5, "restart-witness", 10, 10
    )
    ask_qty = 5 if flat else 4
    ask = replace(
        bid,
        client_order_id="restart-witness-ask",
        side=Side.ASK,
        price_ticks=104,
        quantity=ask_qty,
    )
    session = GuardedStrategySession.create(
        root / "session.json", "active-witness", strategy(), 12
    )
    for intent in (bid, ask):
        session.dispatch(intent, timestamp_ns=10, sink=lambda _: None)
    exchange = venue()
    for intent in (bid, ask):
        exchange.handle_command(intent, exchange_arrival_timestamp_ns=11)
    for seq, side, price in ((3, Side.BID, 100), (4, Side.ASK, 104)):
        exchange.handle_market_event(event(35, seq, EventType.ADD, side, price, 10))
    if flat:
        bid_result = exchange.handle_market_event(
            event(40, 5, EventType.TRADE, Side.BID, 100, 15)
        )
        ask_result = exchange.handle_market_event(
            event(50, 6, EventType.TRADE, Side.ASK, 104, 15)
        )
        receipts = [
            Receipt("bid-full", 100, bid_result.reports[0]),
            Receipt("ask-full", 110, ask_result.reports[0]),
            Receipt("ask-copy", 120, ask_result.reports[0]),
        ]
        # Known signed inventory/cash, min/max inventory, reservations, missing.
        expected = [
            (0, 0, -5, 5, 10, 0),
            (5, -500, 0, 5, 5, 0),
            (0, 20, 0, 0, 0, 0),
            (0, 20, 0, 0, 0, 0),
        ]
    else:
        bid_result = exchange.handle_market_event(
            event(40, 5, EventType.TRADE, Side.BID, 100, 12)
        )
        cancel = CancelRequest(45, 45, client_order_id=bid.client_order_id)
        session.dispatch(cancel, timestamp_ns=45, sink=lambda _: None)
        terminal = exchange.handle_command(
            cancel, exchange_arrival_timestamp_ns=50
        ).reports[0]
        ask_result = exchange.handle_market_event(
            event(60, 6, EventType.TRADE, Side.ASK, 104, 14)
        )
        receipts = [
            Receipt("cancel-no-detail", 100, terminal),
            Receipt("late-bid-two", 110, bid_result.reports[0]),
            Receipt("bid-two-copy", 120, bid_result.reports[0]),
            Receipt("ask-four", 130, ask_result.reports[0]),
        ]
        expected = [
            (0, 0, -4, 5, 9, 0),
            (0, 0, -2, 2, 4, 2),
            (2, -200, -2, 2, 4, 0),
            (2, -200, -2, 2, 4, 0),
            (-2, 216, -2, -2, 0, 0),
        ]
    assert not exchange.live_orders
    digest = write_inbox(root / "inbox.json", "active-witness", [bid, ask], receipts)
    save(root / "evidence-identity.json", dict(inbox_sha256=digest))
    client = DurableClient.create(
        root / "checkpoint.json", root / "inbox.json", "active-witness", digest
    )
    prefixes = []
    for offset, scalar in enumerate(expected):
        if offset:
            assert client.advance()
        result = recovered(root, evidence=True)["assessment"]
        observed = (
            result["known_ledger"]["known_inventory"],
            result["known_ledger"]["trade_cash_ticks"],
            result["inventory_min"],
            result["inventory_max"],
            sum(r["reservation_quantity"] for r in result["potentially_outstanding"]),
            sum(
                r["missing_fill_quantity_min"]
                for r in result["missing_execution_detail"]
            ),
        )
        assert observed == scalar
        assert not result["resume_allowed"]
        prefixes.append(dict(offset=offset, expected=list(scalar), assessment=result))
    assert result["saved_order_evidence_complete"] and result["supplied_inbox_drained"]
    assert result["known_ledger"]["known_inventory"] == (0 if flat else -2)
    assert result["known_ledger"]["trade_cash_ticks"] == (20 if flat else 216)
    save(
        root / "scenario.json",
        dict(
            mode="flat" if flat else "two-sided-missing-detail",
            strategy_config=strategy().config.model_dump(mode="json"),
            intents=[_wire(asdict(bid)), _wire(asdict(ask))],
            new_arrival_ns=11,
            cancel_arrival_ns=None if flat else 50,
            market=[
                event(1, 1, EventType.ADD, Side.BID, 100, 10).as_dict(),
                event(2, 2, EventType.ADD, Side.ASK, 104, 10).as_dict(),
                event(35, 3, EventType.ADD, Side.BID, 100, 10).as_dict(),
                event(35, 4, EventType.ADD, Side.ASK, 104, 10).as_dict(),
                event(
                    40, 5, EventType.TRADE, Side.BID, 100, 15 if flat else 12
                ).as_dict(),
                event(
                    50 if flat else 60,
                    6,
                    EventType.TRADE,
                    Side.ASK,
                    104,
                    15 if flat else 14,
                ).as_dict(),
            ],
        ),
    )
    save(root / "prefixes.json", prefixes)
    save(
        root / "venue-evidence.json",
        dict(
            fills=[_wire(asdict(f)) for f in (*bid_result.fills, *ask_result.fills)],
            scalar_expectations=[list(e) for e in expected],
        ),
    )
    return dict(
        mode="complete-flat" if flat else "two-sided-missing-detail",
        prefixes=len(prefixes),
        final_assessment=result,
    )


def produce(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    naive = naive_witness(root / "naive")
    deaths = [
        kill_case(root / f"{kind}-{side}-{hook}", kind, hook, side)
        for kind in ("new", "cancel")
        for side in (1, -1)
        for hook in HOOKS
    ]
    risks = [
        risk_case(root / f"risk-{mode}-{side}", mode, side)
        for mode in (
            "cancel-lost",
            "cancel-delayed",
            "new-delayed-unknown",
            "live-residual",
        )
        for side in (1, -1)
    ]
    accounts = [
        account_case(root / ("flat" if flat else "two-sided"), flat)
        for flat in (False, True)
    ]
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*.json"))
    }
    audit = dict(
        version="active-restart-v1",
        baseline="ad108ebaad2edca9b2997dab95800080d5fb6802",
        contract="docs/ACTIVE_RESTART_CONTRACT_V1.md",
        naive_witness=naive,
        process_death_cases=deaths,
        risk_cases=risks,
        account_cases=accounts,
        artifact_sha256=hashes,
        limitation="Synthetic surviving parent venue; real client SIGKILL, no power-loss or active resumption claim",
    )
    save(root / "audit.json", audit)
    return audit


@pytest.fixture(scope="module")
def study(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    output = request.config.getoption("--active-restart-output")
    return produce(
        Path(output) if output else tmp_path_factory.mktemp("active-restart-v1")
    )


def test_actual_process_restart_matrix(study: dict[str, Any]) -> None:
    cases = study["process_death_cases"]
    assert len(cases) == 40
    assert all(row["child_returncode"] == -signal.SIGKILL for row in cases)
    assert any(row["venue_fill_quantity"] == 6 for row in cases)
    assert any(row["venue_fill_quantity"] == 2 for row in cases)
    assert any(
        row["reserved_commands"] == 1 and row["sink_calls"] == 0 for row in cases
    )


def test_naive_restart_hazard(study: dict[str, Any]) -> None:
    assert study["naive_witness"]["lost_pending_cancel"]


def test_risk_prefixes_preserve_uncertainty(study: dict[str, Any]) -> None:
    assert len(study["risk_cases"]) == 8
    unresolved = [row for row in study["risk_cases"] if row["mode"] == "live-residual"]
    assert all(
        row["final_assessment"]["potentially_outstanding"][0]["reservation_quantity"]
        == 4
        for row in unresolved
    )
    assert all(
        not row["final_assessment"]["resume_allowed"] for row in study["risk_cases"]
    )


def test_complete_account_evidence_never_rearms(study: dict[str, Any]) -> None:
    flat = next(row for row in study["account_cases"] if row["mode"] == "complete-flat")
    assert flat["final_assessment"]["known_ledger"]["known_inventory"] == 0
    assert flat["final_assessment"]["known_ledger"]["trade_cash_ticks"] == 20
    assert flat["final_assessment"]["resume_allowed"] is False
