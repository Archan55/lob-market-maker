"""Repeat v2 real deaths and preserve exact prior quarantine source/evidence.

Pinned local git archives only; no market data, account or network is needed.
Use a fresh output directory. Random orphan staging names, locks and test timing
are excluded; all saved JSON journals, venue traces and prefix witnesses compare.
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

BASELINE = "e5332720fe301285044781ab7020d223a0958b24"
SUITES = (
    "tests/integration/test_outbound_recovery_independent.py",
    "tests/integration/test_outbound_recovery_v2.py",
)


def run(
    output: Path,
    suites: tuple[str, ...],
    option: str,
    env: dict[str, str] | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *suites,
            "-q",
            "--basetemp",
            str(output.parent / (output.name + "-tmp")),
            option,
            str(output),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    output.with_suffix(".txt").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def manifest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.json"))
    }


def compare(left: Path, right: Path) -> int:
    a, b = manifest(left), manifest(right)
    assert a and a == b, (
        set(a.items()) - set(b.items()),
        set(b.items()) - set(a.items()),
    )
    for name in a:
        assert (left / name).read_bytes() == (right / name).read_bytes(), name
    return len(a)


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/outbound-recovery-verification-v2"
    )
    output.mkdir(parents=True, exist_ok=True)
    for label in ("repeat-a", "repeat-b"):
        run(output / label, SUITES, "--outbound-recovery-output")
    repeated = compare(output / "repeat-a", output / "repeat-b")
    audit = json.loads((output / "repeat-a/audit.json").read_text())
    published = Path("docs/OUTBOUND_RECOVERY_V2.json")
    if published.exists():
        assert published.read_bytes() == (output / "repeat-a/audit.json").read_bytes()
    source = subprocess.check_output(["git", "archive", BASELINE, "src", "docs"])
    with tempfile.TemporaryDirectory(prefix="lobmm-outbound-preservation-") as temp:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temp, filter="data")
        environment = dict(os.environ, PYTHONPATH=str(Path(temp) / "src"))
        prior = ("tests/integration/test_active_restart_v1.py",)
        run(output / "old-active", prior, "--active-restart-output", environment)
        run(output / "new-active", prior, "--active-restart-output")
        preserved = compare(output / "old-active", output / "new-active")
        documents = [p for p in (Path(temp) / "docs").rglob("*") if p.is_file()]
        for path in documents:
            assert path.read_bytes() == Path(path.relative_to(temp)).read_bytes(), str(
                path
            )
    result = dict(
        baseline=BASELINE,
        repeat_equal_json_artifacts=repeated,
        prior_active_equal_json_artifacts=preserved,
        prior_documents_unchanged=len(documents),
        primary_scenarios=audit["case_count"],
        primary_real_process_deaths=audit["process_deaths"],
        primary_audit_sha256=hashlib.sha256(
            (output / "repeat-a/audit.json").read_bytes()
        ).hexdigest(),
        deterministic_manifest=manifest(output / "repeat-a"),
        note="Exact stable bytes in one dependency environment; SIGKILL is not power loss. Independent artifacts included; timings, random staging names and lock files excluded.",
    )
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    (output / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "deterministic_manifest"},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
