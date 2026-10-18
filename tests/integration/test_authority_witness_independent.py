"""Independent raw-input, process-death and missing-evidence challenges for v4."""

from __future__ import annotations

import hashlib
import json
import select
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from lobmm.account_fence import SimulatedAccountAuthorityV3
from lobmm.authority_witness import WitnessedAccountAuthorityV4, WitnessToken
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest
from tools.witness_independent_v4 import read_store, reconstruct

ACCOUNT = "independent-account-v4"
AUTHORITY = "independent-authority-v4"
WITNESS = "independent-witness-v4"
INCARNATION = "independent-incarnation-v4"
STRATEGY = "independent-review-v4"
WORKER = Path(__file__).resolve().parents[2] / "tools/witness_independent_v4.py"


def _new(client_id: str = "bid-seven", side: Side = Side.BID) -> NewOrderRequest:
    return NewOrderRequest(
        client_id,
        side,
        113 if side is Side.BID else 127,
        7 if side is Side.BID else 5,
        STRATEGY,
        1,
        2,
    )


def _world(
    path: Path, *, mixed: bool = False
) -> tuple[SimulatedAccountAuthorityV3, WitnessedAccountAuthorityV4]:
    venue = SimulatedAccountAuthorityV3.create(path / "authority.sqlite", AUTHORITY)
    owner = venue.claim(
        ACCOUNT, "owner-a", "session-a", STRATEGY, expected_generation=0
    )
    venue.dispatch(owner, "accepted-bid", _new(), timestamp_ns=10)
    if mixed:
        venue.dispatch(
            owner, "accepted-ask", _new("ask-five", Side.ASK), timestamp_ns=11
        )
    facade = WitnessedAccountAuthorityV4.enroll(
        venue.path, path / "witness.sqlite", AUTHORITY, WITNESS, INCARNATION
    )
    return venue, facade


def _claim(
    facade: WitnessedAccountAuthorityV4,
    identity: str = "takeover-b",
    expected: int = 1,
    owner: str = "b",
) -> WitnessToken:
    return facade.claim(
        identity,
        ACCOUNT,
        "owner-" + owner,
        "session-" + owner,
        STRATEGY,
        expected_generation=expected,
    )


def _observed(venue: SimulatedAccountAuthorityV3) -> dict[str, Any]:
    raw = read_store(venue.path, "authority")
    derived = reconstruct(raw["body"])[ACCOUNT]
    snapshot = venue.snapshot(ACCOUNT)
    assert (snapshot["generation"], snapshot["inventory"], snapshot["cash_ticks"]) == (
        derived["generation"],
        derived["inventory"],
        derived["cash_ticks"],
    )
    saved = {order["client_order_id"]: order for order in snapshot["orders"]}
    for expected in derived["orders"]:
        for key, value in expected.items():
            assert saved[expected["client_order_id"]][key] == value
    unique_fills = {
        report["fill"]["fill_id"]: report["fill"]
        for report in snapshot["reports"]
        if report["fill"] is not None
    }
    assert (
        sum(fill["side"] * fill["quantity"] for fill in unique_fills.values())
        == derived["inventory"]
    )
    assert (
        -sum(
            fill["side"] * fill["quantity"] * fill["price_ticks"]
            for fill in unique_fills.values()
        )
        == derived["cash_ticks"]
    )
    return {
        "raw_authority": raw,
        "from_raw_inputs": derived,
        "unique_report_fills": list(unique_fills.values()),
    }


def _publish(
    config: pytest.Config, temporary: Path, name: str, value: dict[str, Any]
) -> None:
    configured = config.getoption("--authority-witness-output", default=None)
    destination = (Path(configured) if configured else temporary) / "independent"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (name + ".json")).write_text(
        json.dumps({"synthetic_uncalibrated": True, **value}, sort_keys=True, indent=2)
        + "\n"
    )
    files = sorted(p for p in destination.glob("*.json") if p.name != "audit.json")
    audit = {
        "version": "independent-witness-v4",
        "case_count": len(files),
        "artifact_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
        "source_sha256": {
            "tests": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "worker_and_raw_auditor": hashlib.sha256(WORKER.read_bytes()).hexdigest(),
        },
    }
    (destination / "audit.json").write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n"
    )


def _receive(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], (
        "independent process did not reply"
    )
    line = process.stdout.readline()
    assert line, "independent worker exited before response"
    result: dict[str, Any] = json.loads(line)
    return result


def _send(process: subprocess.Popen[str], operation: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(operation) + "\n")
    process.stdin.flush()


@contextmanager
def _worker(facade: WitnessedAccountAuthorityV4) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(WORKER),
            str(facade.authority.path),
            "--worker",
            "--witness",
            str(facade.witness_path),
        ],
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


def _issuance(owner: str = "b", *, pause: str | None = None) -> dict[str, Any]:
    return {
        "operation": "claim",
        "issuance_id": "takeover-" + owner,
        "account_id": ACCOUNT,
        "owner_id": "owner-" + owner,
        "session_id": "session-" + owner,
        "strategy_id": STRATEGY,
        "expected_generation": 1,
        "pause": pause,
    }


def test_independent_mixed_book_issuance_identity_and_scalar_exposure(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    venue, facade = _world(tmp_path, mixed=True)
    facade.trade(ACCOUNT, Side.BID, 2, timestamp_ns=12)
    facade.trade(ACCOUNT, Side.ASK, 1, timestamp_ns=13)
    token = _claim(facade)
    retained = read_store(facade.witness_path, "witness")
    assert _claim(facade) == token
    assert read_store(facade.witness_path, "witness") == retained
    with pytest.raises(RecoveryError, match="conflicting issuance"):
        _claim(facade, owner="different")
    with pytest.raises(RecoveryError):
        _claim(facade, "competitor-c", owner="c")
    with pytest.raises(RecoveryError, match="quarantined"):
        facade.dispatch(
            token, "unaccepted-new", _new("unaccepted-new"), timestamp_ns=14
        )
    facade.trade(ACCOUNT, Side.BID, 1, timestamp_ns=15)
    facade.trade(ACCOUNT, Side.ASK, 2, timestamp_ns=16)
    for client_id in ("bid-seven", "ask-five"):
        request = CancelRequest(17, 17, client_order_id=client_id)
        facade.dispatch(token, "cancel-" + client_id, request, timestamp_ns=17)
    for client_id in ("bid-seven", "ask-five"):
        request = CancelRequest(17, 17, client_order_id=client_id)
        facade.dispatch(token, "cancel-" + client_id, request, timestamp_ns=18)
    assert facade.trade(ACCOUNT, Side.BID, 100, timestamp_ns=19) == []
    assert facade.trade(ACCOUNT, Side.ASK, 100, timestamp_ns=20) == []
    newest = _claim(facade, "takeover-c", 2, "c")
    assert _claim(facade) == token
    before = read_store(facade.witness_path, "witness")
    for altered in (
        token,
        replace(newest, issuance_id=token.issuance_id),
        replace(newest, witness_id="other"),
        replace(newest, incarnation_id="other"),
    ):
        with pytest.raises(RecoveryError):
            facade.dispatch(
                altered,
                "stale-or-spliced",
                CancelRequest(21, 21, client_order_id="bid-seven"),
                timestamp_ns=21,
            )
    assert read_store(facade.witness_path, "witness") == before
    observed = _observed(venue)
    result = observed["from_raw_inputs"]
    assert (result["inventory"], result["cash_ticks"]) == (0, 42)
    assert [
        (
            order["filled_quantity"],
            order["cancelled_quantity"],
            order["remaining_quantity"],
        )
        for order in result["orders"]
    ] == [(3, 4, 0), (3, 2, 0)]
    assert "unaccepted-new" not in result["command_ids"]
    _publish(
        pytestconfig,
        tmp_path,
        "mixed-book-and-issuance",
        {
            **observed,
            "witness": read_store(facade.witness_path, "witness"),
            "old_token": asdict(token),
            "new_token": asdict(newest),
            "entry_quarantined": facade.snapshot(ACCOUNT)["entry_enabled"] is False,
        },
    )


def test_independent_two_live_issuance_contenders(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    venue, facade = _world(tmp_path)
    operations = [_issuance(owner) for owner in ("b", "c")]
    with _worker(facade) as first, _worker(facade) as second:
        for process, operation in zip((first, second), operations, strict=True):
            _send(process, operation)
        replies = [_receive(first), _receive(second)]
        assert sorted("result" in reply for reply in replies) == [False, True]
    raw = read_store(venue.path, "authority")
    derived = reconstruct(raw["body"])[ACCOUNT]
    evidence = read_store(facade.witness_path, "witness")["body"]
    assert derived["generation"] == 2 and evidence["watermark"] == 1
    assert len(evidence["issuances"]) == 1 and evidence["pending"] is None
    configured = pytestconfig.getoption("--authority-witness-output", default=None)
    runtime = (Path(configured) if configured else tmp_path) / "independent"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "runtime-races.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "inputs": operations,
                    "replies": replies,
                    "raw_authority": raw,
                    "witness": evidence,
                },
                sort_keys=True,
            )
            + "\n"
        )
    _publish(
        pytestconfig,
        tmp_path,
        "two-live-issuance-contenders",
        {
            "successful_contenders": 1,
            "rejected_contenders": 1,
            "generation": derived["generation"],
            "watermark": evidence["watermark"],
            "retained_issuances": len(evidence["issuances"]),
            "accepted_order_remaining": derived["orders"][0]["remaining_quantity"],
            "processes_live_before_dispatch": 2,
            "raw_chronology": "runtime-races.jsonl",
        },
    )


@pytest.mark.parametrize(
    "stage",
    ["before_intent", "after_intent", "after_authority_commit", "after_finalize"],
)
def test_independent_real_process_death_and_cancellation_cost(
    tmp_path: Path, pytestconfig: pytest.Config, stage: str
) -> None:
    venue, facade = _world(tmp_path)
    with _worker(facade) as process:
        _send(process, _issuance(pause=stage))
        assert _receive(process) == {"paused": stage}
        process.kill()
        process.wait(timeout=10)
        assert process.returncode == -9
    observed_before = _observed(venue)
    retained_before = read_store(facade.witness_path, "witness")
    committed = stage in {"after_authority_commit", "after_finalize"}
    pending = stage in {"after_intent", "after_authority_commit"}
    assert observed_before["from_raw_inputs"]["generation"] == (2 if committed else 1)
    assert (retained_before["body"]["pending"] is not None) is pending
    if pending:
        with pytest.raises(RecoveryError, match="pending witness"):
            WitnessedAccountAuthorityV4.open(
                venue.path, facade.witness_path, AUTHORITY, WITNESS, INCARNATION
            )
        with pytest.raises(RecoveryError):
            _claim(facade)
        # Independently modeled market action is outside the frozen facade.
        # It demonstrates the residual cost, without silently repairing evidence.
        venue.trade(ACCOUNT, Side.BID, 7, timestamp_ns=30)
    else:
        token = _claim(facade)
        assert _claim(facade) == token
        facade.dispatch(
            token,
            "cancel-before-market",
            CancelRequest(20, 20, client_order_id="bid-seven"),
            timestamp_ns=20,
        )
        assert facade.trade(ACCOUNT, Side.BID, 7, timestamp_ns=30) == []
    after = _observed(venue)
    expected = (7, -791) if pending else (0, 0)
    assert (
        after["from_raw_inputs"]["inventory"],
        after["from_raw_inputs"]["cash_ticks"],
    ) == expected
    _publish(
        pytestconfig,
        tmp_path,
        "death-" + stage,
        {
            "pause_stage": stage,
            "real_sigkill": True,
            "before_market": observed_before,
            "witness_after_death": retained_before,
            "after_market": after,
            "cancellation_blocked": pending,
            "market_progress_used_direct_v3_outside_facade": pending,
        },
    )


def test_independent_matching_joint_rollback_erases_execution_history(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    venue, facade = _world(tmp_path)
    token = _claim(facade)
    saved_authority = tmp_path / "saved-authority.sqlite"
    saved_witness = tmp_path / "saved-witness.sqlite"
    shutil.copyfile(venue.path, saved_authority)
    shutil.copyfile(facade.witness_path, saved_witness)
    visible_before = {
        "authority": read_store(venue.path, "authority"),
        "witness": read_store(facade.witness_path, "witness"),
    }
    externally_observed = facade.trade(ACCOUNT, Side.BID, 3, timestamp_ns=20)
    assert sum(report["fill"]["quantity"] for report in externally_observed) == 3
    _claim(facade, "takeover-c", 2, "c")
    shutil.copyfile(saved_authority, venue.path)
    shutil.copyfile(saved_witness, facade.witness_path)
    reopened = WitnessedAccountAuthorityV4.open(
        venue.path, facade.witness_path, AUTHORITY, WITNESS, INCARNATION
    )
    visible_after = {
        "authority": read_store(venue.path, "authority"),
        "witness": read_store(facade.witness_path, "witness"),
    }
    assert visible_before == visible_after
    assert _claim(reopened) == token
    from_inputs = reconstruct(visible_after["authority"]["body"])[ACCOUNT]
    assert (from_inputs["inventory"], from_inputs["cash_ticks"]) == (0, 0)
    _publish(
        pytestconfig,
        tmp_path,
        "joint-rollback-impossibility",
        {
            "identical_visible_stores": True,
            "visible_stores": visible_after,
            "history_without_execution": {
                "external_inventory": 0,
                "external_cash_ticks": 0,
            },
            "history_with_execution": {
                "external_inventory": 3,
                "external_cash_ticks": -339,
                "retained_observer_reports_outside_restored_stores": externally_observed,
            },
            "restored_model_inventory": from_inputs["inventory"],
            "no_third_observation_in_facade": True,
        },
    )


def test_independent_byte_identical_replacement_and_changed_namespace(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    venue, facade = _world(tmp_path)
    original = read_store(venue.path, "authority")
    # Replacement with identical logical content cannot prove physical lineage.
    replacement = tmp_path / "replacement.sqlite"
    shutil.copyfile(venue.path, replacement)
    replacement.replace(venue.path)
    reopened = WitnessedAccountAuthorityV4.open(
        venue.path, facade.witness_path, AUTHORITY, WITNESS, INCARNATION
    )
    assert read_store(venue.path, "authority") == original
    token = _claim(reopened)
    venue.path.unlink()
    SimulatedAccountAuthorityV3.create(venue.path, AUTHORITY)
    with pytest.raises(RecoveryError, match="watermark mismatch"):
        reopened.snapshot(ACCOUNT)
    _publish(
        pytestconfig,
        tmp_path,
        "namespace-logical-versus-physical",
        {
            "byte_identical_logical_replacement_accepted": True,
            "changed_reset_rejected": True,
            "before_replacement": original,
            "token_before_changed_reset": asdict(token),
            "after_changed_reset": read_store(venue.path, "authority"),
            "retained_witness": read_store(facade.witness_path, "witness"),
            "physical_file_identity_not_attested": True,
        },
    )


def test_independent_checksumming_does_not_validate_issuance_semantics(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    venue, facade = _world(tmp_path)
    token = _claim(facade)
    connection = sqlite3.connect(facade.witness_path)
    try:
        evidence = read_store(facade.witness_path, "witness")["body"]
        evidence["issuances"][0]["request"]["owner_id"] = "contradictory-owner"
        raw = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        connection.execute(
            "UPDATE witness SET body=?,digest=? WHERE id=1",
            (raw, hashlib.sha256(raw).hexdigest()),
        )
        connection.commit()
    finally:
        connection.close()
    before = read_store(venue.path, "authority")
    with pytest.raises(RecoveryError):
        facade.dispatch(
            token,
            "must-not-cancel-on-contradictory-evidence",
            CancelRequest(20, 20, client_order_id="bid-seven"),
            timestamp_ns=20,
        )
    assert read_store(venue.path, "authority") == before
    _publish(
        pytestconfig,
        tmp_path,
        "malformed-issuance-rejected",
        {
            "fault": "correctly-checksummed contradictory request owner",
            "fault_requires_noncanonical_state_not_hostile_authentication_claim": True,
            "authority_unchanged": True,
            "raw_venue": before,
            "malformed_witness": read_store(facade.witness_path, "witness"),
        },
    )
