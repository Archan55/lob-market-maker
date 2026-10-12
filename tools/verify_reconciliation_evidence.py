"""Repeat query evidence and preserve every prior study against exact PR2 head.

Local git archive supplies old source. No network, market service or account.
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

from verify_transport_evidence import LEGACY, compare, run

BASELINE = "ff65d453e53292d48e425107de269a3f5466573d"
TRANSPORT = (
    "transport",
    "tests/integration/test_transport_stress_v1.py",
    "--transport-study-output",
)


def main() -> None:
    root = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/reconciliation-verification-v1"
    )
    root.mkdir(parents=True, exist_ok=True)
    totals = {}
    source = subprocess.check_output(["git", "archive", BASELINE, "src"])
    with tempfile.TemporaryDirectory(prefix="lobmm-no-recovery-baseline-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        for name, suite, option in (*LEGACY, TRANSPORT):
            run(suite, option, root / "before" / name, env)
            run(suite, option, root / "after" / name)
            totals[name] = compare(root / "before" / name, root / "after" / name)
    suite, option = (
        "tests/integration/test_reconciliation_stress_v1.py",
        "--reconciliation-study-output",
    )
    for label in ("repeat-a", "repeat-b"):
        run(suite, option, root / label)
    repeats = compare(root / "repeat-a", root / "repeat-b")
    result = dict(
        baseline=BASELINE,
        preserved_equal_artifacts=totals,
        preserved_equal_total=sum(totals.values()),
        reconciliation_repeat_equal_artifacts=repeats,
        corrections=[],
        note="Identical stable artifacts and audit JSON in one installed environment; wall time excluded.",
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
