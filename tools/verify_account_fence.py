"""Repeat canonical v3 evidence and preserve exact preceding PR2 source/evidence.

Actual free-running races are retained as JSONL and checked semantically by the
independent suite. Their OS-selected chronology is explicitly excluded from the
canonical JSON byte comparison. Controlled contested-transaction witnesses retain
full chronological events and repeat byte for byte.
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

from verify_outbound_recovery import compare, manifest, run

BASELINE = "3d261672ec4dc8d55e2c01f59c283e4f85de4bb2"
SUITES = (
    "tests/integration/test_account_fence_v3.py",
    "tests/integration/test_account_fence_independent.py",
)
PRIOR = (
    "tests/integration/test_outbound_recovery_v2.py",
    "tests/integration/test_outbound_recovery_independent.py",
)


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/account-fence-verification-v3"
    )
    output.mkdir(parents=True, exist_ok=True)
    for label in ("repeat-a", "repeat-b"):
        run(output / label, SUITES, "--account-fence-output")
    repeated = compare(output / "repeat-a", output / "repeat-b")
    audit_primary = json.loads((output / "repeat-a/primary/audit.json").read_text())
    audit_independent = json.loads(
        (output / "repeat-a/independent/audit.json").read_text()
    )
    source = subprocess.check_output(["git", "archive", BASELINE, "src", "docs"])
    with tempfile.TemporaryDirectory(prefix="lobmm-fence-preservation-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        environment = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        run(output / "prior-source", PRIOR, "--outbound-recovery-output", environment)
        run(output / "current-source", PRIOR, "--outbound-recovery-output")
        prior_equal = compare(output / "prior-source", output / "current-source")
        documents = [p for p in (Path(temporary) / "docs").rglob("*") if p.is_file()]
        for path in documents:
            assert (
                path.read_bytes() == Path(path.relative_to(temporary)).read_bytes()
            ), str(path)
    result = dict(
        version="account-fence-v3-verification",
        baseline=BASELINE,
        synthetic_uncalibrated=True,
        canonical_equal_json_artifacts=repeated,
        prior_equal_json_artifacts=prior_equal,
        prior_documents_unchanged=len(documents),
        primary_scenarios=audit_primary["scenarios"],
        independent_witnesses=audit_independent["case_count"],
        real_process_deaths=audit_primary["process_deaths"]
        + audit_independent["process_deaths"],
        canonical_manifest=manifest(output / "repeat-a"),
        excluded_from_byte_comparison="independent/runtime-races.jsonl: raw actual inputs, replies and ordered snapshots of free-running process races; scheduler dependent. Controlled transaction races are canonical JSON.",
        limits="One retained modeled authority/exact account; transferable trusted tokens, zero starting balances and zero costs. No process exclusivity, exposure cap, live safety, exactly-once, namespace federation or anti-rollback guarantee.",
    )
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    published = Path("docs/ACCOUNT_FENCE_V3.json")
    if published.exists():
        assert published.read_bytes() == (output / "results.json").read_bytes()
    (output / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "canonical_manifest"},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
