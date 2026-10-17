"""Repeat active restart evidence and preserve the immediately prior durable path."""

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

from verify_durable_recovery import compare_durable, run

BASELINE = "ad108ebaad2edca9b2997dab95800080d5fb6802"


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/active-restart-verification-v1"
    )
    output.mkdir(parents=True, exist_ok=True)
    for label in ("repeat-a", "repeat-b"):
        run(
            "tests/integration/test_active_restart_v1.py",
            "--active-restart-output",
            output / label,
        )
    left, right = output / "repeat-a", output / "repeat-b"
    audit = json.loads((left / "audit.json").read_text())
    assert audit == json.loads((right / "audit.json").read_text())
    for name, digest in audit["artifact_sha256"].items():
        content = (left / name).read_bytes()
        assert content == (right / name).read_bytes(), name
        assert hashlib.sha256(content).hexdigest() == digest, name
    published = Path("docs/ACTIVE_RESTART_V1.json")
    if published.exists():
        assert published.read_bytes() == (left / "audit.json").read_bytes()
    archive_bytes = subprocess.check_output(["git", "archive", BASELINE, "src", "docs"])
    with tempfile.TemporaryDirectory(prefix="lobmm-active-preservation-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        run(
            "tests/integration/test_durable_recovery_v1.py",
            "--durable-study-output",
            output / "prior-durable",
            env,
        )
        run(
            "tests/integration/test_durable_recovery_v1.py",
            "--durable-study-output",
            output / "current-durable",
        )
        count, _ = compare_durable(output / "prior-durable", output / "current-durable")
        documents = [p for p in (Path(temporary) / "docs").rglob("*") if p.is_file()]
        for path in documents:
            assert (
                path.read_bytes() == Path(path.relative_to(temporary)).read_bytes()
            ), str(path)
    result = dict(
        baseline=BASELINE,
        active_equal_artifacts=len(audit["artifact_sha256"]),
        prior_durable_equal_artifacts=count,
        preserved_documents=len(documents),
        process_death_cases=len(audit["process_death_cases"]),
        risk_cases=len(audit["risk_cases"]),
        account_cases=len(audit["account_cases"]),
        account_prefixes=sum(r["prefixes"] for r in audit["account_cases"]),
        risk_prefixes=sum(r["prefixes"] for r in audit["risk_cases"]),
        published_audit_sha256=hashlib.sha256(
            (left / "audit.json").read_bytes()
        ).hexdigest(),
    )
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    (output / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
