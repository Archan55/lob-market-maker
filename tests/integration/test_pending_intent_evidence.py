"""Fixed pending-intent matrix and complete repeated raw evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.pending_intent_experiment import run, run_case


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("prefill", [0, 2])
@pytest.mark.parametrize("postfill", [0, 1, 7])
def test_pending_cancellation_closed_prefix(
    tmp_path: Path, side: int, committed: bool, prefill: int, postfill: int
) -> None:
    case = run_case(tmp_path / "case", side, committed, prefill, postfill)
    decision = case["classification"]
    assert decision["disposition"] == (
        "observed_exact_commit" if committed else "certified_no_commit"
    )
    assert decision["authorizes_recovery"] is False
    assert case["classification_left_both_stores_byte_identical"]
    assert case["cancellation_liveness_improved"] is False
    assert set(case["blocked_operations_after_classification"]) == {
        "snapshot",
        "facade_trade",
        "target_cancel_retry",
        "sibling_cancel",
        "new_entry",
    }
    if postfill:
        assert not case["current_hash_matches_old"]
        assert not case["current_hash_matches_proposed"]


def test_pending_evidence_repeated_artifacts_and_negative_witnesses(
    tmp_path: Path,
) -> None:
    result = run(tmp_path / "study")
    assert result["cases"] == 24 and result["repeated_canonical_artifacts"] == 25
    negatives = json.loads(
        (tmp_path / "study/repeat-a/negative-cases.json").read_text()
    )
    assert (
        negatives["authority-only-rollback"]["disposition"] == "observed_exact_commit"
    )
    joint = negatives["joint-rollback-impossibility"]
    assert (
        joint["identical_conditional_classifications"][0]
        == joint["identical_conditional_classifications"][1]
    )
    assert joint["detection_claim"] is False
