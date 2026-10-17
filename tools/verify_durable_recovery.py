"""Produce reproducible disk recovery evidence without modifying old artifacts."""

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
from typing import Any

from verify_reconciliation_evidence import TRANSPORT
from verify_reordering_evidence import RECONCILIATION
from verify_transport_evidence import LEGACY, compare

BASELINE = "8391031107b0f18c4c0176099f9098d4195ed88b"
REORDERING = (
    "reordering",
    "tests/integration/test_reordering_stress_v1.py",
    "--reordering-study-output",
)


def run(
    suite: str, option: str, output: Path, env: dict[str, str] | None = None
) -> None:
    # Distinct basetemps protect active durable files from concurrent pytest
    # retention cleanup in other preservation jobs using the shared /tmp root.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            suite,
            "-q",
            "--basetemp",
            str(output.parent / (output.name + "-pytest-tmp")),
            option,
            str(output),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".txt").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def compare_durable(left: Path, right: Path) -> tuple[int, dict[str, Any]]:
    before = json.loads((left / "audit.json").read_text())
    assert before == json.loads((right / "audit.json").read_text())
    for name, digest in before["artifact_sha256"].items():
        saved = (left / name).read_bytes()
        assert saved == (right / name).read_bytes(), name
        assert hashlib.sha256(saved).hexdigest() == digest, name
    return len(before["artifact_sha256"]), before


def main() -> None:
    destination = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/durable-recovery-verification-v1"
    )
    destination.mkdir(parents=True, exist_ok=True)
    totals = {}
    source = subprocess.check_output(["git", "archive", BASELINE, "src", "docs"])
    with tempfile.TemporaryDirectory(prefix="lobmm-pr2-durable-baseline-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        for name, suite, option in (*LEGACY, TRANSPORT, RECONCILIATION, REORDERING):
            run(suite, option, destination / "before" / name, env)
            run(suite, option, destination / "after" / name)
            totals[name] = compare(
                destination / "before" / name, destination / "after" / name
            )
        documents = list((Path(temporary) / "docs").rglob("*"))
        previous_documents = [path for path in documents if path.is_file()]
        for path in previous_documents:
            assert (
                path.read_bytes() == Path(path.relative_to(temporary)).read_bytes()
            ), str(path)
    for label in ("repeat-a", "repeat-b"):
        run(
            "tests/integration/test_durable_recovery_v1.py",
            "--durable-study-output",
            destination / label,
        )
    repeats, audit = compare_durable(destination / "repeat-a", destination / "repeat-b")
    published = Path("docs/DURABLE_RECOVERY_V1.json")
    if published.exists():
        assert (
            published.read_bytes()
            == (destination / "repeat-a" / "audit.json").read_bytes()
        )
    result = dict(
        baseline=BASELINE,
        preserved_equal_artifacts=totals,
        preserved_equal_total=sum(totals.values()),
        prior_documents_equal=len(previous_documents),
        durable_repeat_equal_artifacts=repeats,
        full_loop_cases=len(audit["full_loop_cases"]),
        prefixes=sum(row["prefixes"] for row in audit["full_loop_cases"]),
        process_death_cases=len(audit["process_death_cases"]),
        corrections=[],
        note="Exact stable bytes in one installed dependency environment; real SIGKILL process death, not power loss.",
    )
    (destination / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    (destination / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
