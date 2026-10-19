"""Primary bounded authority experiments and independent raw-event scalar oracle.

No matching engine, portfolio or production fold is used to derive expectations.
Six units at 101 distinguish these controls from the reviewer's seven at 113.
"""

from __future__ import annotations

import hashlib
import json
import select
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from lobmm.account_fence import (
    FencedRecoverySessionV3,
    SimulatedAccountAuthorityV3,
)
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2

AUTHORITY = "primary-authority"
ACCOUNT = "primary-account"
STRATEGY = "primary-strategy"
WORKER = Path(__file__).resolve().parents[2] / "tools/account_fence_worker.py"


def new(cid: str = "six", side: Side = Side.BID) -> NewOrderRequest:
    return NewOrderRequest(cid, side, 101, 6, STRATEGY, 1, 1)


def claim(authority: SimulatedAccountAuthorityV3, expected: int = 0):
    return authority.claim(
        ACCOUNT,
        f"owner-{expected}",
        f"session-{expected}",
        STRATEGY,
        expected_generation=expected,
    )


def audit(state: dict[str, Any]) -> dict[str, Any]:
    """Derive ownership, identities, quantity and cash from raw event inputs."""
    owners = []
    orders: dict[str, list[int]] = {}
    seen_commands: set[str] = set()
    inventory = cash = 0
    for seq, event in enumerate(state["events"], 1):
        assert event["seq"] == seq
        if event["kind"] == "claim":
            assert event["expected_generation"] == len(owners)
            assert event["owner_id"] not in owners
            owners.append(event["owner_id"])
        elif event["kind"] == "command":
            token = event["token"]
            assert token["generation"] == len(owners)
            assert token["owner_id"] == owners[-1]
            assert token["account_id"] == ACCOUNT
            if event["command_id"] in seen_commands:
                continue
            seen_commands.add(event["command_id"])
            request = event["request"]
            payload = request["payload"]
            cid = payload["client_order_id"]
            if request["kind"] == "new":
                assert len(owners) == 1
                assert cid not in orders
                # original, executed, residual, cancelled, sign, price
                orders[cid] = [
                    payload["quantity"],
                    0,
                    payload["quantity"],
                    0,
                    payload["side"],
                    payload["price_ticks"],
                ]
            elif cid in orders:
                row = orders[cid]
                row[3] += row[2]
                row[2] = 0
        else:
            budget = event["quantity"]
            for row in orders.values():
                if row[4] != event["side"]:
                    continue
                execution = min(row[2], budget)
                budget -= execution
                row[1] += execution
                row[2] -= execution
                inventory += row[4] * execution
                cash -= row[4] * execution * row[5]
        for original, executed, residual, cancelled, _, _ in orders.values():
            assert original == executed + residual + cancelled
    assert state["generation"] == len(owners)
    assert state["entry_enabled"] == (len(owners) == 1)
    assert (inventory, cash) == (state["inventory"], state["cash_ticks"])
    assert len(state["orders"]) == len(orders)
    for order in state["orders"]:
        row = orders[order["client_order_id"]]
        assert row[:4] == [
            order[k]
            for k in (
                "original_quantity",
                "filled_quantity",
                "remaining_quantity",
                "cancelled_quantity",
            )
        ]
    return dict(
        generation=len(owners),
        entry_enabled=state["entry_enabled"],
        inventory=inventory,
        cash_ticks=cash,
        quantities=orders,
    )


@pytest.fixture(scope="module")
def archive(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    configured = request.config.getoption("--account-fence-output")
    root = (
        Path(configured) if configured else tmp_path_factory.mktemp("fence")
    ) / "primary"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    paths = sorted(root.glob("case-*.json"))
    data = dict(
        version="account-fence-v3-primary",
        synthetic_uncalibrated=True,
        scenarios=len(paths),
        process_deaths=sum(json.loads(p.read_text())["process_deaths"] for p in paths),
        artifacts={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    )
    (root / "audit.json").write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


def save(root: Path, name: str, *, states=(), process_deaths=0, **extra):
    data = dict(
        states=states,
        scalar_audits=[audit(s) for s in states],
        process_deaths=process_deaths,
        **extra,
    )
    (root / ("case-" + name + ".json")).write_text(
        json.dumps(data, sort_keys=True, indent=2) + "\n"
    )


def receive(process):
    assert select.select([process.stdout], [], [], 15)[0], "worker timed out"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def send(process, line):
    process.stdin.write(line + "\n")
    process.stdin.flush()


@contextmanager
def worker(path: Path, request: dict[str, Any]):
    process = subprocess.Popen(
        [sys.executable, "-u", str(WORKER), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        send(process, json.dumps(request))
        assert receive(process) == {"ready": True}
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_held_old_process_new_rejected_after_takeover(tmp_path, archive, side):
    path = tmp_path / "venue.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = claim(authority)
    request = dict(
        operation="new",
        token=asdict(token),
        command_id="held-new",
        client_order_id="six",
        side=int(side),
    )
    with worker(path, request) as process:
        current = claim(authority, 1)
        send(process, "go")
        result = receive(process)
        assert "stale" in result["error"]
        with pytest.raises(RecoveryError, match="disabled"):
            authority.dispatch(current, "fresh-new", new(side=side), timestamp_ns=10)
    authority.trade(ACCOUNT, side, 6, timestamp_ns=20)
    state = authority.snapshot(ACCOUNT)
    assert state["inventory"] == 0
    # Same account + different retained directories still bypasses v2 local locks.
    direct_orders = []
    journals = []
    try:
        for suffix in ("old", "fresh"):
            journal = OutboundRecoverySessionV2.create(
                tmp_path / suffix, ACCOUNT, suffix, STRATEGY
            )
            journals.append(journal)
            journal.submit_new(
                new(cid=suffix, side=side), timestamp_ns=10, sink=direct_orders.append
            )
    finally:
        for journal in journals:
            journal.close()
    local_only_fills = sum(order.quantity for order in direct_orders)
    assert local_only_fills == 12
    save(
        archive,
        f"held-{side.name}",
        states=[state],
        held_command=request,
        stale_result=result,
        local_only_reserved_and_executable=local_only_fills,
        local_only_orders=[asdict(r) for r in direct_orders],
        fenced_fills=0,
    )


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
@pytest.mark.parametrize("cancel", (False, True))
def test_accepted_before_takeover_still_fills_and_cancel_can_reduce(
    tmp_path, archive, side, cancel
):
    authority = SimulatedAccountAuthorityV3.create(tmp_path / "venue.sqlite", AUTHORITY)
    token = claim(authority)
    root = tmp_path / "journal"
    journal = OutboundRecoverySessionV2.create(root, ACCOUNT, "session-0", STRATEGY)
    adapter = FencedRecoverySessionV3(journal, authority, token)
    delivered = []
    adapter.submit_new(new(side=side), timestamp_ns=10, deliver=delivered.append)
    accepted = delivered.pop()
    adapter.capture(accepted, notification_timestamp_ns=11)
    journal.close()
    current = claim(authority, 1)
    prefixes = [authority.snapshot(ACCOUNT)]
    fills = authority.trade(ACCOUNT, side, 2, timestamp_ns=20)
    prefixes.append(authority.snapshot(ACCOUNT))
    with OutboundRecoverySessionV2.open(
        root, ACCOUNT, "session-0", STRATEGY
    ) as reopened:
        recovered = FencedRecoverySessionV3(reopened, authority, current)
        if cancel:
            recovered.recover_cancel("six", timestamp_ns=30, deliver=delivered.append)
            # Delivery of a terminal state before old fill detail is supported.
            recovered.capture(delivered.pop(), notification_timestamp_ns=31)
            state_missing_detail = reopened.snapshot()
            assert state_missing_detail["cancellation_work"][0]["terminal"]
        else:
            state_missing_detail = reopened.snapshot()
        prefixes.append(authority.snapshot(ACCOUNT))
        known = reopened.snapshot()["knowledge"]
        assert (
            known["inventory_min"]
            <= prefixes[-1]["inventory"]
            <= known["inventory_max"]
        )
        fills += authority.trade(ACCOUNT, side, 6, timestamp_ns=40)
        for report in reversed(fills):
            recovered.capture(report, notification_timestamp_ns=50)
            known = reopened.snapshot()["knowledge"]
            actual = authority.snapshot(ACCOUNT)["inventory"]
            assert known["inventory_min"] <= actual <= known["inventory_max"]
            assert not recovered.capture(report, notification_timestamp_ns=51)
        final_client = reopened.snapshot()
        assert not final_client["entry_enabled"]
        expected = 2 if cancel else 6
        assert final_client["ledger"]["known_inventory"] == int(side) * expected
        prefixes.append(authority.snapshot(ACCOUNT))
        assert prefixes[-1]["inventory"] == int(side) * expected
    save(
        archive,
        f"accepted-{side.name}-{cancel}",
        states=prefixes,
        accepted=accepted,
        raw_journal=json.loads((root / "journal.json").read_text()),
        client_without_details=state_missing_detail,
        client=final_client,
        executed_units=expected,
        cancelled_units=6 - expected,
    )


def test_two_simultaneous_takeovers_have_one_winner(tmp_path, archive):
    path = tmp_path / "venue.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    claim(authority)
    a = dict(operation="claim", owner="a", expected_generation=1, pause="before_commit")
    b = dict(operation="claim", owner="b", expected_generation=1, announce=True)
    with worker(path, a) as first, worker(path, b) as second:
        send(first, "go")
        assert receive(first) == {"boundary": "before_commit"}
        send(second, "go")
        assert receive(second) == {"attempting": True}
        assert not select.select([second.stdout], [], [], 0.05)[0]
        send(first, "go")
        results = [receive(first), receive(second)]
        assert "result" in results[0] and "error" in results[1]
    state = authority.snapshot(ACCOUNT)
    save(
        archive,
        "takeover-race",
        states=[state],
        commands=[a, b],
        outcomes=results,
        chronology=[
            "a-uncommitted-holds-transaction",
            "b-attempts-and-waits",
            "a-commits",
            "b-CAS-refused",
        ],
    )


@pytest.mark.parametrize("boundary", ("before_commit", "after_commit"))
@pytest.mark.parametrize("operation", ("claim", "new", "cancel"))
def test_actual_death_rolls_back_or_loses_only_return(
    tmp_path, archive, boundary, operation
):
    path = tmp_path / "venue.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = claim(authority)
    if operation == "cancel":
        authority.dispatch(token, "initial-new", new(), timestamp_ns=5)
    request = dict(
        operation=operation,
        owner="next-owner",
        expected_generation=1,
        token=asdict(token),
        command_id="dying-command",
        client_order_id="six",
        timestamp_ns=10,
        pause=boundary,
    )
    before = authority.snapshot(ACCOUNT)
    with worker(path, request) as process:
        send(process, "go")
        assert receive(process) == {"boundary": boundary}
        process.kill()
        assert process.wait(timeout=10) == -9
    reopened = SimulatedAccountAuthorityV3.open(path, AUTHORITY)
    after = reopened.snapshot(ACCOUNT)
    if boundary == "before_commit":
        assert before == after
    elif operation == "claim":
        assert after["generation"] == 2 and not after["entry_enabled"]
    else:
        assert reopened.lookup(ACCOUNT, "dying-command") is not None
        assert after["orders"][0]["remaining_quantity"] == (
            0 if operation == "cancel" else 6
        )
    save(
        archive,
        f"death-{operation}-{boundary}",
        states=[before, after],
        process_deaths=1,
        input=request,
        killed_at=boundary,
        retained_reply=reopened.lookup(ACCOUNT, "dying-command"),
    )


def test_shared_current_token_does_not_identify_one_process(tmp_path, archive):
    path = tmp_path / "venue.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = claim(authority)
    common = dict(operation="new", token=asdict(token), timestamp_ns=10)
    a = common | dict(command_id="one", client_order_id="one", pause="before_commit")
    b = common | dict(command_id="two", client_order_id="two", announce=True)
    with worker(path, a) as first, worker(path, b) as second:
        send(first, "go")
        assert receive(first) == {"boundary": "before_commit"}
        send(second, "go")
        assert receive(second) == {"attempting": True}
        assert not select.select([second.stdout], [], [], 0.05)[0]
        send(first, "go")
        replies = [receive(first), receive(second)]
        assert all("result" in reply for reply in replies)
    authority.trade(ACCOUNT, Side.BID, 12, timestamp_ns=20)
    state = authority.snapshot(ACCOUNT)
    assert state["inventory"] == 12
    save(
        archive,
        "shared-token-counterexample",
        states=[state],
        token=asdict(token),
        commands=[a, b],
        replies=replies,
        inventory=12,
        cash_ticks=-1212,
        limitation="Two processes sharing the current credential are indistinguishable; no per-process exclusivity or account exposure cap.",
    )


@pytest.mark.parametrize("lost_requests", (False, True))
def test_cancel_exhaustion_with_lost_requests_or_lost_receipts(
    tmp_path, archive, lost_requests
):
    authority = SimulatedAccountAuthorityV3.create(tmp_path / "venue.sqlite", AUTHORITY)
    token = claim(authority)
    root = tmp_path / "journal"
    with OutboundRecoverySessionV2.create(
        root, ACCOUNT, "session-0", STRATEGY
    ) as journal:
        adapter = FencedRecoverySessionV3(journal, authority, token)
        adapter.submit_new(new(), timestamp_ns=10, deliver=lambda _: None)
    current = claim(authority, 1)
    reports = []
    with OutboundRecoverySessionV2.open(
        root, ACCOUNT, "session-0", STRATEGY
    ) as journal:
        adapter = FencedRecoverySessionV3(journal, authority, current)
        for attempt in range(4):
            if lost_requests:
                journal.recover_cancel(
                    "six", timestamp_ns=20 + attempt, sink=lambda _: None
                )
            else:
                adapter.recover_cancel(
                    "six", timestamp_ns=20 + attempt, deliver=reports.append
                )
        with pytest.raises(RecoveryError):
            adapter.recover_cancel("six", timestamp_ns=30, deliver=reports.append)
    with OutboundRecoverySessionV2.open(
        root, ACCOUNT, "session-0", STRATEGY
    ) as journal:
        client = journal.snapshot()
        assert client["cancellation_work"][0]["exhausted"]
        assert not client["cancellation_work"][0]["terminal"]
    authority.trade(ACCOUNT, Side.BID, 6, timestamp_ns=40)
    state = authority.snapshot(ACCOUNT)
    assert state["inventory"] == (6 if lost_requests else 0)
    save(
        archive,
        f"exhaustion-{lost_requests}",
        states=[state],
        client=client,
        retained_undelivered_reports=reports,
        raw_journal=json.loads((root / "journal.json").read_text()),
    )


def test_local_and_authority_rollback_boundaries(tmp_path, archive):
    path = tmp_path / "venue.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    old = claim(authority)
    backup = tmp_path / "before-takeover.sqlite"
    shutil.copyfile(path, backup)  # quiescent, no WAL, no open connection
    root = tmp_path / "journal"
    with OutboundRecoverySessionV2.create(
        root, ACCOUNT, "session-0", STRATEGY
    ) as journal:
        local_before = (root / "journal.json").read_bytes()
        FencedRecoverySessionV3(journal, authority, old).submit_new(
            new(), timestamp_ns=10, deliver=lambda _: None
        )
    claim(authority, 1)
    (root / "journal.json").write_bytes(local_before)
    with OutboundRecoverySessionV2.open(
        root, ACCOUNT, "session-0", STRATEGY
    ) as journal:
        assert not journal.snapshot()["entry_enabled"]
        assert journal.snapshot()["cancellation_work"] == []
    with pytest.raises(RecoveryError, match="stale"):
        authority.dispatch(old, "rolled-client", new("other"), timestamp_ns=20)
    fenced = authority.snapshot(ACCOUNT)
    authority.trade(ACCOUNT, Side.BID, 6, timestamp_ns=30)
    after_fill = authority.snapshot(ACCOUNT)
    assert (
        after_fill["inventory"] == 6
    )  # lost local reservation hides existing exposure
    shutil.copyfile(backup, path)
    rolled_authority = SimulatedAccountAuthorityV3.open(path, AUTHORITY)
    rolled_authority.dispatch(old, "old-revived", new("revived"), timestamp_ns=40)
    rolled_authority.trade(ACCOUNT, Side.BID, 6, timestamp_ns=50)
    revived = rolled_authority.snapshot(ACCOUNT)
    assert revived["generation"] == 1 and revived["entry_enabled"]
    save(
        archive,
        "rollback-counterexample",
        states=[fenced, after_fill, revived],
        local_lost_reservation=True,
        limitation="Local rollback can hide exposure; authority rollback revives old credentials and entry. No anti-rollback guarantee.",
    )


def test_retained_ids_conflicts_context_and_mixed_scalar(tmp_path, archive):
    authority = SimulatedAccountAuthorityV3.create(tmp_path / "venue.sqlite", AUTHORITY)
    token = claim(authority)
    first = authority.dispatch(token, "bid", new("bid"), timestamp_ns=10)
    assert authority.dispatch(token, "bid", new("bid"), timestamp_ns=11) == first
    with pytest.raises(RecoveryError, match="conflicting"):
        authority.dispatch(token, "bid", new("different"), timestamp_ns=12)
    authority.dispatch(token, "ask", new("ask", Side.ASK), timestamp_ns=12)
    authority.trade(ACCOUNT, Side.BID, 2, timestamp_ns=20)
    authority.trade(ACCOUNT, Side.ASK, 5, timestamp_ns=21)
    authority.dispatch(
        token,
        "cancel-bid",
        CancelRequest(22, 22, client_order_id="bid"),
        timestamp_ns=22,
    )
    with pytest.raises(RecoveryError, match="reuse"):
        authority.dispatch(token, "new-id-same-client", new("bid"), timestamp_ns=23)
    for bad in (
        replace(token, owner_id="other"),
        replace(token, strategy_id="other"),
        replace(token, session_id="other"),
        replace(token, authority_id="other"),
    ):
        with pytest.raises(RecoveryError):
            authority.dispatch(bad, "mismatch", new("extra"), timestamp_ns=24)
    current = claim(authority, 1)
    with pytest.raises(RecoveryError, match="reuse"):
        authority.claim(
            ACCOUNT, token.owner_id, "again", STRATEGY, expected_generation=2
        )
    with pytest.raises(RecoveryError, match="stale"):
        authority.dispatch(
            token,
            "cancel-bid",
            CancelRequest(22, 22, client_order_id="bid"),
            timestamp_ns=25,
        )
    state = authority.snapshot(ACCOUNT)
    assert (state["inventory"], state["cash_ticks"]) == (-3, 303)
    save(archive, "identities-mixed-cash", states=[state], current=asdict(current))


def test_second_authority_and_account_alias_are_concrete_bypass_boundaries(
    tmp_path, archive
):
    authority = SimulatedAccountAuthorityV3.create(tmp_path / "first.sqlite", AUTHORITY)
    token = claim(authority)
    authority.dispatch(token, "one", new(), timestamp_ns=10)
    authority.trade(ACCOUNT, Side.BID, 6, timestamp_ns=20)
    # Reusing the same configured authority ID on another DB does not federate
    # state. The exact same trusted credential can be valid in both universes.
    other = SimulatedAccountAuthorityV3.create(tmp_path / "second.sqlite", AUTHORITY)
    assert claim(other) == token
    other.dispatch(token, "one", new(), timestamp_ns=10)
    other.trade(ACCOUNT, Side.BID, 6, timestamp_ns=20)
    separate_universes = [authority.snapshot(ACCOUNT), other.snapshot(ACCOUNT)]
    assert sum(s["inventory"] for s in separate_universes) == 12
    alias = ACCOUNT + " "
    alias_token = authority.claim(
        alias, "owner-0", "session-0", STRATEGY, expected_generation=0
    )
    authority.dispatch(alias_token, "alias", new(), timestamp_ns=10)
    authority.trade(alias, Side.BID, 6, timestamp_ns=20)
    alias_state = authority.snapshot(alias)
    assert alias_state["inventory"] + separate_universes[0]["inventory"] == 12
    save(
        archive,
        "namespace-bypass-counterexamples",
        states=separate_universes,
        alias_state=alias_state,
        limitation="Different authority DBs and account aliases bypass this exact-name single-authority model; configured authority strings are not globally unique identities.",
    )
