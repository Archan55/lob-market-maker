"""Real client deaths, surviving venue, and a raw-evidence scalar oracle.

The oracle below never imports the production fold or knowledge implementation.
It derives quantity intervals, unique integer cash and Decimal costs directly
from delivered immutable report dictionaries and separately audits venue fills.
Synthetic queue tapes remain uncalibrated research witnesses.
"""

from __future__ import annotations

import hashlib
import json
import selectors
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from lobmm.config import FeeConfig
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent
from lobmm.exchange import Exchange
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest

ROOT = Path(__file__).resolve().parents[2]
PERSISTENCE = (
    "before_temp",
    "partial_write",
    "after_write",
    "after_file_fsync",
    "after_replace",
    "after_directory_fsync",
)
HOOKS = (*PERSISTENCE, "before_sink", "sink_effect", "after_sink", "after_ack")
OLD = set(PERSISTENCE[:4])
EFFECT = {"sink_effect", "after_sink", "after_ack"}
TERMINAL = {"filled", "cancelled", "expired", "rejected"}
pytestmark = pytest.mark.integration


def wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    return value


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wire(value), sort_keys=True, indent=2) + "\n")


def request(side: int = 1, cid: str = "study-1") -> NewOrderRequest:
    return NewOrderRequest(
        cid, Side(side), 100 if side == 1 else 104, 6, "study", 10, 10
    )


def receipt(
    report: ExecutionReport, timestamp: int, identity: str | None = None
) -> dict[str, Any]:
    return dict(
        receipt_id=identity or f"receipt-{report.report_id}",
        notification_timestamp_ns=timestamp,
        kind="report",
        payload=wire(asdict(report)),
    )


def market(t: int, seq: int, kind: EventType, side: int, qty: int) -> MarketEvent:
    return MarketEvent(t, seq, kind, Side(side), 100 if side == 1 else 104, qty)


class Venue:
    """Lives only in the parent; transport effects survive every child death."""

    def __init__(self) -> None:
        self.exchange = Exchange(
            fee_config=FeeConfig(
                maker_fee_per_unit=Decimal("0.001"),
                maker_rebate_per_unit=Decimal("0.003"),
            )
        )
        self.exchange.handle_market_event(market(1, 1, EventType.ADD, 1, 10))
        self.exchange.handle_market_event(market(2, 2, EventType.ADD, -1, 10))
        self.trace: list[dict[str, Any]] = []
        self.commands: list[NewOrderRequest | CancelRequest] = []
        self.reports: list[ExecutionReport] = []

    def command(
        self, action: NewOrderRequest | CancelRequest, timestamp: int
    ) -> list[ExecutionReport]:
        self.commands.append(action)
        result = self.exchange.handle_command(
            action, exchange_arrival_timestamp_ns=timestamp
        )
        self.reports.extend(result.reports)
        self.trace.append(
            dict(
                command="new" if isinstance(action, NewOrderRequest) else "cancel",
                payload=wire(asdict(action)),
                arrival_ns=timestamp,
                reports=[wire(asdict(report)) for report in result.reports],
            )
        )
        return list(result.reports)

    def trade(self, timestamp: int, side: int, quantity: int) -> list[ExecutionReport]:
        event = market(timestamp, len(self.trace) + 10, EventType.TRADE, side, quantity)
        result = self.exchange.handle_market_event(event)
        self.reports.extend(result.reports)
        self.trace.append(
            dict(
                market=event.as_dict(),
                reports=[wire(asdict(r)) for r in result.reports],
            )
        )
        return list(result.reports)

    def add_behind(self, timestamp: int, side: int) -> None:
        event = market(timestamp, len(self.trace) + 10, EventType.ADD, side, 10)
        self.exchange.handle_market_event(event)
        self.trace.append(dict(market=event.as_dict(), reports=[]))

    def audit(self) -> dict[str, Any]:
        fills = [wire(asdict(fill)) for fill in self.exchange.registry.fills]
        inventory = sum(int(fill["side"]) * fill["quantity"] for fill in fills)
        cash = sum(
            -int(fill["side"]) * fill["quantity"] * fill["price_ticks"]
            for fill in fills
        )
        for order in self.exchange.registry.orders:
            actual = sum(
                fill["quantity"] for fill in fills if fill["order_id"] == order.order_id
            )
            assert actual == order.cumulative_filled_quantity
            assert order.original_quantity == actual + order.remaining_quantity
            assert 0 <= actual <= order.original_quantity
        return dict(
            inventory=inventory,
            trade_cash_ticks=cash,
            fees=str(sum((Decimal(fill["fee"]) for fill in fills), Decimal(0))),
            rebates=str(sum((Decimal(fill["rebate"]) for fill in fills), Decimal(0))),
            fills=fills,
            orders=[wire(asdict(order)) for order in self.exchange.registry.orders],
        )


Sink = Callable[[NewOrderRequest | CancelRequest], object]


class ClientProcess:
    def __init__(
        self,
        root: Path,
        mode: str = "open",
        *,
        worker: str = "outbound_recovery_worker.py",
        extra: tuple[str, ...] = (),
    ) -> None:
        root.parent.mkdir(parents=True, exist_ok=True)
        self.child = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / worker),
                str(root),
                mode,
                *extra,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.initial = self.read()

    def read(self) -> dict[str, Any]:
        assert self.child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            assert selector.select(20), "child did not reach the requested boundary"
        line = self.child.stdout.readline()
        if not line:
            assert self.child.stderr is not None
            raise AssertionError("child exited: " + self.child.stderr.read())
        value: dict[str, Any] = json.loads(line)
        return value

    def send(self, operation: dict[str, Any]) -> None:
        assert self.child.stdin is not None
        self.child.stdin.write(json.dumps(operation) + "\n")
        self.child.stdin.flush()

    def call(
        self, operation: str, *, sink: Sink | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.send(dict(operation=operation, **kwargs))
        while True:
            response = self.read()
            if "command" not in response:
                return response
            raw = response["payload"]
            action = (
                NewOrderRequest(**(raw | {"side": Side(raw["side"])}))
                if response["command"] == "new"
                else CancelRequest(**raw)
            )
            assert sink is not None, "unexpected command emission"
            sink(action)
            self.send(dict(transport_returned=True))

    def kill(self) -> None:
        self.child.kill()
        self.child.wait(timeout=10)
        assert self.child.returncode == -signal.SIGKILL
        self.finish()

    def close(self) -> None:
        if self.child.poll() is None and "open_error" not in self.initial:
            assert self.call("close") == dict(closed=True)
        self.child.wait(timeout=10)
        assert self.child.returncode == 0
        self.finish()

    def finish(self) -> None:
        assert self.child.stderr is not None
        errors = self.child.stderr.read()
        self.child.stderr.close()
        if self.child.stdout is not None:
            self.child.stdout.close()
        if self.child.stdin is not None:
            self.child.stdin.close()
        assert not errors, errors


def snapshot(client: ClientProcess) -> dict[str, Any]:
    response = client.call("snapshot")
    assert "error" not in response, response
    return cast(dict[str, Any], response["snapshot"])


def pending(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in state["cancellation_work"] if not row["terminal"]]


def freeze_crash(root: Path, client_root: Path, filename: str = "journal.json") -> None:
    """Retain exact committed/partial bytes without random mkstemp filenames."""
    committed = (client_root / filename).read_bytes()
    unfinished = sorted(
        path.read_bytes() for path in client_root.glob(filename + ".pending-*")
    )
    save(
        root / "crash-boundary.json",
        dict(
            committed=dict(
                sha256=hashlib.sha256(committed).hexdigest(),
                raw_utf8=committed.decode(),
            ),
            pending=[
                dict(sha256=hashlib.sha256(raw).hexdigest(), raw_utf8=raw.decode())
                for raw in unfinished
            ],
        ),
    )


def audit_prefix(
    state: dict[str, Any], venue: Venue, *, recovering: bool = True
) -> dict[str, Any]:
    """Independent order scalars from raw saved new/receipt dictionaries."""
    if recovering:
        assert not state["entry_enabled"]
    new_records = {
        record["payload"]["client_order_id"]: record
        for record in state["outbound"]
        if record["kind"] == "new"
    }
    intents = {cid: record["payload"] for cid, record in new_records.items()}
    details: dict[str, dict[str, Any]] = {}
    minimum = maximum = known = cash = 0
    fees = rebates = Decimal(0)
    expected_orders: dict[str, Any] = {}
    for cid, intent in intents.items():
        quantity = intent["quantity"]
        remaining = quantity
        terminal = False
        for message in state["receipts"]:
            assert message["kind"] == "report"
            report = message["payload"]
            if (
                report["client_order_id"] != cid
                or report.get("reason") == "unknown_order"
            ):
                continue
            assert (
                intent["send_timestamp_ns"]
                <= report["exchange_timestamp_ns"]
                <= message["notification_timestamp_ns"]
            )
            assert (
                new_records[cid]["dispatch_timestamp_ns"]
                <= report["exchange_timestamp_ns"]
            )
            remaining = min(remaining, report["remaining_quantity"])
            terminal = terminal or report["order_status"] in TERMINAL
            if report.get("fill") is not None:
                fill = report["fill"]
                if fill["fill_id"] in details:
                    assert details[fill["fill_id"]] == fill
                details[fill["fill_id"]] = fill
        filled = sum(
            fill["quantity"]
            for fill in details.values()
            if fill["client_order_id"] == cid
        )
        lower = quantity - remaining
        upper = lower if terminal else quantity
        side = intent["side"]
        minimum += side * (lower if side == 1 else upper)
        maximum += side * (upper if side == 1 else lower)
        expected_orders[cid] = dict(
            notified_fill_quantity=filled,
            executed_quantity_min=lower,
            executed_quantity_max=upper,
            terminal_report_received=terminal,
            reservation_quantity=0 if terminal else quantity - filled,
            missing_fill_quantity_min=max(0, lower - filled),
        )
    for fill in details.values():
        known += fill["side"] * fill["quantity"]
        cash -= fill["side"] * fill["quantity"] * fill["price_ticks"]
        fees += Decimal(fill["fee"])
        rebates += Decimal(fill["rebate"])
    assert state["ledger"]["known_inventory"] == known
    assert state["ledger"]["trade_cash_ticks"] == cash
    assert Decimal(state["ledger"]["fees"]) == fees
    assert Decimal(state["ledger"]["rebates"]) == rebates
    assert state["knowledge"]["inventory_min"] == minimum
    assert state["knowledge"]["inventory_max"] == maximum
    assert state["knowledge"]["complete"] == all(
        order["terminal_report_received"] and order["missing_fill_quantity_min"] == 0
        for order in expected_orders.values()
    )
    for actual in state["knowledge"]["orders"]:
        for key, value in expected_orders[actual["client_order_id"]].items():
            assert actual[key] == value, (key, actual, value)
    assert len(state["knowledge"]["orders"]) == len(intents)
    venue_audit = venue.audit()
    actual_inventory = sum(
        fill["side"] * fill["quantity"]
        for fill in venue_audit["fills"]
        if fill["client_order_id"] in intents
    )
    assert minimum <= actual_inventory <= maximum
    return dict(
        known_inventory=known,
        trade_cash_ticks=cash,
        fees=str(fees),
        rebates=str(rebates),
        inventory_min=minimum,
        inventory_max=maximum,
        actual_scoped_inventory=actual_inventory,
        venue_prefix_length=len(venue.trace),
        orders=expected_orders,
    )


def capture(
    client: ClientProcess,
    report: ExecutionReport,
    timestamp: int,
    *,
    identity: str | None = None,
) -> dict[str, Any]:
    response = client.call("capture", receipt=receipt(report, timestamp, identity))
    assert "error" not in response, response
    return cast(dict[str, Any], response["snapshot"])


def start_order(
    root: Path, venue: Venue, side: int = 1
) -> tuple[ClientProcess, ExecutionReport]:
    client = ClientProcess(root, "create")
    assert client.initial["opened"]["entry_enabled"]
    answers: list[ExecutionReport] = []
    response = client.call(
        "new",
        request=wire(asdict(request(side))),
        timestamp_ns=10,
        sink=lambda action: answers.extend(venue.command(action, 11)),
    )
    assert "error" not in response, response
    assert len(answers) == 1
    capture(client, answers[0], 20)
    return client, answers[0]


def reopen(root: Path) -> ClientProcess:
    client = ClientProcess(root)
    assert "opened" in client.initial, client.initial
    assert not client.initial["opened"]["entry_enabled"]
    return client


def finish_case(
    root: Path, venue: Venue, prefixes: list[dict[str, Any]], result: dict[str, Any]
) -> dict[str, Any]:
    result = result | {
        "artifact_directory": root.name,
        "audited_prefixes": len(prefixes),
    }
    save(root / "venue.json", dict(trace=venue.trace, audit=venue.audit()))
    save(root / "prefixes.json", prefixes)
    save(root / "result.json", result)
    return result


def new_death(root: Path, hook: str, side: int) -> dict[str, Any]:
    venue = Venue()
    client = ClientProcess(root / "client", "create")
    sent: list[NewOrderRequest | CancelRequest] = []

    def send(action: NewOrderRequest | CancelRequest) -> None:
        sent.append(action)
        venue.command(action, 11)

    response = client.call(
        "new",
        request=wire(asdict(request(side))),
        timestamp_ns=10,
        kill_hook=hook,
        sink=send,
    )
    assert response == dict(ready=hook)
    client.kill()
    freeze_crash(root, root / "client")
    recovered = reopen(root / "client")
    initial = recovered.initial["opened"]
    assert len(initial["outbound"]) == int(hook not in OLD)
    assert len(sent) == int(hook in EFFECT)
    prefixes = [dict(snapshot=initial, oracle=audit_prefix(initial, venue))]
    reports: list[ExecutionReport] = []
    if sent:
        venue.add_behind(35, side)
        reports.extend(venue.trade(40, side, 12))
    if hook not in OLD:
        response = recovered.call(
            "cancel",
            client_order_id="study-1",
            timestamp_ns=45,
            sink=lambda action: reports.extend(venue.command(action, 50)),
        )
        assert response["result"] is True
        for report in reports:
            state = capture(recovered, report, 60)
            prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
        if sent:
            assert not pending(state)
        else:
            assert pending(state), "unknown cancel must not close a possibly sent new"
    if sent:
        assert not venue.trade(70, side, 4)
    state = snapshot(recovered)
    assert len([r for r in state["outbound"] if r["kind"] == "new"]) == int(
        hook not in OLD
    )
    assert abs(venue.audit()["inventory"]) == (2 if sent else 0)
    # A refused fresh new cannot reach the surviving venue after reopen.
    rejected = recovered.call(
        "new", request=wire(asdict(request(side, "fresh-after-open"))), timestamp_ns=80
    )
    assert "error" in rejected
    recovered.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="new",
            hook=hook,
            side=side,
            deaths=1,
            reserved=hook not in OLD,
            venue_saw_new=bool(sent),
            final_inventory=venue.audit()["inventory"],
            new_after_open_blocked=True,
        ),
    )


def cancel_death(root: Path, hook: str, side: int) -> dict[str, Any]:
    venue = Venue()
    original, _ = start_order(root / "client", venue, side)
    original.close()
    client = reopen(root / "client")
    queued: list[NewOrderRequest | CancelRequest] = []
    response = client.call(
        "cancel",
        client_order_id="study-1",
        timestamp_ns=30,
        kill_hook=hook,
        sink=queued.append,
    )
    assert response == dict(ready=hook)
    client.kill()
    freeze_crash(root, root / "client")
    recovered = reopen(root / "client")
    initial = recovered.initial["opened"]
    assert len(initial["outbound"]) == 1 + int(hook not in OLD)
    assert len(queued) == int(hook in EFFECT)
    prefixes = [dict(snapshot=initial, oracle=audit_prefix(initial, venue))]
    venue.add_behind(35, side)
    reports = venue.trade(40, side, 12)
    response = recovered.call(
        "cancel",
        client_order_id="study-1",
        timestamp_ns=45,
        sink=lambda action: reports.extend(venue.command(action, 50)),
    )
    assert response["result"] is True
    for action in queued:
        reports.extend(venue.command(action, 55))
    for report in reports:
        state = capture(recovered, report, 60)
        prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    assert not venue.trade(70, side, 4)
    final = snapshot(recovered)
    assert not pending(final)
    assert abs(final["ledger"]["known_inventory"]) == 2
    assert final["knowledge"]["complete"]
    assert not final["entry_enabled"]
    after_terminal = recovered.call(
        "cancel", client_order_id="study-1", timestamp_ns=80
    )
    assert after_terminal["result"] is False
    assert after_terminal["snapshot"]["outbound"] == final["outbound"]
    prefixes.append(
        dict(
            snapshot=after_terminal["snapshot"],
            oracle=audit_prefix(after_terminal["snapshot"], venue),
        )
    )
    recovered.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="cancel",
            hook=hook,
            side=side,
            deaths=1,
            queued_old_cancel=bool(queued),
            final_inventory=venue.audit()["inventory"],
            cancellation_resumed=True,
        ),
    )


def capture_death(root: Path, hook: str, side: int) -> dict[str, Any]:
    venue = Venue()
    original, _ = start_order(root / "client", venue, side)
    original.close()
    venue.add_behind(35, side)
    fill_report = venue.trade(40, side, 12)[0]
    terminal = venue.command(CancelRequest(45, 45, client_order_id="study-1"), 50)[0]
    client = reopen(root / "client")
    candidate = receipt(fill_report, 60)
    response = client.call("capture", receipt=candidate, kill_hook=hook)
    assert response == dict(ready=hook)
    client.kill()
    freeze_crash(root, root / "client")
    recovered = reopen(root / "client")
    initial = recovered.initial["opened"]
    assert abs(initial["ledger"]["known_inventory"]) == (0 if hook in OLD else 2)
    prefixes = [dict(snapshot=initial, oracle=audit_prefix(initial, venue))]
    # Adapter still owns an unacknowledged receipt, even if replace committed.
    response = recovered.call("capture", receipt=candidate)
    assert "error" not in response
    state = response["snapshot"]
    assert abs(state["ledger"]["known_inventory"]) == 2
    assert len(state["receipts"]) == 2
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    # A separate receipt identity carrying the same report cannot book twice.
    state = capture(recovered, fill_report, 70, identity="redelivery-separate-id")
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    state = capture(recovered, terminal, 80)
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    assert state["knowledge"]["complete"] and not pending(state)
    recovered.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="capture",
            hook=hook,
            side=side,
            deaths=1,
            initial_known_inventory=initial["ledger"]["known_inventory"],
            final_inventory=venue.audit()["inventory"],
            duplicate_booked_once=True,
        ),
    )


def delayed_new(root: Path, side: int) -> dict[str, Any]:
    venue = Venue()
    client = ClientProcess(root / "client", "create")
    queued: list[NewOrderRequest | CancelRequest] = []
    assert client.call(
        "new",
        request=wire(asdict(request(side))),
        timestamp_ns=10,
        kill_hook="sink_effect",
        sink=queued.append,
    ) == dict(ready="sink_effect")
    client.kill()
    freeze_crash(root, root / "client")
    recovered = reopen(root / "client")
    prefixes = [
        dict(
            snapshot=recovered.initial["opened"],
            oracle=audit_prefix(recovered.initial["opened"], venue),
        )
    ]
    unknown: list[ExecutionReport] = []
    recovered.call(
        "cancel",
        client_order_id="study-1",
        timestamp_ns=25,
        sink=lambda action: unknown.extend(venue.command(action, 30)),
    )
    assert unknown[0].reason == "unknown_order"
    state = capture(recovered, unknown[0], 35)
    assert pending(state)
    assert state["knowledge"]["orders"][0]["reservation_quantity"] == 6
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    # The old process is dead; its transport queue independently admits the new.
    accepted = venue.command(queued[0], 40)[0]
    venue.add_behind(45, side)
    partial = venue.trade(50, side, 12)[0]
    terminal: list[ExecutionReport] = []
    recovered.call(
        "cancel",
        client_order_id="study-1",
        timestamp_ns=55,
        sink=lambda action: terminal.extend(venue.command(action, 60)),
    )
    # Terminal arrives first; accounting remains incomplete until fill details.
    state = capture(recovered, terminal[0], 70)
    assert not pending(state) and not state["knowledge"]["complete"]
    assert state["ledger"]["known_inventory"] == 0
    assert state["knowledge"]["orders"][0]["missing_fill_quantity_min"] == 2
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    for report, stamp in ((accepted, 75), (partial, 80), (partial, 85)):
        state = capture(recovered, report, stamp, identity=f"delayed-{stamp}")
        prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    assert state["knowledge"]["complete"]
    assert abs(state["ledger"]["known_inventory"]) == 2
    assert not venue.trade(90, side, 4)
    recovered.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="delayed-old-new",
            side=side,
            deaths=1,
            early_unknown_did_not_fence_new=True,
            terminal_without_details_incomplete=True,
            final_inventory=venue.audit()["inventory"],
        ),
    )


def missing_terminal(root: Path, side: int, *, lose_cancel: bool) -> dict[str, Any]:
    venue = Venue()
    original, _ = start_order(root / "client", venue, side)
    original.close()
    recovered = reopen(root / "client")
    prefixes = [
        dict(
            snapshot=recovered.initial["opened"],
            oracle=audit_prefix(recovered.initial["opened"], venue),
        )
    ]
    emitted: list[NewOrderRequest | CancelRequest] = []

    def send(action: NewOrderRequest | CancelRequest) -> None:
        emitted.append(action)
        if not lose_cancel:
            venue.command(action, action.send_timestamp_ns + 1)

    for attempt in range(4):
        response = recovered.call(
            "cancel",
            client_order_id="study-1",
            timestamp_ns=30 + attempt * 10,
            sink=send,
        )
        assert response["result"] is True
        state = response["snapshot"]
        prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    response = recovered.call(
        "cancel", client_order_id="study-1", timestamp_ns=80, sink=send
    )
    assert "error" in response or response.get("result") is False
    assert len(emitted) == 4
    recovered.close()
    recovered = reopen(root / "client")
    state = recovered.initial["opened"]
    assert pending(state) and not state["knowledge"]["complete"]
    assert len(state["outbound"]) == 5
    if lose_cancel:
        venue.add_behind(90, side)
        venue.trade(100, side, 16)
        assert abs(venue.audit()["inventory"]) == 6
    else:
        assert not venue.exchange.live_orders
        assert not venue.trade(100, side, 16)
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    recovered.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="cancel-lost" if lose_cancel else "terminal-lost",
            side=side,
            deaths=0,
            attempts=4,
            unresolved=True,
            final_inventory=venue.audit()["inventory"],
        ),
    )


def lock_and_bypass(root: Path) -> dict[str, Any]:
    venue = Venue()
    owner, _ = start_order(root / "first", venue)
    competitor = ClientProcess(root / "first")
    assert "open_error" in competitor.initial
    competitor.close()
    prefixes = [
        dict(
            snapshot=snapshot(owner),
            oracle=audit_prefix(snapshot(owner), venue, recovering=False),
        )
    ]
    # Another directory for the same account can create a different saved ID.
    bypass = ClientProcess(root / "second", "create")
    response = bypass.call(
        "new",
        request=wire(asdict(request(1, "study-2"))),
        timestamp_ns=12,
        sink=lambda action: venue.command(action, 13),
    )
    assert "error" not in response
    owner.kill()
    freeze_crash(root, root / "first")
    recovered = reopen(root / "first")
    assert len(recovered.initial["opened"]["outbound"]) == 1
    venue.add_behind(30, 1)
    venue.trade(40, 1, 20)
    assert venue.audit()["inventory"] == 10
    # Historical trades consume at most displayed depth. Add another external
    # ten behind both orders, then the last two own units can execute.
    venue.add_behind(41, 1)
    venue.trade(42, 1, 2)
    # Saved journal size six cannot bound two account-wide six-unit orders.
    assert venue.audit()["inventory"] == 12
    state = recovered.initial["opened"]
    prefixes.append(dict(snapshot=state, oracle=audit_prefix(state, venue)))
    assert state["knowledge"]["inventory_max"] == 6
    recovered.close()
    bypass.close()
    return finish_case(
        root,
        venue,
        prefixes,
        dict(
            kind="local-lock-account-bypass",
            deaths=1,
            same_directory_refused=True,
            same_account_other_directory_allowed=True,
            first_journal_max=6,
            account_actual_inventory=12,
        ),
    )


def old_quarantine_baseline(root: Path, side: int) -> dict[str, Any]:
    """Old API still permanently quarantines and leaves venue residual live."""
    venue = Venue()
    root.mkdir(parents=True)
    old = ClientProcess(
        root,
        "new",
        worker="active_restart_worker.py",
        extra=("--side", str(side), "--kill-hook", "sink_effect"),
    )
    message = old.initial
    assert message["command"] == "new"
    raw = message["payload"]
    intent = NewOrderRequest(**(raw | {"side": Side(raw["side"])}))
    venue.command(intent, 11)
    old.send(dict(reports=[]))
    assert old.read() == dict(ready="sink_effect")
    old.kill()
    freeze_crash(root, root, "session.json")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/active_restart_worker.py"),
            str(root),
            "recover",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    recovered = json.loads(completed.stdout)
    assert recovered["snapshot"]["quarantined"]
    assert len(recovered["blocked"]) == 4 and recovered["effects"] == []
    save(root / "recovered.json", recovered)
    venue.add_behind(35, side)
    venue.trade(40, side, 12)
    venue.trade(70, side, 4)
    assert abs(venue.audit()["inventory"]) == 6
    return finish_case(
        root,
        venue,
        [],
        dict(
            kind="unchanged-old-quarantine",
            side=side,
            deaths=1,
            final_inventory=venue.audit()["inventory"],
            quarantined=True,
        ),
    )


def test_outbound_recovery_surviving_venue(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    configured = request.config.getoption("--outbound-recovery-output")
    output = Path(configured) if configured else tmp_path / "outbound-recovery-v2"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for side in (1, -1):
        side_name = "bid" if side == 1 else "ask"
        for hook in HOOKS:
            results.append(new_death(output / f"new-{side_name}-{hook}", hook, side))
            results.append(
                cancel_death(output / f"cancel-{side_name}-{hook}", hook, side)
            )
        for hook in (*PERSISTENCE, "after_ack"):
            results.append(
                capture_death(output / f"capture-{side_name}-{hook}", hook, side)
            )
        results.append(delayed_new(output / f"delayed-new-{side_name}", side))
        for lose_cancel in (False, True):
            results.append(
                missing_terminal(
                    output / f"missing-terminal-{side_name}-{lose_cancel}",
                    side,
                    lose_cancel=lose_cancel,
                )
            )
        results.append(
            old_quarantine_baseline(output / f"old-quarantine-{side_name}", side)
        )
    results.append(lock_and_bypass(output / "local-lock-bypass"))
    # Only our declared scenarios enter this manifest. An independent reviewer
    # can preserve evidence in a sibling folder without changing this result.
    paths = sorted(
        path
        for case in results
        for path in (output / case["artifact_directory"]).rglob("*.json")
    )
    artifacts = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    save(
        output / "audit.json",
        dict(
            version="outbound-recovery-v2-study",
            synthetic_uncalibrated=True,
            case_count=len(results),
            process_deaths=sum(case["deaths"] for case in results),
            audited_prefixes=sum(case["audited_prefixes"] for case in results),
            cases=results,
            artifact_sha256=artifacts,
        ),
    )
