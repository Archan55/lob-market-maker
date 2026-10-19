"""Bounded write-intent/issuance experiments; raw evidence retained for review."""

from __future__ import annotations

import hashlib
import json
import select
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from lobmm.account_fence import SimulatedAccountAuthorityV3
from lobmm.authority_witness import WitnessedAccountAuthorityV4
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest

WORKER = Path(__file__).resolve().parents[2] / "tools/authority_witness_worker.py"


def raw(path, table):
    with sqlite3.connect(path) as connection:
        body, digest = connection.execute(f"SELECT body,digest FROM {table}").fetchone()
    assert hashlib.sha256(body).hexdigest() == digest
    return json.loads(body)


def setup(root, side=Side.BID):
    venue_path, witness_path = root / "venue.sqlite", root / "witness.sqlite"
    venue = SimulatedAccountAuthorityV3.create(venue_path, "venue")
    old = venue.claim(
        "account", "original", "original", "strategy", expected_generation=0
    )
    accepted = venue.dispatch(
        old,
        "accepted",
        NewOrderRequest("six", side, 101, 6, "strategy", 1, 1),
        timestamp_ns=1,
    )
    facade = WitnessedAccountAuthorityV4.enroll(
        venue_path, witness_path, "venue", "witness", "incarnation"
    )
    return venue, facade, accepted


def issue(facade, identity="recovery", expected=1):
    return facade.claim(
        identity,
        "account",
        identity,
        identity,
        "strategy",
        expected_generation=expected,
    )


@pytest.fixture(scope="module")
def archive(request, tmp_path_factory):
    output = request.config.getoption("--authority-witness-output")
    root = (
        Path(output) if output else tmp_path_factory.mktemp("witness-evidence")
    ) / "primary"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    cases = sorted(root.glob("case-*.json"))
    (root / "audit.json").write_text(
        json.dumps(
            dict(
                synthetic_uncalibrated=True,
                cases=len(cases),
                process_deaths=sum(
                    json.loads(p.read_text()).get("process_deaths", 0) for p in cases
                ),
                artifacts={
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cases
                },
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def save(archive, name, venue, facade, **extra):
    evidence = dict(
        authority=raw(venue.path, "authority"),
        witness=raw(facade.witness_path, "witness"),
        **extra,
    )
    (archive / ("case-" + name + ".json")).write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n"
    )


def receive(process):
    assert select.select([process.stdout], [], [], 20)[0], "worker timed out"
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def send(process, value):
    process.stdin.write(value + "\n")
    process.stdin.flush()


@contextmanager
def worker(venue, facade, request):
    process = subprocess.Popen(
        [sys.executable, "-u", str(WORKER), str(venue.path), str(facade.witness_path)],
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


@pytest.mark.parametrize("side", [Side.BID, Side.ASK])
def test_accepted_exposure_cancel_and_quarantine(tmp_path, archive, side):
    venue, facade, accepted = setup(tmp_path, side)
    token = issue(facade)
    reports = facade.trade("account", side, 2, timestamp_ns=2)
    terminal = facade.dispatch(
        token, "cancel", CancelRequest(3, 3, client_order_id="six"), timestamp_ns=3
    )
    # Response loss/retry does not cancel twice; stale accepted detail remains true.
    assert (
        facade.dispatch(
            token, "cancel", CancelRequest(3, 3, client_order_id="six"), timestamp_ns=4
        )
        == terminal
    )
    assert facade.trade("account", side, 6, timestamp_ns=5) == []
    state = facade.snapshot("account")
    assert (state["inventory"], state["cash_ticks"]) == (
        int(side) * 2,
        -int(side) * 202,
    )
    assert state["orders"][0]["cancelled_quantity"] == 4
    for current in (token, replace(token, issuance_id="unissued")):
        with pytest.raises(RecoveryError, match="quarantined"):
            facade.dispatch(
                current,
                "new",
                NewOrderRequest("unaccepted", side, 101, 6, "strategy", 1, 1),
                timestamp_ns=6,
            )
    assert not state["entry_enabled"]
    save(
        archive,
        f"exposure-{int(side)}",
        venue,
        facade,
        accepted=accepted,
        fills=reports,
        terminal=terminal,
        state=state,
    )


@pytest.mark.parametrize(
    "boundary",
    ["before_intent", "after_intent", "after_authority_commit", "after_finalize"],
)
def test_process_death_and_lost_takeover_response(tmp_path, archive, boundary):
    venue, facade, _ = setup(tmp_path)
    request = dict(
        operation="claim", owner="recovery", issuance_id="recovery", pause=boundary
    )
    with worker(venue, facade, request) as process:
        send(process, "go")
        assert receive(process) == {"boundary": boundary}
        process.kill()
        assert process.wait(timeout=10) == -9
    generation = venue.snapshot("account")["generation"]
    assert generation == (
        2 if boundary in ("after_authority_commit", "after_finalize") else 1
    )
    if boundary in ("before_intent", "after_finalize"):
        token = issue(facade)
        assert token == issue(facade)
        assert facade.snapshot("account")["generation"] == 2
    else:
        with pytest.raises(RecoveryError, match="pending"):
            issue(facade)
        # Cancellation frozen does not stop the already accepted venue order.
        venue.trade("account", Side.BID, 6, timestamp_ns=2)
        assert venue.snapshot("account")["inventory"] == 6
    save(
        archive,
        boundary,
        venue,
        facade,
        process_deaths=1,
        request=request,
        observed_generation=generation,
        final=venue.snapshot("account"),
    )


@pytest.mark.parametrize("same_id", [True, False])
def test_actual_competing_issuers(tmp_path, archive, same_id):
    venue, facade, _ = setup(tmp_path)
    a = dict(operation="claim", owner="a", issuance_id="a")
    b = a if same_id else dict(operation="claim", owner="b", issuance_id="b")
    with worker(venue, facade, a) as first, worker(venue, facade, b) as second:
        send(first, "go")
        send(second, "go")
        replies = [receive(first), receive(second)]
    successes = [r for r in replies if "result" in r]
    assert len(successes) == (2 if same_id else 1)
    if same_id:
        assert successes[0] == successes[1]
    assert facade.snapshot("account")["generation"] == 2
    # OS winner is retained separately; canonical proof is winner-independent.
    with (archive / "runtime-races.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                dict(
                    requests=[a, b],
                    replies=replies,
                    authority=raw(venue.path, "authority"),
                    witness=raw(facade.witness_path, "witness"),
                ),
                sort_keys=True,
            )
            + "\n"
        )
    (archive / f"case-competing-{same_id}.json").write_text(
        json.dumps(
            dict(
                same_id=same_id,
                successes=len(successes),
                generation=2,
                issuance_records=len(raw(facade.witness_path, "witness")["issuances"]),
            ),
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def test_duplicate_conflict_and_historical_issuance(tmp_path, archive):
    venue, facade, _ = setup(tmp_path)
    old = issue(facade)
    current = issue(facade, "next", 2)
    before = raw(facade.witness_path, "witness")
    assert issue(facade) == old
    with pytest.raises(RecoveryError, match="conflicting issuance"):
        facade.claim(
            "recovery",
            "account",
            "different",
            "different",
            "strategy",
            expected_generation=1,
        )
    for invalid in (
        old,
        replace(old, context=current.context),
        replace(current, witness_id="foreign"),
    ):
        with pytest.raises(RecoveryError):
            facade.dispatch(
                invalid,
                "bad",
                CancelRequest(2, 2, client_order_id="six"),
                timestamp_ns=2,
            )
    assert before == raw(facade.witness_path, "witness")
    save(
        archive,
        "duplicate-stale-splice",
        venue,
        facade,
        old=asdict(old),
        current=asdict(current),
    )


@pytest.mark.parametrize("restore", ["venue", "witness", "both", "client"])
def test_rollback_combinations_and_impossibility(tmp_path, archive, restore):
    venue, facade, _ = setup(tmp_path)
    old = issue(facade)
    saved_venue, saved_witness = tmp_path / "old-v", tmp_path / "old-w"
    shutil.copyfile(venue.path, saved_venue)
    shutil.copyfile(facade.witness_path, saved_witness)
    token = issue(facade, "next", 2)
    external = facade.trade("account", Side.BID, 2, timestamp_ns=2)
    before = dict(
        authority=raw(venue.path, "authority"),
        witness=raw(facade.witness_path, "witness"),
    )
    if restore in ("venue", "both"):
        shutil.copyfile(saved_venue, venue.path)
    if restore in ("witness", "both"):
        shutil.copyfile(saved_witness, facade.witness_path)
    if restore in ("venue", "witness"):
        with pytest.raises(RecoveryError, match="mismatch"):
            facade.snapshot("account")
    elif restore == "client":
        with pytest.raises(RecoveryError, match="stale"):
            facade.dispatch(
                old,
                "cancel",
                CancelRequest(3, 3, client_order_id="six"),
                timestamp_ns=3,
            )
        facade.dispatch(
            token, "cancel", CancelRequest(3, 3, client_order_id="six"), timestamp_ns=3
        )
        assert facade.snapshot("account")["orders"][0]["cancelled_quantity"] == 4
    else:
        # These exact visible document bytes match a history with no external fill.
        assert raw(venue.path, "authority") == raw(saved_venue, "authority")
        assert raw(facade.witness_path, "witness") == raw(saved_witness, "witness")
        assert facade.snapshot("account")["inventory"] == 0
        facade.dispatch(
            old, "cancel", CancelRequest(3, 3, client_order_id="six"), timestamp_ns=3
        )
        assert facade.snapshot("account")["orders"][0]["cancelled_quantity"] == 6
        assert sum(r["fill"]["quantity"] for r in external) == 2
    save(
        archive,
        "rollback-" + restore,
        venue,
        facade,
        external_executed=external,
        before=before,
    )


def test_witness_unavailable_namespace_and_copied_current_token(tmp_path, archive):
    venue, facade, _ = setup(tmp_path)
    token = issue(facade)
    hidden = tmp_path / "hidden"
    facade.witness_path.rename(hidden)
    with pytest.raises(RecoveryError, match="unavailable"):
        facade.snapshot("account")
    hidden.rename(facade.witness_path)
    with pytest.raises(RecoveryError, match="lineage"):
        WitnessedAccountAuthorityV4.open(
            venue.path, facade.witness_path, "venue", "witness", "reset"
        )
    with (
        worker(venue, facade, dict(operation="cancel", token=asdict(token))) as first,
        worker(venue, facade, dict(operation="cancel", token=asdict(token))) as second,
    ):
        send(first, "go")
        send(second, "go")
        replies = [receive(first), receive(second)]
    assert replies[0] == replies[1] and "result" in replies[0]
    assert facade.snapshot("account")["orders"][0]["cancelled_quantity"] == 6
    save(
        archive,
        "unavailable-namespace-copied-token",
        venue,
        facade,
        replies=replies,
        process_exclusivity=False,
    )


def test_v3_bypass_is_concrete_failure(tmp_path, archive):
    venue, facade, _ = setup(tmp_path)
    # No facade issuance yet: v3 initial token is still valid outside the gate.
    old = venue.claim(
        "other-account", "bypass", "bypass", "strategy", expected_generation=0
    )
    venue.dispatch(
        old,
        "outside",
        NewOrderRequest("outside", Side.BID, 101, 6, "strategy", 1, 1),
        timestamp_ns=2,
    )
    with pytest.raises(RecoveryError, match="mismatch"):
        facade.snapshot("account")
    assert venue.snapshot("other-account")["orders"][0]["remaining_quantity"] == 6
    save(archive, "direct-v3-bypass", venue, facade, unwitnessed_executable_quantity=6)


def test_held_unaccepted_process_after_takeover(tmp_path, archive):
    venue, facade, accepted = setup(tmp_path)
    old = issue(facade)
    held = dict(operation="new", token=asdict(old))
    with worker(venue, facade, held) as process:
        current = issue(facade, "next", 2)
        send(process, "go")
        reply = receive(process)
    assert "quarantined" in reply["error"]
    facade.dispatch(
        current, "cancel", CancelRequest(2, 2, client_order_id="six"), timestamp_ns=2
    )
    state = facade.snapshot("account")
    assert len(state["orders"]) == 1
    assert state["orders"][0]["cancelled_quantity"] == 6
    save(
        archive,
        "held-unaccepted",
        venue,
        facade,
        held=held,
        reply=reply,
        accepted=accepted,
    )
