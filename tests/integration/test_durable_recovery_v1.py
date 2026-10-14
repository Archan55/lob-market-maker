"""Durable offline replay: independent evidence witness and real SIGKILL tests.

The contract and finite matrix are fixed in DURABLE_RECOVERY_CONTRACT_V1.md.
Full-loop inputs are recorded before ResearchTransport's duplicate filtering.
Expectations below never call ClientKnowledge or the durable fold. The original
reordering fixture supplies market inputs, not expected recovery accounting.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_reordering_stress_v1 import MODES, Case, config, plans, tape

from lobmm.backtest import run_backtest
from lobmm.channels import ChannelDelivery
from lobmm.durable_recovery import DurableClient, Receipt, write_inbox
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse
from lobmm.scheduler import Scheduler
from lobmm.transport import ClientKnowledge

pytestmark = pytest.mark.integration
VERSION = "durable-recovery-v1"
HOOKS = (
    "before_temp",
    "partial_write",
    "after_write",
    "after_file_fsync",
    "after_replace",
    "after_directory_fsync",
    "after_ack",
)
CASES = tuple(
    replace(case, name=f"{case.name}__{side}", side=side)
    for case in MODES
    for side in (1, -1)
)
ROOT = Path(__file__).resolve().parents[2]


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def payload_identity(value: Any) -> str:
    """Separate deterministic witness serialization, independent of storage code."""
    return json.dumps(
        value if isinstance(value, dict) else asdict(value),
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def capture(case: Case, root: Path) -> tuple[list[NewOrderRequest], list[Receipt], Any]:
    intents: list[NewOrderRequest] = []
    receipts: list[Receipt] = []
    original_step = Scheduler.run_one
    original_sent = ClientKnowledge.sent

    def sent(client: Any, request: NewOrderRequest) -> None:
        intents.append(request)
        original_sent(client, request)

    def observed(scheduler: Any, handler: Any) -> Any:
        def before(event: Any, current: Any) -> None:
            envelope = event.payload
            if isinstance(envelope, ChannelDelivery) and isinstance(
                envelope.payload, (ExecutionReport, ReconciliationResponse)
            ):
                receipts.append(
                    Receipt(
                        f"{envelope.channel_name}:{envelope.channel_sequence}",
                        event.timestamp_ns,
                        envelope.payload,
                    )
                )
            handler(event, current)

        return original_step(scheduler, before)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ClientKnowledge, "sent", sent)
        patch.setattr(Scheduler, "run_one", observed)
        faults, reconciliation = plans(case)
        result = run_backtest(
            config(case),
            tape(case),
            run_name=case.name,
            output_root=root,
            transport_faults=faults,
            reconciliation=reconciliation,
        )
    return intents, receipts, result


def witness(intents: list[NewOrderRequest], receipts: list[Receipt]) -> dict[str, Any]:
    """Intersect sent-quantity evidence; union immutable fill identities exactly."""
    sent = {request.client_order_id: request for request in intents}
    remaining = {cid: request.quantity for cid, request in sent.items()}
    terminal: set[str] = set()
    fills: dict[str, Fill] = {}
    notifications: dict[str, int] = {}
    order_identities: dict[str, str] = {}
    seen: dict[str, dict[str, str]] = {
        key: {} for key in ("receipt", "report", "query")
    }

    def bind(kind: str, key: str, value: Any) -> None:
        identity = payload_identity(value)
        old = seen[kind].setdefault(key, identity)
        assert old == identity, f"conflicting {kind} identity: {key}"

    def fill_detail(fill: Fill, timestamp: int) -> None:
        assert fill.client_order_id in sent
        request = sent[fill.client_order_id]
        assert request.side == fill.side and request.strategy_id == fill.strategy_id
        assert (
            order_identities.setdefault(fill.order_id, fill.client_order_id)
            == fill.client_order_id
        )
        assert fills.setdefault(fill.fill_id, fill) == fill
        notifications[fill.fill_id] = min(
            notifications.get(fill.fill_id, timestamp), timestamp
        )

    for receipt in receipts:
        payload = receipt.payload
        bind(
            "receipt",
            receipt.receipt_id,
            dict(
                kind="report" if isinstance(payload, ExecutionReport) else "response",
                payload=json.loads(payload_identity(payload)),
            ),
        )
        if isinstance(payload, ExecutionReport):
            bind("report", payload.report_id, payload)
            if payload.reason == "unknown_order":
                continue
            cid = payload.client_order_id
            assert cid in sent
            assert order_identities.setdefault(payload.order_id, cid) == cid
            if payload.fill is not None:
                fill_detail(payload.fill, receipt.notification_timestamp_ns)
            remaining[cid] = min(remaining[cid], payload.remaining_quantity)
            if payload.order_status.terminal:
                terminal.add(cid)
        else:
            bind("query", payload.query_id, payload)
            for order in payload.orders:
                for fill in order.fills:
                    fill_detail(fill, receipt.notification_timestamp_ns)
                if order.status is not None:
                    assert order.remaining_quantity is not None
                    remaining[order.client_order_id] = min(
                        remaining[order.client_order_id], order.remaining_quantity
                    )
                    if order.status.terminal:
                        terminal.add(order.client_order_id)

    orders = []
    inventory_min = inventory_max = known = 0
    for cid, request in sent.items():
        detailed = sum(
            fill.quantity for fill in fills.values() if fill.client_order_id == cid
        )
        lower = max(detailed, request.quantity - remaining[cid])
        upper = lower if cid in terminal else request.quantity
        side = int(request.side)
        known += side * detailed
        inventory_min += min(side * lower, side * upper)
        inventory_max += max(side * lower, side * upper)
        orders.append(
            dict(
                client_order_id=cid,
                side=side,
                sent_quantity=request.quantity,
                notified_fill_quantity=detailed,
                executed_quantity_min=lower,
                executed_quantity_max=upper,
                terminal_report_received=cid in terminal,
                reservation_quantity=0
                if cid in terminal
                else request.quantity - detailed,
                missing_fill_quantity_min=max(0, lower - detailed),
            )
        )
    return dict(
        knowledge=dict(
            known_inventory=known,
            inventory_min=inventory_min,
            inventory_max=inventory_max,
            complete=all(
                o["terminal_report_received"] and not o["missing_fill_quantity_min"]
                for o in orders
            ),
            orders=orders,
        ),
        fills={key: json.loads(payload_identity(fill)) for key, fill in fills.items()},
        notifications=notifications,
        ledger=dict(
            known_inventory=known,
            trade_cash_ticks=sum(
                -int(f.side) * f.quantity * f.price_ticks for f in fills.values()
            ),
            fees=str(sum((f.fee for f in fills.values()), Decimal(0))),
            rebates=str(sum((f.rebate for f in fills.values()), Decimal(0))),
        ),
        identities={
            kind: {key: json.loads(value) for key, value in values.items()}
            for kind, values in seen.items()
        },
        order_identities=order_identities,
    )


def assert_witness(snapshot: dict[str, Any], expected: dict[str, Any]) -> None:
    assert snapshot["knowledge"] == expected["knowledge"]
    assert {fill["fill_id"]: fill for fill in snapshot["fills"]} == expected["fills"]
    assert snapshot["notifications"] == expected["notifications"]
    for key, value in expected["ledger"].items():
        if key in {"fees", "rebates"}:
            assert Decimal(snapshot["ledger"][key]) == Decimal(value)
        else:
            assert snapshot["ledger"][key] == value
    for kind, identities in expected["identities"].items():
        assert snapshot[f"{kind}_identities"] == identities
    assert snapshot["order_identities"] == expected["order_identities"]


def durable_paths(
    root: Path, session: str, intents: list[NewOrderRequest], receipts: list[Receipt]
) -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    inbox, checkpoint = root / "inbox.json", root / "checkpoint.json"
    digest = write_inbox(inbox, session_id=session, intents=intents, receipts=receipts)
    return checkpoint, inbox, digest


def run_full_loop(case: Case, root: Path) -> dict[str, Any]:
    intents, receipts, result = capture(case, root / "backtest")
    checkpoint, inbox, digest = durable_paths(
        root / case.name, case.name, intents, receipts
    )
    client = DurableClient.create(
        checkpoint, inbox, session_id=case.name, inbox_sha256=digest
    )
    previous = witness(intents, [])
    assert_witness(client.snapshot(), previous)
    snapshots = [client.snapshot()]
    for offset in range(1, len(receipts) + 1):
        assert client.advance()
        expected = witness(intents, receipts[:offset])
        assert_witness(client.snapshot(), expected)
        assert (
            expected["knowledge"]["inventory_min"]
            >= previous["knowledge"]["inventory_min"]
        )
        assert (
            expected["knowledge"]["inventory_max"]
            <= previous["knowledge"]["inventory_max"]
        )
        # Every prefix is reopened from bytes, without a cloned object.
        client = DurableClient.open(
            checkpoint, inbox, session_id=case.name, inbox_sha256=digest
        )
        assert client.offset == offset
        assert_witness(client.snapshot(), expected)
        snapshots.append(client.snapshot())
        previous = expected
    assert not client.advance()
    delivered = result.diagnostics["transport_faults"]["deliveries"]
    delivered_evidence = [
        row
        for row in delivered
        if row["channel"] in {"execution_reports", "reconciliation_responses"}
    ]
    assert len(receipts) == len(delivered_evidence)
    duplicate_count = len(receipts) - len({r.receipt_id for r in receipts})
    assert duplicate_count == sum(
        row["duplicate_ignored"] for row in delivered_evidence
    )
    assert (
        client.snapshot()["knowledge"]
        == result.diagnostics["client_stop"]["client_knowledge"]
    )
    venue_inventory = result.diagnostics["end_inventory"]
    save(checkpoint.parent / "prefixes.json", snapshots)
    return dict(
        name=case.name,
        receipts=len(receipts),
        prefixes=len(snapshots),
        duplicate_envelopes=duplicate_count,
        client=previous,
        synthetic_venue_inventory=venue_inventory,
        observed_venue_knowledge_gap=venue_inventory
        - previous["knowledge"]["known_inventory"],
        inbox_sha256=digest,
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
    )


def crash_inputs(kind: str) -> tuple[list[NewOrderRequest], list[Receipt]]:
    request = NewOrderRequest("crash-order", Side.BID, 101, 7, "durable-test", 0, 0)
    fill = Fill(
        "fill-a",
        "order-a",
        request.client_order_id,
        request.strategy_id,
        Side.BID,
        2,
        101,
        LiquidityRole.MAKER,
        5,
        Decimal("0.013"),
        Decimal("0.002"),
    )
    accepted = ExecutionReport(
        "accepted",
        ReportType.ACCEPTED,
        1,
        "order-a",
        request.client_order_id,
        OrderStatus.LIVE,
        7,
    )
    partial = ExecutionReport(
        "partial",
        ReportType.PARTIAL_FILL,
        5,
        "order-a",
        request.client_order_id,
        OrderStatus.PARTIALLY_FILLED,
        5,
        fill,
    )
    terminal = ReconciliationResponse(
        "terminal",
        8,
        9,
        9,
        20,
        True,
        (
            OrderEvidence(
                request.client_order_id, 8, OrderStatus.CANCELLED, 3, (), False
            ),
        ),
    )
    if kind == "fill":
        return [request], [
            Receipt("accepted-envelope", 2, accepted),
            Receipt("fill-envelope", 6, partial),
            Receipt("terminal-envelope", 10, terminal),
        ]
    return [request], [
        Receipt("fill-envelope", 6, partial),
        Receipt("terminal-envelope", 10, terminal),
        Receipt("late-accepted", 11, accepted),
    ]


def worker_arguments(
    checkpoint: Path, inbox: Path, session: str, digest: str
) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "tools/durable_recovery_worker.py"),
        str(checkpoint),
        str(inbox),
        session,
        digest,
    ]


def kill_and_reopen(kind: str, hook: str, root: Path) -> dict[str, Any]:
    intents, receipts = crash_inputs(kind)
    session = f"crash-{kind}-{hook}"
    checkpoint, inbox, digest = durable_paths(
        root / session, session, intents, receipts
    )
    client = DurableClient.create(
        checkpoint, inbox, session_id=session, inbox_sha256=digest
    )
    assert client.advance()
    old_bytes = checkpoint.read_bytes()
    old_offset = client.offset
    child = subprocess.Popen(
        [
            *worker_arguments(checkpoint, inbox, session, digest),
            "--kill-hook",
            hook,
            "--one",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
    )
    assert child.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), f"worker did not reach {hook}"
            ready = json.loads(child.stdout.readline())
        assert ready == {
            "ready": hook,
            "offset_before": old_offset,
            "returned_to_caller": hook == "after_ack",
        }
        os.kill(child.pid, signal.SIGKILL)
        _stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == -signal.SIGKILL, stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=20)
    # Independent process reads disk after death, then replays the suffix.
    orphan_count = len(list(checkpoint.parent.glob("checkpoint.json.pending-*")))
    assert orphan_count == int(
        hook in {"partial_write", "after_write", "after_file_fsync"}
    )
    committed_after_death = checkpoint.read_bytes()
    if hook not in {"after_replace", "after_directory_fsync", "after_ack"}:
        assert committed_after_death == old_bytes
    output = subprocess.run(
        worker_arguments(checkpoint, inbox, session, digest),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
    )
    recovered = json.loads(output.stdout)
    expected_offset = old_offset + (
        hook in {"after_replace", "after_directory_fsync", "after_ack"}
    )
    assert recovered["recovered_offset"] == expected_offset
    assert_witness(
        recovered["recovered_snapshot"], witness(intents, receipts[:expected_offset])
    )
    assert recovered["final_offset"] == len(receipts)
    assert_witness(recovered["final_snapshot"], witness(intents, receipts))
    # Supplementary same-implementation consistency, separate from the oracle:
    # an uninterrupted client produces exactly the same committed bytes.
    uninterrupted_path = checkpoint.parent / "uninterrupted.json"
    uninterrupted = DurableClient.create(
        uninterrupted_path, inbox, session_id=session, inbox_sha256=digest
    )
    while uninterrupted.advance():
        pass
    assert uninterrupted.snapshot() == recovered["final_snapshot"]
    assert uninterrupted_path.read_bytes() == checkpoint.read_bytes()
    result = dict(
        kind=kind,
        hook=hook,
        child_returncode=child.returncode,
        old_offset=old_offset,
        recovered_offset=recovered["recovered_offset"],
        final_offset=recovered["final_offset"],
        process_death=True,
        fresh_recovery_process=True,
        acknowledged_before_death=hook == "after_ack",
        orphan_temporary_count=orphan_count,
        old_checkpoint_sha256=hashlib.sha256(old_bytes).hexdigest(),
        checkpoint_after_death_sha256=hashlib.sha256(committed_after_death).hexdigest(),
        uninterrupted_checkpoint_sha256=hashlib.sha256(
            uninterrupted_path.read_bytes()
        ).hexdigest(),
        final_checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        recovered_snapshot=recovered["recovered_snapshot"],
        final_snapshot=recovered["final_snapshot"],
    )
    save(checkpoint.parent / "death-evidence.json", result)
    return result


def produce_study(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    cases = [run_full_loop(case, root / "full-loop") for case in CASES]
    crashes = [
        kill_and_reopen(kind, hook, root / "process-death")
        for kind in ("fill", "terminal-only-truncated")
        for hook in HOOKS
    ]
    hashes = {}
    for path in sorted(root.rglob("*.json")):
        if path.name in {
            "inbox.json",
            "checkpoint.json",
            "prefixes.json",
            "death-evidence.json",
            "uninterrupted.json",
        }:
            hashes[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    evidence = dict(
        version=VERSION,
        contract="docs/DURABLE_RECOVERY_CONTRACT_V1.md",
        full_loop_cases=cases,
        process_death_cases=crashes,
        artifact_sha256=hashes,
        distinction="SIGKILL of actual waiting child; fresh disk-only recovery interpreter; no power-failure claim",
    )
    save(root / "audit.json", evidence)
    return evidence


@pytest.fixture(scope="module")
def study(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    destination = request.config.getoption("--durable-study-output")
    root = Path(destination) if destination else tmp_path_factory.mktemp(VERSION)
    return produce_study(root)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_durable_full_loop_prefix_witness(case: Case, study: dict[str, Any]) -> None:
    row = next(row for row in study["full_loop_cases"] if row["name"] == case.name)
    assert row["prefixes"] == row["receipts"] + 1


@pytest.mark.parametrize("kind", ("fill", "terminal-only-truncated"))
@pytest.mark.parametrize("hook", HOOKS)
def test_actual_sigkill_boundary(kind: str, hook: str, study: dict[str, Any]) -> None:
    row = next(
        row
        for row in study["process_death_cases"]
        if row["kind"] == kind and row["hook"] == hook
    )
    assert row["child_returncode"] == -signal.SIGKILL


@pytest.mark.parametrize(
    "corruption",
    ("cash", "cost", "quantity", "fill-identity", "receipt-identity", "notification"),
)
def test_independent_witness_rejects_corruption(
    corruption: str, tmp_path: Path
) -> None:
    intents, receipts = crash_inputs("fill")
    checkpoint, inbox, digest = durable_paths(
        tmp_path, "oracle-control", intents, receipts
    )
    client = DurableClient.create(
        checkpoint, inbox, session_id="oracle-control", inbox_sha256=digest
    )
    while client.advance():
        pass
    observed = copy.deepcopy(client.snapshot())
    if corruption == "cash":
        observed["ledger"]["trade_cash_ticks"] += 1
    elif corruption == "cost":
        observed["ledger"]["fees"] = "0"
    elif corruption == "quantity":
        observed["knowledge"]["orders"][0]["executed_quantity_min"] += 1
    elif corruption == "fill-identity":
        observed["fills"][0]["fill_id"] = "different-fill"
    elif corruption == "receipt-identity":
        observed["receipt_identities"].pop("fill-envelope")
    else:
        observed["notifications"]["fill-a"] += 1
    with pytest.raises(AssertionError):
        assert_witness(observed, witness(intents, receipts))
