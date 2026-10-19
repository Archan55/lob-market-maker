"""Repeat witnessed-authority evidence and preserve exact pre-witness PR2 files."""

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

BASELINE = "91675f77738dda6940072cdaba565c455ae2f274"
SUITES = (
    "tests/integration/test_authority_witness_v4.py",
    "tests/integration/test_authority_witness_independent.py",
)
PRIOR = (
    "tests/integration/test_account_fence_v3.py",
    "tests/integration/test_account_fence_independent.py",
)


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/authority-witness-verification-v4"
    )
    output.mkdir(parents=True, exist_ok=True)
    for label in ("repeat-a", "repeat-b"):
        run(output / label, SUITES, "--authority-witness-output")
    repeated = compare(output / "repeat-a", output / "repeat-b")
    archive_bytes = subprocess.check_output(["git", "archive", BASELINE, "src", "docs"])
    unchanged = {}
    with tempfile.TemporaryDirectory(prefix="lobmm-witness-preservation-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            archive.extractall(temporary, filter="data")
        for directory in ("src", "docs"):
            paths = sorted((Path(temporary) / directory).rglob("*"))
            files = [path for path in paths if path.is_file()]
            for path in files:
                assert (
                    path.read_bytes() == Path(path.relative_to(temporary)).read_bytes()
                ), str(path)
            unchanged[directory] = len(files)
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        run(output / "prior-source", PRIOR, "--account-fence-output", env)
        run(output / "current-source", PRIOR, "--account-fence-output")
        preserved = compare(output / "prior-source", output / "current-source")
    result = dict(
        version="authority-witness-v4-verification",
        baseline=BASELINE,
        synthetic_uncalibrated=True,
        entry_enabled=False,
        repeated_json_artifacts=repeated,
        preserved_v3_json_artifacts=preserved,
        unchanged_prior_files=unchanged,
        canonical_manifest=manifest(output / "repeat-a"),
        runtime_chronology="Free-running race JSONL retained and checked semantically; OS chronology excluded from byte comparison.",
        limits="Conditional independently retained witness; pending intent freezes cancellation. Copied current tokens transferable. Joint matching-prefix rollback and identical namespace clones are undetectable. No entry recovery or live claim.",
    )
    raw = json.dumps(result, sort_keys=True, indent=2) + "\n"
    (output / "results.json").write_text(raw)
    published = Path("docs/AUTHORITY_WITNESS_V4.json")
    if published.exists():
        assert published.read_text() == raw
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "canonical_manifest"},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
