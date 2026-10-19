"""Repeat a fixed synthetic pending-cancel study; no recovery/entry is enabled.

Run from repository root: python -m tools.pending_intent_experiment OUTPUT.
Evidence independence and completeness are assumed oracle properties, not a
third-storage implementation. Raw SQLite observations are test-harness inputs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lobmm.account_fence import SimulatedAccountAuthorityV3
from lobmm.authority_witness import WitnessedAccountAuthorityV4
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest
from tools.pending_intent_evidence import canonical, classify, digest
from tools.witness_independent_v4 import read_store, reconstruct

BASELINE = "a452915e940f70dc642b9a67d4874b79bdf09ba3"
LINEAGE = dict(
    authority_id="pending-evidence-authority",
    witness_id="pending-evidence-witness",
    incarnation_id="pending-evidence-incarnation",
)
ACCOUNT = "pending-evidence-account"
STRATEGY = "pending-evidence-study"


class InjectedStop(RuntimeError):
    """Synchronous fail-stop injection releases local SQLite transactions."""


def _raw(venue: SimulatedAccountAuthorityV3) -> dict[str, Any]:
    return read_store(venue.path, "authority")["body"]


def transition(
    before: dict[str, Any], after: dict[str, Any], sequence: int
) -> dict[str, Any]:
    changes = [
        account
        for account in before["accounts"]
        if before["accounts"][account] != after["accounts"][account]
    ]
    assert len(changes) == 1
    account = changes[0]
    old, new = before["accounts"][account], after["accounts"][account]
    assert new[:-1] == old
    return dict(
        sequence=sequence,
        account_id=account,
        event=new[-1],
        before_digest=digest(before),
        after_digest=digest(after),
    )


def _derived(document: dict[str, Any]) -> dict[str, Any]:
    result = reconstruct(document)[ACCOUNT]
    live = result["orders"]
    result["executable_quantity"] = sum(o["remaining_quantity"] for o in live)
    result["attainable_inventory_interval"] = [
        result["inventory"]
        - sum(o["remaining_quantity"] for o in live if o["side"] == -1),
        result["inventory"]
        + sum(o["remaining_quantity"] for o in live if o["side"] == 1),
    ]
    return result


def _cut(archive: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
    return dict(
        source_id=archive["source_id"],
        lineage=copy.deepcopy(LINEAGE),
        query_id="independently-supplied-closed-query-cut",
        sequence=len(archive["transitions"]),
        head_digest=digest(head),
        coverage="all-authority-transitions",
        pending_writer_terminal=True,
    )


def run_case(
    directory: Path, side: int, committed: bool, prefill: int, postfill: int
) -> dict[str, Any]:
    directory.mkdir(parents=True)
    venue = SimulatedAccountAuthorityV3.create(
        directory / "authority.sqlite", LINEAGE["authority_id"]
    )
    initial = venue.claim(
        ACCOUNT, "initial-owner", "initial-session", STRATEGY, expected_generation=0
    )
    for identity, price, quantity in (
        ("target", 113 if side == 1 else 127, 7),
        ("sibling", 109 if side == 1 else 131, 5),
    ):
        venue.dispatch(
            initial,
            "accept-" + identity,
            NewOrderRequest(identity, Side(side), price, quantity, STRATEGY, 1, 1),
            timestamp_ns=10,
        )
    facade = WitnessedAccountAuthorityV4.enroll(
        venue.path,
        directory / "witness.sqlite",
        LINEAGE["authority_id"],
        LINEAGE["witness_id"],
        LINEAGE["incarnation_id"],
    )
    owner = facade.claim(
        "recovery-issuance",
        ACCOUNT,
        "recovery-owner",
        "recovery-session",
        STRATEGY,
        expected_generation=1,
    )
    if prefill:
        facade.trade(ACCOUNT, Side(side), prefill, timestamp_ns=15)
    baseline = _raw(venue)
    request = CancelRequest(20, 20, client_order_id="target")
    event = dict(
        kind="command",
        token=asdict(owner.context),
        command_id="pending-target-cancel",
        request=dict(kind="cancel", payload=asdict(request), dispatch_timestamp_ns=20),
        timestamp_ns=20,
    )
    proposed = copy.deepcopy(baseline)
    proposed["accounts"][ACCOUNT].append(event)
    boundary = "after_authority_commit" if committed else "after_intent"

    def stop(stage: str) -> None:
        if stage == boundary:
            raise InjectedStop(stage)

    try:
        facade.dispatch(
            owner, "pending-target-cancel", request, timestamp_ns=20, hook=stop
        )
    except InjectedStop as exc:
        assert str(exc) == boundary
    else:
        raise AssertionError("injected boundary was not reached")
    pending_witness = read_store(facade.witness_path, "witness")["body"]
    pending = pending_witness["pending"]
    assert pending["old_digest"] == digest(baseline)
    assert pending["proposed_digest"] == digest(proposed)
    capsule = dict(
        lineage=copy.deepcopy(LINEAGE),
        watermark=pending["watermark"],
        old_digest=pending["old_digest"],
        proposed_digest=pending["proposed_digest"],
        baseline=baseline,
        account_id=ACCOUNT,
        event=event,
    )
    archive: dict[str, Any] = dict(
        source_id="hypothetical-independent-complete-transition-source",
        lineage=copy.deepcopy(LINEAGE),
        baseline_digest=digest(baseline),
        transitions=[],
    )
    after_boundary = _raw(venue)
    if committed:
        assert after_boundary == proposed
        archive["transitions"].append(transition(baseline, after_boundary, 1))
    else:
        assert after_boundary == baseline
    if postfill:
        venue.trade(ACCOUNT, Side(side), postfill, timestamp_ns=30)
        archive["transitions"].append(
            transition(after_boundary, _raw(venue), len(archive["transitions"]) + 1)
        )
    at_cut = _raw(venue)
    cut = _cut(archive, at_cut)
    before_bytes = [venue.path.read_bytes(), facade.witness_path.read_bytes()]
    decision = classify(capsule, archive, cut, at_cut)
    assert decision["disposition"] == (
        "observed_exact_commit" if committed else "certified_no_commit"
    )
    assert before_bytes == [venue.path.read_bytes(), facade.witness_path.read_bytes()]
    errors = {}
    for name, operation in (
        ("snapshot", lambda: facade.snapshot(ACCOUNT)),
        ("facade_trade", lambda: facade.trade(ACCOUNT, Side(side), 1, timestamp_ns=35)),
        (
            "target_cancel_retry",
            lambda: facade.dispatch(
                owner, "pending-target-cancel", request, timestamp_ns=35
            ),
        ),
        (
            "sibling_cancel",
            lambda: facade.dispatch(
                owner,
                "blocked-sibling-cancel",
                CancelRequest(35, 35, client_order_id="sibling"),
                timestamp_ns=35,
            ),
        ),
        (
            "new_entry",
            lambda: facade.dispatch(
                owner,
                "quarantined-new",
                NewOrderRequest("new", Side(side), 100, 1, STRATEGY, 35, 35),
                timestamp_ns=35,
            ),
        ),
    ):
        try:
            operation()
        except RecoveryError as exc:
            errors[name] = str(exc)
        else:
            raise AssertionError(name + " unexpectedly permitted")
    assert before_bytes == [venue.path.read_bytes(), facade.witness_path.read_bytes()]
    # Autonomous accepted-order execution continues after local fail-stop.
    venue.trade(ACCOUNT, Side(side), 7, timestamp_ns=40)
    final = _raw(venue)
    independent = _derived(final)
    snapshot = venue.snapshot(ACCOUNT)
    assert (independent["inventory"], independent["cash_ticks"]) == (
        snapshot["inventory"],
        snapshot["cash_ticks"],
    )
    for raw_order, venue_order in zip(
        independent["orders"], snapshot["orders"], strict=True
    ):
        for key, value in raw_order.items():
            assert venue_order[key] == value
    initial_risk, cut_risk = _derived(after_boundary), _derived(at_cut)
    return dict(
        synthetic_uncalibrated=True,
        inputs=dict(side=side, committed=committed, prefill=prefill, postfill=postfill),
        failure_boundary=boundary,
        fail_stop_exception_not_sigkill=True,
        capsule=capsule,
        archive=archive,
        independently_supplied_cut=cut,
        raw_after_boundary=after_boundary,
        raw_at_cut=at_cut,
        raw_final=final,
        raw_witness=pending_witness,
        classification=decision,
        classification_left_both_stores_byte_identical=True,
        current_hash_matches_old=digest(at_cut) == pending["old_digest"],
        current_hash_matches_proposed=digest(at_cut) == pending["proposed_digest"],
        after_boundary_from_raw_inputs=initial_risk,
        at_cut_from_raw_inputs=cut_risk,
        final_from_raw_inputs=independent,
        executable_quantity_time_20_to_50=(
            10 * initial_risk["executable_quantity"]
            + 10 * cut_risk["executable_quantity"]
            + 10 * independent["executable_quantity"]
        ),
        blocked_operations_after_classification=errors,
        cancellation_liveness_improved=False,
        entry_recovery_enabled=False,
    )


def negative_cases(case: dict[str, Any]) -> dict[str, Any]:
    capsule = case["capsule"]
    source = case["archive"]
    closed_cut = case["independently_supplied_cut"]
    current = case["raw_at_cut"]
    outcomes = {}
    for fault in (
        "missing",
        "stale-cut",
        "gap",
        "contradictory",
        "wrong-lineage",
        "nonterminal",
        "observer-crash-gap",
        "fill-only",
    ):
        archive, cut = copy.deepcopy(source), copy.deepcopy(closed_cut)
        if fault == "missing":
            archive = None
        elif fault == "stale-cut":
            cut["sequence"], cut["head_digest"] = 0, capsule["old_digest"]
        elif fault == "gap":
            archive["transitions"][0]["sequence"] = 2
        elif fault == "contradictory":
            archive["transitions"][-1]["event"]["quantity"] += 1
        elif fault == "wrong-lineage":
            archive["lineage"]["incarnation_id"] = "foreign-incarnation"
        elif fault == "nonterminal":
            cut["pending_writer_terminal"] = False
        elif fault == "observer-crash-gap":
            archive["transitions"] = archive["transitions"][1:]
        else:
            cut["coverage"] = "all-fills-only"
        decision = classify(capsule, archive, cut, current)
        assert decision["disposition"] == "unresolved", (fault, decision)
        outcomes[fault] = dict(archive=archive, cut=cut, classification=decision)
    # Complete command evidence distinguishes historical commit from restored
    # current authority, but cannot authorize replay/repair of that authority.
    restored = classify(capsule, source, closed_cut, capsule["baseline"])
    assert restored["disposition"] == "observed_exact_commit"
    assert restored["current_history"] == "retained_prefix_behind_cut"
    outcomes["authority-only-rollback"] = restored
    # No fills occur in either history. History 1 had cancelled target's seven
    # units before authority restore; history 0 never committed cancellation.
    old_archive = copy.deepcopy(source)
    old_archive["transitions"] = []
    old_cut = _cut(old_archive, capsule["baseline"])
    visible = dict(
        authority=capsule["baseline"],
        witness=case["raw_witness"],
        fill_only_observation=[],
    )
    identical_decisions = [
        classify(capsule, old_archive, old_cut, capsule["baseline"]) for _ in range(2)
    ]
    assert identical_decisions[0] == identical_decisions[1]
    assert identical_decisions[0]["disposition"] == "certified_no_commit"
    outcomes["joint-rollback-impossibility"] = dict(
        same_visible_evidence=visible,
        same_restored_archive=old_archive,
        same_restored_cut=old_cut,
        histories=[
            dict(target_cancel_committed=False, historical_cancelled_quantity=0),
            dict(target_cancel_committed=True, historical_cancelled_quantity=7),
        ],
        identical_conditional_classifications=identical_decisions,
        assumptions_failed_in_second_history="archive independence and cut freshness",
        detection_claim=False,
    )
    outcomes["fill-only-indistinguishability"] = dict(
        same_visible_evidence=visible,
        history_without_cancel_commit=dict(historical_cancelled_quantity=0),
        history_with_cancel_then_authority_restore=dict(
            historical_cancelled_quantity=7
        ),
        independent_complete_fill_stream_in_both=[],
        conclusion="full fill coverage omits cancellation disposition",
    )
    return outcomes


def run(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for label in ("repeat-a", "repeat-b"):
        destination = output / label
        destination.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="lob-pending-evidence-") as temporary:
            cases = []
            for side in (1, -1):
                for committed in (False, True):
                    for prefill in (0, 2):
                        for postfill in (0, 1, 7):
                            name = f"side{side}-committed{committed}-pre{prefill}-post{postfill}"
                            case = run_case(
                                Path(temporary) / name,
                                side,
                                committed,
                                prefill,
                                postfill,
                            )
                            (destination / (name + ".json")).write_bytes(
                                canonical(case)
                            )
                            cases.append(case)
            positive = next(
                c
                for c in cases
                if c["inputs"] == dict(side=1, committed=True, prefill=0, postfill=1)
            )
            (destination / "negative-cases.json").write_bytes(
                canonical(negative_cases(positive))
            )
        artifacts[label] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.glob("*.json"))
        }
    assert artifacts["repeat-a"] == artifacts["repeat-b"]
    result = dict(
        baseline=BASELINE,
        synthetic_uncalibrated=True,
        cases=24,
        repeated_canonical_artifacts=len(artifacts["repeat-a"]),
        canonical_sha256=artifacts["repeat-a"],
        source_sha256={
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in (
                "tools/pending_intent_evidence.py",
                "tools/pending_intent_experiment.py",
                "tools/witness_independent_v4.py",
                "docs/PENDING_INTENT_EVIDENCE_CONTRACT.md",
            )
        },
        entry_recovery_enabled=False,
        pending_recovery_enabled=False,
    )
    (output / "results.json").write_bytes(canonical(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output), sort_keys=True))


if __name__ == "__main__":
    main()
