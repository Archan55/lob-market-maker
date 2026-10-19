"""Bounded, read-only experimental classification; never authorizes recovery.

The archive and fresh cut are hypothetical independently retained evidence, not
infrastructure implemented by this tool. See PENDING_INTENT_EVIDENCE_CONTRACT.md.
Complete *command* coverage and a terminal writer are necessary for noncommit.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from tools.witness_independent_v4 import reconstruct

MAX_TRANSITIONS = 64
MAX_BYTES = 4 * 1024 * 1024


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _keys(value: Any, keys: tuple[str, ...]) -> None:
    _require(type(value) is dict and set(value) == set(keys), "evidence fields")


def _number(value: Any) -> None:
    _require(type(value) is int and value >= 0, "evidence sequence")


def _hash(value: Any) -> None:
    _require(
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value),
        "evidence digest",
    )


def _lineage(value: Any) -> None:
    _keys(value, ("authority_id", "witness_id", "incarnation_id"))
    _require(
        all(type(v) is str and bool(v) for v in value.values()), "evidence lineage"
    )


def _append(document: dict[str, Any], account: str, event: dict[str, Any]) -> None:
    _require(
        type(account) is str and account in document["accounts"],
        "unknown transition account",
    )
    document["accounts"][account].append(copy.deepcopy(event))


def _document(value: Any, authority: str) -> None:
    _keys(value, ("version", "authority_id", "accounts"))
    _require(
        value["version"] == "simulated-account-authority-v3"
        and value["authority_id"] == authority
        and type(value["accounts"]) is dict
        and 0 < len(value["accounts"]) <= 8,
        "authority document identity",
    )
    _require(
        all(type(v) is list and len(v) <= 1024 for v in value["accounts"].values()),
        "authority event bound",
    )
    # This is the prior independently authored integer raw-input auditor, not
    # the venue's _fold/_apply/snapshot. Scope remains its client-ID cancel subset.
    reconstruct(value)


def _prefix(prefix: dict[str, Any], document: dict[str, Any]) -> bool:
    return all(
        account in document["accounts"]
        and document["accounts"][account][: len(events)] == events
        for account, events in prefix["accounts"].items()
    )


def classify(
    capsule: Any,
    archive: Any,
    cut: Any,
    current_authority: Any,
) -> dict[str, Any]:
    """Classify only under externally supplied retention/completeness assumptions.

    The cut must come from an independently trusted fresh query, not from the
    archive being checked. A jointly restored archive AND cut is undetectable.
    No file handle, API object, write or command-dispatch capability is accepted.
    """
    result: dict[str, Any] = {
        "disposition": "unresolved",
        "reason": "invalid evidence",
        "current_history": "unassessed",
        "authorizes_recovery": False,
        "entry_recovery_enabled": False,
        "conditional_on_independent_complete_fresh_source": True,
    }
    try:
        _require(
            len(canonical([capsule, archive, cut, current_authority])) <= MAX_BYTES,
            "evidence byte bound",
        )
        _keys(
            capsule,
            (
                "lineage",
                "watermark",
                "old_digest",
                "proposed_digest",
                "baseline",
                "account_id",
                "event",
            ),
        )
        _lineage(capsule["lineage"])
        _number(capsule["watermark"])
        _require(capsule["watermark"] > 0, "pending watermark")
        for field in ("old_digest", "proposed_digest"):
            _hash(capsule[field])
        base = copy.deepcopy(capsule["baseline"])
        _document(base, capsule["lineage"]["authority_id"])
        _require(digest(base) == capsule["old_digest"], "unanchored baseline")
        event = capsule["event"]
        _require(
            type(event) is dict
            and event.get("kind") == "command"
            and event.get("request", {}).get("kind") == "cancel",
            "capsule is not cancellation",
        )
        proposed = copy.deepcopy(base)
        _append(proposed, capsule["account_id"], event)
        _document(proposed, capsule["lineage"]["authority_id"])
        _require(
            digest(proposed) == capsule["proposed_digest"], "candidate hash mismatch"
        )
        _keys(
            archive,
            ("source_id", "lineage", "baseline_digest", "transitions"),
        )
        _keys(
            cut,
            (
                "source_id",
                "lineage",
                "query_id",
                "sequence",
                "head_digest",
                "coverage",
                "pending_writer_terminal",
            ),
        )
        _lineage(archive["lineage"])
        _lineage(cut["lineage"])
        _require(
            capsule["lineage"] == archive["lineage"] == cut["lineage"],
            "lineage mismatch",
        )
        _require(
            type(cut["source_id"]) is str
            and bool(cut["source_id"])
            and archive["source_id"] == cut["source_id"]
            and type(cut["query_id"]) is str
            and bool(cut["query_id"]),
            "source/query identity",
        )
        _require(
            cut["coverage"] == "all-authority-transitions"
            and cut["pending_writer_terminal"] is True,
            "no complete terminal command cut",
        )
        _require(
            archive["baseline_digest"] == capsule["old_digest"],
            "archive baseline mismatch",
        )
        _number(cut["sequence"])
        _hash(cut["head_digest"])
        transitions = archive["transitions"]
        _require(
            type(transitions) is list
            and len(transitions) <= MAX_TRANSITIONS
            and len(transitions) == cut["sequence"],
            "archive incomplete or stale cut",
        )
        history = copy.deepcopy(base)
        hashes = [digest(history)]
        observed = None
        for sequence, transition in enumerate(transitions, 1):
            _keys(
                transition,
                ("sequence", "account_id", "event", "before_digest", "after_digest"),
            )
            _number(transition["sequence"])
            _require(transition["sequence"] == sequence, "archive sequence gap")
            _require(transition["before_digest"] == hashes[-1], "broken prefix")
            _append(history, transition["account_id"], transition["event"])
            _document(history, capsule["lineage"]["authority_id"])
            hashes.append(digest(history))
            _require(transition["after_digest"] == hashes[-1], "contradictory effect")
            if (
                transition["before_digest"] == capsule["old_digest"]
                and transition["after_digest"] == capsule["proposed_digest"]
                and transition["account_id"] == capsule["account_id"]
                and transition["event"] == event
            ):
                observed = sequence
            elif (
                transition["event"].get("kind") == "command"
                and transition["account_id"] == capsule["account_id"]
                and transition["event"].get("command_id") == event["command_id"]
            ):
                raise ValueError("candidate command occurred at a different prefix")
        _require(hashes[-1] == cut["head_digest"], "archive differs from fresh cut")
        _document(current_authority, capsule["lineage"]["authority_id"])
        current = digest(current_authority)
        _require(
            observed is not None or not _prefix(proposed, current_authority),
            "current history contradicts claimed command absence",
        )
        result.update(
            disposition=(
                "observed_exact_commit"
                if observed is not None
                else "certified_no_commit"
            ),
            reason="conditional complete terminal prefix verified",
            observed_transition=observed,
            cut_sequence=cut["sequence"],
            current_history=(
                "at_observed_cut"
                if current == hashes[-1]
                else "retained_prefix_behind_cut"
                if current in hashes
                else "descendant_after_observed_cut"
                if _prefix(history, current_authority)
                else "inconsistent_with_complete_archive"
            ),
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        AssertionError,
        RecursionError,
    ) as exc:
        result["reason"] = str(exc) or type(exc).__name__
    return result
