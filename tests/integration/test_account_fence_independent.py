"""Independent process, retained-identity and scalar exposure probes for v3.

The production study's scenario/oracle helpers are never imported. Expectations
use seven units at 113, signed integer quantity/cash, and raw unique fill IDs.
"""

from __future__ import annotations

import hashlib
import json
import select
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from lobmm.account_fence import (
    FencedRecoverySessionV3,
    FenceToken,
    SimulatedAccountAuthorityV3,
    to_receipt,
)
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2

ACCOUNT = "independent-account"
AUTHORITY = "independent-authority"
STRATEGY = "independent-review"
WORKER = (
    Path(__file__).resolve().parents[2] / "tools/account_fence_independent_worker.py"
)


def _new(side: Side = Side.BID, cid: str = "independent-seven") -> NewOrderRequest:
    return NewOrderRequest(cid, side, 113, 7, STRATEGY, 5, 6)


def _claim(
    authority: SimulatedAccountAuthorityV3,
    generation: int = 0,
    *,
    owner: str = "owner-a",
    session: str = "session-a",
) -> FenceToken:
    return authority.claim(
        ACCOUNT, owner, session, STRATEGY, expected_generation=generation
    )


def _receive(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], "worker did not reply"
    line = process.stdout.readline()
    assert line, "worker exited without a JSON reply"
    result: dict[str, Any] = json.loads(line)
    return result


def _send(process: subprocess.Popen[str], operation: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(operation) + "\n")
    process.stdin.flush()


def _rpc(process: subprocess.Popen[str], operation: dict[str, Any]) -> dict[str, Any]:
    _send(process, operation)
    return _receive(process)


@contextmanager
def _worker(path: Path) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-u", str(WORKER), str(path), AUTHORITY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _receive(process) == {"ready": True}
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


def _dispatch(
    token: dict[str, Any],
    request: NewOrderRequest | CancelRequest,
    command_id: str,
    timestamp: int,
    *,
    pause: str | None = None,
) -> dict[str, Any]:
    return {
        "operation": "dispatch",
        "token": token,
        "kind": "new" if isinstance(request, NewOrderRequest) else "cancel",
        "request": asdict(request),
        "command_id": command_id,
        "timestamp_ns": timestamp,
        "pause": pause,
    }


def _publish(
    config: pytest.Config, temporary: Path, name: str, value: dict[str, Any]
) -> None:
    configured = config.getoption("--account-fence-output", default=None)
    destination = (
        Path(configured) / "independent" if configured else temporary / "independent"
    )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (name + ".json")).write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n"
    )
    paths = sorted(p for p in destination.glob("*.json") if p.name != "audit.json")
    audit = {
        "version": "independent-account-fence-v3",
        "synthetic_uncalibrated": True,
        "case_count": len(paths),
        "process_deaths": sum(
            json.loads(p.read_text())["process_deaths"] for p in paths
        ),
        "artifact_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
        "source_sha256": {
            "tests": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "worker": hashlib.sha256(WORKER.read_bytes()).hexdigest(),
        },
    }
    (destination / "audit.json").write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n"
    )


def _runtime_race(
    config: pytest.Config, temporary: Path, value: dict[str, Any]
) -> None:
    configured = config.getoption("--account-fence-output", default=None)
    destination = (
        Path(configured) / "independent" if configured else temporary / "independent"
    )
    destination.mkdir(parents=True, exist_ok=True)
    # Genuine races can select either legal order. Preserve the observed
    # chronology separately from the deterministic .json outcome manifest.
    with (destination / "runtime-races.jsonl").open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def test_independent_two_live_claimants_and_stale_process_commands(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    path = tmp_path / "authority.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    operations = [
        {
            "operation": "claim",
            "owner_id": "owner-" + letter,
            "session_id": "session-" + letter,
            "expected_generation": 0,
        }
        for letter in ("a", "b")
    ]
    with _worker(path) as first, _worker(path) as second:
        processes = (first, second)
        # Both interpreters already opened the same authority before either
        # receives its competing expected-generation-zero request.
        for process, operation in zip(processes, operations, strict=True):
            _send(process, operation)
        replies = [_receive(process) for process in processes]
        assert sorted("result" in reply for reply in replies) == [False, True]
        winner = next(index for index, reply in enumerate(replies) if "result" in reply)
        loser = 1 - winner
        old_token = replies[winner]["result"]
        assert old_token["generation"] == 1
        takeover = _rpc(
            processes[loser], operations[loser] | {"expected_generation": 1}
        )
        new_token = takeover["result"]
        assert new_token["generation"] == 2
        stale_new = _rpc(
            processes[winner], _dispatch(old_token, _new(), "stale-new", 10)
        )
        stale_cancel = _rpc(
            processes[winner],
            _dispatch(
                old_token,
                CancelRequest(11, 11, client_order_id="independent-seven"),
                "stale-cancel",
                11,
            ),
        )
        fresh_new = _rpc(
            processes[loser], _dispatch(new_token, _new(), "disabled-new", 12)
        )
        assert all("error" in reply for reply in (stale_new, stale_cancel, fresh_new))
    state = authority.snapshot(ACCOUNT)
    assert state["generation"] == 2
    assert state["entry_enabled"] is False
    assert state["orders"] == [] and state["commands"] == []
    _runtime_race(
        pytestconfig,
        tmp_path,
        {
            "scenario": "two-live-claimants",
            "initial_claim_inputs": operations,
            "initial_claim_outputs": replies,
            "winner_input_index": winner,
            "takeover_output": takeover,
            "stale_new_output": stale_new,
            "stale_cancel_output": stale_cancel,
            "fresh_new_output": fresh_new,
            "ordered_authority": state,
        },
    )
    # Scheduling can choose either winner, so publish only invariant quantities.
    _publish(
        pytestconfig,
        tmp_path,
        "two-live-claimants",
        {
            "process_deaths": 0,
            "concurrent_interpreters": 2,
            "initial_claim_successes": sum("result" in r for r in replies),
            "generation": state["generation"],
            "entry_enabled": state["entry_enabled"],
            "stale_command_rejections": 2,
            "new_owner_entry_rejections": 1,
            "accepted_orders": len(state["orders"]),
        },
    )


def test_independent_shared_current_token_cannot_identify_one_process(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    path = tmp_path / "authority.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = asdict(_claim(authority))
    inputs: list[dict[str, Any]] = []
    with _worker(path) as first, _worker(path) as second:
        for process, letter in zip((first, second), ("a", "b"), strict=True):
            operation = _dispatch(
                token, _new(cid="cloned-token-" + letter), "new-" + letter, 10
            )
            inputs.append(operation)
            _send(process, operation)
        replies = [_receive(process) for process in (first, second)]
        assert all("result" in reply for reply in replies)
    before_fill = authority.snapshot(ACCOUNT)
    assert before_fill["generation"] == 1 and before_fill["entry_enabled"] is True
    assert len(before_fill["orders"]) == 2
    assert sum(order["remaining_quantity"] for order in before_fill["orders"]) == 14
    reports = authority.trade(ACCOUNT, Side.BID, 14, timestamp_ns=20)
    quantity = sum(report["fill"]["quantity"] for report in reports)
    cash = -sum(
        report["fill"]["quantity"] * report["fill"]["price_ticks"] for report in reports
    )
    assert (quantity, cash) == (14, -1582)
    _runtime_race(
        pytestconfig,
        tmp_path,
        {
            "scenario": "shared-current-token-counterexample",
            "dispatch_inputs": inputs,
            "dispatch_outputs": replies,
            "ordered_authority": authority.snapshot(ACCOUNT),
        },
    )
    # This is an explicit bounded-model limitation: the same trusted serialized
    # current token names one owner context, not one operating-system process.
    _publish(
        pytestconfig,
        tmp_path,
        "shared-current-token-counterexample",
        {
            "process_deaths": 0,
            "concurrent_interpreters": 2,
            "accepted_orders": 2,
            "independent_signed_quantity": quantity,
            "independent_cash_ticks": cash,
            "generation": before_fill["generation"],
            "entry_enabled": before_fill["entry_enabled"],
            "client_identities": sorted(
                order["client_order_id"] for order in before_fill["orders"]
            ),
        },
    )


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_independent_old_order_fills_after_takeover_delayed_detail_and_cancel(
    tmp_path: Path, pytestconfig: pytest.Config, side: Side
) -> None:
    authority = SimulatedAccountAuthorityV3.create(
        tmp_path / "authority.sqlite", AUTHORITY
    )
    old = _claim(authority)
    request = _new(side)
    root = tmp_path / "client"
    with OutboundRecoverySessionV2.create(
        root, ACCOUNT, "session-a", STRATEGY
    ) as client:
        acceptance: list[dict[str, Any]] = []
        client.submit_new(
            request,
            timestamp_ns=10,
            sink=lambda action: acceptance.append(
                authority.dispatch(old, "new-seven", action, timestamp_ns=10)
            ),
        )
        client.capture(to_receipt(acceptance[0], 11))
    first = authority.trade(ACCOUNT, side, 2, timestamp_ns=20)
    current = _claim(authority, 1, owner="owner-b", session="session-b")
    # The fence prevents stale command effects but cannot retract an existing
    # accepted order. Three units have executed when cancellation arrives.
    later = authority.trade(ACCOUNT, side, 1, timestamp_ns=30)
    with pytest.raises(RecoveryError):
        authority.dispatch(
            old,
            "old-cancel",
            CancelRequest(31, 31, client_order_id=request.client_order_id),
            timestamp_ns=31,
        )
    cancelled = authority.dispatch(
        current,
        "current-cancel",
        CancelRequest(40, 40, client_order_id=request.client_order_id),
        timestamp_ns=40,
    )
    reports = first + later
    fills = {report["fill"]["fill_id"]: report["fill"] for report in reports}
    assert len(fills) == 2
    scalar_quantity = int(side) * sum(fill["quantity"] for fill in fills.values())
    scalar_cash = -int(side) * sum(
        fill["quantity"] * fill["price_ticks"] for fill in fills.values()
    )
    assert scalar_quantity == int(side) * 3
    assert scalar_cash == -int(side) * 339
    with OutboundRecoverySessionV2.open(root, ACCOUNT, "session-a", STRATEGY) as client:
        client.capture(to_receipt(cancelled, 41))
        terminal_without_detail = client.snapshot()
        assert terminal_without_detail["ledger"]["known_inventory"] == 0
        assert terminal_without_detail["knowledge"]["inventory_min"] == scalar_quantity
        assert terminal_without_detail["knowledge"]["inventory_max"] == scalar_quantity
        for stamp, report in enumerate(reversed(reports), 42):
            client.capture(to_receipt(report, stamp))
        assert client.capture(to_receipt(reports[0], 44)) is False
        received = client.snapshot()
        assert received["entry_enabled"] is False
        assert received["ledger"]["known_inventory"] == scalar_quantity
        assert received["ledger"]["trade_cash_ticks"] == scalar_cash
    assert authority.trade(ACCOUNT, side, 7, timestamp_ns=50) == []
    state = authority.snapshot(ACCOUNT)
    assert state["inventory"] == scalar_quantity
    assert state["cash_ticks"] == scalar_cash
    order = state["orders"][0]
    assert (
        order["original_quantity"],
        order["filled_quantity"],
        order["remaining_quantity"],
        order["cancelled_quantity"],
    ) == (7, 3, 0, 4)
    _publish(
        pytestconfig,
        tmp_path,
        "old-order-delayed-detail-" + side.name.lower(),
        {
            "process_deaths": 0,
            "venue": state,
            "terminal_without_detail": terminal_without_detail,
            "received": received,
            "raw_unique_fills": list(fills.values()),
            "independent_signed_quantity": scalar_quantity,
            "independent_cash_ticks": scalar_cash,
        },
    )


def test_independent_owner_and_order_identity_tombstones(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    authority = SimulatedAccountAuthorityV3.create(
        tmp_path / "authority.sqlite", AUTHORITY
    )
    token = _claim(authority)
    request = _new()
    authority.dispatch(token, "first-new", request, timestamp_ns=10)
    authority.dispatch(
        token,
        "terminal-cancel",
        CancelRequest(20, 20, client_order_id=request.client_order_id),
        timestamp_ns=20,
    )
    before = authority.snapshot(ACCOUNT)
    with pytest.raises(RecoveryError):
        authority.dispatch(token, "reused-client", request, timestamp_ns=30)
    with pytest.raises(RecoveryError):
        authority.dispatch(
            token,
            "first-new",
            replace(request, client_order_id="different", quantity=8),
            timestamp_ns=30,
        )
    assert authority.snapshot(ACCOUNT) == before
    new = _claim(authority, 1, owner="owner-b", session="session-b")
    after = authority.snapshot(ACCOUNT)
    with pytest.raises(RecoveryError):
        _claim(authority, 2, owner="owner-a", session="session-c")
    with pytest.raises(RecoveryError):
        authority.dispatch(
            replace(new, authority_id="other-authority"),
            "wrong-authority",
            CancelRequest(40, 40, client_order_id=request.client_order_id),
            timestamp_ns=40,
        )
    assert authority.snapshot(ACCOUNT) == after
    _publish(
        pytestconfig,
        tmp_path,
        "retained-owner-order-identities",
        {"process_deaths": 0, "before_takeover": before, "after_takeover": after},
    )


def test_independent_adapter_rejects_genuine_foreign_terminal_and_mutated_report(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    authority = SimulatedAccountAuthorityV3.create(
        tmp_path / "authority.sqlite", AUTHORITY
    )
    token = _claim(authority)
    foreign = authority.claim(
        "different-account",
        "owner-foreign",
        "session-foreign",
        STRATEGY,
        expected_generation=0,
    )
    request = _new()
    authority.dispatch(foreign, "foreign-new", request, timestamp_ns=10)
    wrong_terminal = authority.dispatch(
        foreign,
        "foreign-terminal",
        CancelRequest(20, 20, client_order_id=request.client_order_id),
        timestamp_ns=20,
    )
    root = tmp_path / "client"
    with OutboundRecoverySessionV2.create(
        root, ACCOUNT, "session-a", STRATEGY
    ) as journal:
        adapter = FencedRecoverySessionV3(journal, authority, token)
        adapter.submit_new(request, timestamp_ns=10, deliver=lambda _: None)
        before = journal.snapshot()
        with pytest.raises(RecoveryError):
            adapter.capture(wrong_terminal, notification_timestamp_ns=21)
        assert journal.snapshot() == before
        acceptance = authority.lookup(ACCOUNT, "new-" + request.client_order_id)
        assert acceptance is not None
        with pytest.raises(RecoveryError):
            adapter.capture(
                acceptance | {"remaining_quantity": 6}, notification_timestamp_ns=21
            )
        assert journal.snapshot() == before
        assert adapter.capture(acceptance, notification_timestamp_ns=21)
        _claim(authority, 1, owner="owner-b", session="session-b")
        reports = authority.trade(ACCOUNT, Side.BID, 7, timestamp_ns=30)
        assert len(reports) == 1
        # Receipt evidence remains valid for a retained old-generation order.
        assert adapter.capture(reports[0], notification_timestamp_ns=31)
        assert adapter.capture(reports[0], notification_timestamp_ns=32) is False
        after = journal.snapshot()
        assert after["ledger"]["known_inventory"] == 7
        assert after["ledger"]["trade_cash_ticks"] == -791
        assert after["cancellation_work"][0]["terminal"] is True
    _publish(
        pytestconfig,
        tmp_path,
        "source-bound-capture",
        {
            "process_deaths": 0,
            "foreign_terminal": wrong_terminal,
            "before": before,
            "after": after,
            "venue": authority.snapshot(ACCOUNT),
        },
    )


@pytest.mark.parametrize("operation", ("claim", "dispatch"))
@pytest.mark.parametrize("stage", ("before_commit", "after_commit"))
def test_independent_real_death_has_only_transactional_old_or_new_effect(
    tmp_path: Path, pytestconfig: pytest.Config, operation: str, stage: str
) -> None:
    path = tmp_path / "authority.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = _claim(authority)
    if operation == "claim":
        command = {
            "operation": "claim",
            "owner_id": "owner-b",
            "session_id": "session-b",
            "expected_generation": 1,
            "pause": stage,
        }
    else:
        command = _dispatch(asdict(token), _new(), "lost-reply-new", 10, pause=stage)
    with _worker(path) as process:
        _send(process, command)
        assert _receive(process) == {"paused": stage}
        process.kill()
        assert process.wait(timeout=10) == -9
    reopened = SimulatedAccountAuthorityV3.open(path, AUTHORITY)
    state = reopened.snapshot(ACCOUNT)
    committed = stage == "after_commit"
    if operation == "claim":
        assert state["generation"] == 1 + int(committed)
        assert state["entry_enabled"] is (not committed)
    else:
        assert len(state["orders"]) == int(committed)
        assert len(state["commands"]) == int(committed)
        receipt = reopened.lookup(ACCOUNT, "lost-reply-new")
        assert (receipt is not None) is committed
        if committed:
            assert receipt is not None
            assert receipt["remaining_quantity"] == 7
    _publish(
        pytestconfig,
        tmp_path,
        "real-death-" + operation + "-" + stage,
        {"process_deaths": 1, "operation": operation, "stage": stage, "venue": state},
    )


def test_independent_cancel_retry_exhaustion_retains_seven_unit_exposure(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    authority = SimulatedAccountAuthorityV3.create(
        tmp_path / "authority.sqlite", AUTHORITY
    )
    token = _claim(authority)
    request = _new()
    root = tmp_path / "client"
    with OutboundRecoverySessionV2.create(
        root, ACCOUNT, "session-a", STRATEGY
    ) as client:
        client.submit_new(
            request,
            timestamp_ns=10,
            sink=lambda action: authority.dispatch(
                token, "new-seven", action, timestamp_ns=10
            ),
        )
    _claim(authority, 1, owner="owner-b", session="session-b")
    with OutboundRecoverySessionV2.open(root, ACCOUNT, "session-a", STRATEGY) as client:
        for index in range(4):
            assert client.recover_cancel(
                request.client_order_id,
                timestamp_ns=20 + index,
                sink=lambda _: None,  # all four messages lost before venue processing
            )
        state = client.snapshot()
        assert state["entry_enabled"] is False
        assert state["cancellation_work"][0]["exhausted"] is True
        assert state["cancellation_work"][0]["terminal"] is False
        assert state["knowledge"]["inventory_min"] == 0
        assert state["knowledge"]["inventory_max"] == 7
        with pytest.raises(RecoveryError):
            client.recover_cancel(
                request.client_order_id,
                timestamp_ns=30,
                sink=lambda _: pytest.fail("exhausted send"),
            )
    reports = authority.trade(ACCOUNT, Side.BID, 7, timestamp_ns=40)
    venue = authority.snapshot(ACCOUNT)
    assert sum(report["fill"]["quantity"] for report in reports) == 7
    assert venue["inventory"] == 7 and venue["cash_ticks"] == -791
    assert venue["entry_enabled"] is False
    _publish(
        pytestconfig,
        tmp_path,
        "retry-exhaustion-is-not-flat",
        {"process_deaths": 0, "client": state, "venue": venue, "reports": reports},
    )


def test_independent_authority_rollback_resurrects_stale_entry_counterexample(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    path = tmp_path / "authority.sqlite"
    saved = tmp_path / "old-authority.sqlite"
    authority = SimulatedAccountAuthorityV3.create(path, AUTHORITY)
    token = _claim(authority)
    with sqlite3.connect(path) as source, sqlite3.connect(saved) as destination:
        source.backup(destination)
    _claim(authority, 1, owner="owner-b", session="session-b")
    fenced = authority.snapshot(ACCOUNT)
    with pytest.raises(RecoveryError):
        authority.dispatch(token, "stale-before-rollback", _new(), timestamp_ns=10)
    # Deliberate restoration of trusted authoritative state is outside the
    # claimed fence: there is no independent monotonic external watermark.
    with sqlite3.connect(saved) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    restored = SimulatedAccountAuthorityV3.open(path, AUTHORITY)
    accepted = restored.dispatch(token, "stale-after-rollback", _new(), timestamp_ns=10)
    reports = restored.trade(ACCOUNT, Side.BID, 7, timestamp_ns=20)
    exposed = restored.snapshot(ACCOUNT)
    assert accepted["remaining_quantity"] == 7
    assert exposed["generation"] == 1 and exposed["entry_enabled"] is True
    assert exposed["inventory"] == 7 and exposed["cash_ticks"] == -791
    assert sum(report["fill"]["quantity"] for report in reports) == 7
    _publish(
        pytestconfig,
        tmp_path,
        "authority-rollback-counterexample",
        {"process_deaths": 0, "fenced": fenced, "after_rollback": exposed},
    )
