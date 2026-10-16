"""Identical independent tests against pinned initial v2 source and final source.

Archived commits are on PR2's history. No checkout or older evidence is modified.
Both runs use the exact same test bytes and installed dependencies.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

GROUPS = (
    (
        "callback-and-duplicate",
        "c46febf6dcb4290d7c4faa4a0c1b3d01526eb189",
        "reentrant_access_poisons_outer_operation_even_if_callback_swallows_error or duplicate_receipt_with_impossible_notification_is_invalid_evidence",
        4,
    ),
    (
        "terminal-contradictions",
        "e68f1931bd9ef93c83ae4f50a996bb3408bafb4d",
        "observed_live_state_after_terminal_is_a_conflicting_effect or terminal_status_kind_cannot_change_with_same_execution_quantity",
        6,
    ),
)
TEST = "tests/integration/test_outbound_recovery_independent.py"
SOURCE = "src/lobmm/outbound_recovery.py"


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/outbound-review-reproduction-v2"
    )
    output.mkdir(parents=True, exist_ok=True)
    groups = []
    for group, revision, selector, count in GROUPS:
        old = subprocess.check_output(["git", "archive", revision, "src"])
        records = []
        with tempfile.TemporaryDirectory(prefix="lobmm-outbound-review-") as temporary:
            with tarfile.open(fileobj=io.BytesIO(old)) as archive:
                archive.extractall(temporary, filter="data")
            for label, env, code, expected in (
                (
                    "before",
                    dict(os.environ, PYTHONPATH=str(Path(temporary) / "src")),
                    1,
                    f"{count} failed",
                ),
                ("after", None, 0, f"{count} passed"),
            ):
                name = group + "-" + label
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        TEST,
                        "-q",
                        "-k",
                        selector,
                        "--basetemp",
                        str(output / (name + "-tmp")),
                        "--junitxml",
                        str(output / (name + ".xml")),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                (output / (name + ".txt")).write_text(result.stdout + result.stderr)
                assert result.returncode == code and expected in result.stdout, (
                    result.stdout + result.stderr
                )
                source = Path(temporary) / SOURCE if label == "before" else Path(SOURCE)
                records.append(
                    dict(
                        label=label,
                        returncode=code,
                        expected=expected,
                        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        test_sha256=hashlib.sha256(Path(TEST).read_bytes()).hexdigest(),
                    )
                )
        groups.append(dict(group=group, before_commit=revision, cases=records))
    summary = dict(
        groups=groups,
        identical_test_sha256=hashlib.sha256(Path(TEST).read_bytes()).hexdigest(),
        defects=[
            "swallowed reentrant callback cleared poison",
            "duplicate shortcut acknowledged impossible notification",
            "state evidence contradicted immutable terminal identity",
        ],
        before_failures=10,
        after_passes=10,
    )
    (output / "results.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n"
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
