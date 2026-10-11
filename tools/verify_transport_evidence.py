"""Offline pinned-head baseline and repeat verification in one environment."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "6ae69bd876b938c6d7af454849b4f0caf4160c35"
LEGACY = (
    (
        "latency",
        "tests/integration/test_latency_stress_v1.py",
        "--latency-study-output",
    ),
    (
        "exposure",
        "tests/integration/test_exposure_session_stress_v1.py",
        "--exposure-study-output",
    ),
    (
        "client-stop",
        "tests/integration/test_client_stop_stress_v1.py",
        "--client-stop-study-output",
    ),
)


def run(
    suite: str, option: str, output: Path, env: dict[str, str] | None = None
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q", option, str(output)],
        capture_output=True,
        text=True,
        env=env,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".txt").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def compare(left: Path, right: Path) -> int:
    before = json.loads((left / "audit.json").read_text())
    after = json.loads((right / "audit.json").read_text())
    assert before == after
    count = 0
    for case in before["cases"]:
        hashes = case.get("hashes", case.get("artifact_sha256"))
        assert hashes
        for filename in hashes:
            assert (left / case["name"] / filename).read_bytes() == (
                right / case["name"] / filename
            ).read_bytes(), (case["name"], filename)
            count += 1
    return count


def main() -> None:
    root = Path(
        sys.argv[1] if len(sys.argv) > 1 else "experiments/transport-verification-v1"
    )
    root.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    source = subprocess.check_output(["git", "archive", BASELINE, "src"])
    with tempfile.TemporaryDirectory(prefix="lobmm-reliable-baseline-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(source)) as archive:
            archive.extractall(temporary, filter="data")
        env = dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))
        for name, suite, option in LEGACY:
            run(suite, option, root / "before" / name, env)
            run(suite, option, root / "after" / name)
            totals[name] = compare(root / "before" / name, root / "after" / name)
    suite, option = (
        "tests/integration/test_transport_stress_v1.py",
        "--transport-study-output",
    )
    for label in ("repeat-a", "repeat-b"):
        run(suite, option, root / label)
    repeats = compare(root / "repeat-a", root / "repeat-b")
    result = dict(
        baseline=BASELINE,
        baseline_equal_artifacts=totals,
        baseline_equal_total=sum(totals.values()),
        transport_repeat_equal_artifacts=repeats,
        corrections=[],
        note="Exact bytes within one installed dependency environment; wall time excluded.",
    )
    (root / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    (root / "requirements-observed.txt").write_bytes(
        subprocess.check_output([sys.executable, "-m", "pip", "freeze"])
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
