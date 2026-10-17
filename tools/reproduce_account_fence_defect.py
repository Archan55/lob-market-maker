"""Pin initial source, unchanged independent witness, and practical before/after."""

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

BEFORE = "00d22c496b536ad52f8db0f0a87c5f09120d79bc"
WITNESS = Path("tools/account_fence_identity_witness.py")
TEST = Path("tests/integration/test_account_fence_independent.py")
SELECTOR = "adapter_rejects_genuine_foreign_terminal_and_mutated_report"


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/account-fence-review-reproduction-v3"
    )
    output.mkdir(parents=True, exist_ok=True)
    archive_bytes = subprocess.check_output(["git", "archive", BEFORE, "src"])
    records = []
    with tempfile.TemporaryDirectory(prefix="lobmm-fence-before-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            archive.extractall(temporary, filter="data")
        for label, env in (
            ("before", dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))),
            ("after", None),
        ):
            raw = subprocess.check_output([sys.executable, str(WITNESS)], env=env)
            value = json.loads(raw)
            (output / (label + "-witness.json")).write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n"
            )
            expected = 7 if label == "before" else 0
            assert value["actual_inventory"] == expected
            assert value["actual_cash_ticks"] == -113 * expected
            assert value["foreign_source_rejected"] == (label == "after")
            assert value["terminal_after_foreign"] == (label == "before")
            assert value["own_cancel_attempted"] == (label == "after")
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(TEST),
                    "-q",
                    "-k",
                    SELECTOR,
                    "--junitxml",
                    str(output / (label + ".xml")),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            (output / (label + ".txt")).write_text(run.stdout + run.stderr)
            assert run.returncode == (1 if label == "before" else 0), (
                run.stdout + run.stderr
            )
            assert ("1 failed" if label == "before" else "1 passed") in run.stdout
            source = (
                Path(temporary) / "src/lobmm/account_fence.py"
                if label == "before"
                else Path("src/lobmm/account_fence.py")
            )
            records.append(
                dict(
                    label=label,
                    actual_inventory=expected,
                    actual_cash_ticks=-113 * expected,
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    identical_witness_sha256=hashlib.sha256(
                        WITNESS.read_bytes()
                    ).hexdigest(),
                    identical_test_sha256=hashlib.sha256(TEST.read_bytes()).hexdigest(),
                    regression_exit_code=run.returncode,
                )
            )
    result = dict(
        before_commit=BEFORE,
        defect="Foreign account terminal suppressed own cancellation after acceptance receipt loss",
        cases=records,
        note="Same witness executable uses initial documented bare-conversion flow only when source-bound capture is absent; no expectation is weakened. Independent scalar oracle checks seven avoidable fills before and zero after.",
    )
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
