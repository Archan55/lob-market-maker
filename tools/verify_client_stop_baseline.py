"""Verify preserved baseline artifacts and reproduce unknown-cancel knowledge.

Extracts pinned source into a temporary directory; never changes checkout or
downloads data. Both revisions run in the same installed dependency environment.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "88e9b720de354a83cb7bf8f8537ee6b2c539a29e"


def main() -> None:
    root = Path(
        sys.argv[1] if len(sys.argv) > 1 else "experiments/client-stop-baseline-v1"
    )
    root.mkdir(parents=True, exist_ok=True)
    source = subprocess.check_output(["git", "archive", BASELINE, "src"])
    with tempfile.TemporaryDirectory(prefix="lobmm-client-stop-baseline-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        for label, suite, expected in (
            ("knowledge-before", "tests/unit/test_unknown_cancel_knowledge.py", 1),
            (
                "forced-before",
                "tests/integration/test_exposure_session_stress_v1.py",
                0,
            ),
        ):
            arguments = [sys.executable, "-m", "pytest", suite, "-q"]
            if label == "forced-before":
                arguments.extend(["--exposure-study-output", str(root / "before")])
            result = subprocess.run(arguments, env=env, capture_output=True, text=True)
            (root / f"{label}.txt").write_text(result.stdout + result.stderr)
            assert result.returncode == expected, result.stdout + result.stderr
            if label == "knowledge-before":
                assert "3 failed" in result.stdout
    for label, suite in (
        ("knowledge-after", "tests/unit/test_unknown_cancel_knowledge.py"),
        ("forced-after", "tests/integration/test_exposure_session_stress_v1.py"),
    ):
        arguments = [sys.executable, "-m", "pytest", suite, "-q"]
        if label == "forced-after":
            arguments.extend(["--exposure-study-output", str(root / "after")])
        result = subprocess.run(arguments, capture_output=True, text=True)
        (root / f"{label}.txt").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
    before = json.loads((root / "before" / "audit.json").read_text())
    after = json.loads((root / "after" / "audit.json").read_text())
    count = sum(len(case["artifact_sha256"]) for case in before["cases"])
    corrections = []
    for case in before["cases"]:
        for filename in case["artifact_sha256"]:
            left = (root / "before" / case["name"] / filename).read_bytes()
            right = (root / "after" / case["name"] / filename).read_bytes()
            if left == right:
                continue
            assert (case["name"], filename) == (
                "session-pending",
                "deterministic_diagnostics.json",
            )
            old, new = json.loads(left), json.loads(right)
            differences = {
                key: [old[key], new[key]] for key in old if old[key] != new[key]
            }
            assert differences == {
                "exchange_rejected_quote_attempts": [0, 2],
                "rejected_quote_attempts": [0, 2],
            }, differences
            corrections.append(
                dict(case=case["name"], artifact=filename, differences=differences)
            )
    assert len(corrections) == 1
    for old_case, new_case in zip(before["cases"], after["cases"], strict=True):
        assert {
            key: value for key, value in old_case.items() if key != "artifact_sha256"
        } == {key: value for key, value in new_case.items() if key != "artifact_sha256"}
    summary = dict(
        baseline=BASELINE,
        forced_expiry_cases=before["case_count"],
        equal_artifacts=count - len(corrections),
        versioned_diagnostic_corrections=corrections,
        knowledge_before_failed=3,
        knowledge_after_passed=3,
    )
    (root / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
