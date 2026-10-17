"""Preserve every prior version against pinned PR2 source; repeat new evidence."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from verify_reconciliation_evidence import TRANSPORT
from verify_transport_evidence import LEGACY, compare, run

BASELINE = "3da58b38b49108cc116fa4910d77da689a1ab922"
RECONCILIATION = (
    "reconciliation",
    "tests/integration/test_reconciliation_stress_v1.py",
    "--reconciliation-study-output",
)


def main() -> None:
    root = Path(
        sys.argv[1] if len(sys.argv) > 1 else "experiments/reordering-verification-v1"
    )
    root.mkdir(parents=True, exist_ok=True)
    totals = {}
    source = subprocess.check_output(["git", "archive", BASELINE, "src"])
    with tempfile.TemporaryDirectory(prefix="lobmm-pr2-reorder-baseline-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        for name, suite, option in (*LEGACY, TRANSPORT, RECONCILIATION):
            run(suite, option, root / "before" / name, env)
            run(suite, option, root / "after" / name)
            totals[name] = compare(root / "before" / name, root / "after" / name)
    for label in ("repeat-a", "repeat-b"):
        run(
            "tests/integration/test_reordering_stress_v1.py",
            "--reordering-study-output",
            root / label,
        )
    repeats = compare(root / "repeat-a", root / "repeat-b")
    document = Path("docs/REORDERING_STRESS_V1.json")
    if document.exists():
        assert document.read_bytes() == (root / "repeat-a" / "audit.json").read_bytes()
    result = dict(
        baseline=BASELINE,
        preserved_equal_artifacts=totals,
        preserved_equal_total=sum(totals.values()),
        reordering_repeat_equal_artifacts=repeats,
        corrections=[],
        note="Exact stable artifacts and audit JSON in one dependency environment; wall time excluded.",
    )
    (root / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    (root / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
