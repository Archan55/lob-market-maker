"""Reproduce the exact unchanged independent regression before and after hardening."""

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

BEFORE = "5ca2c928a7ad0bf9b2baf68051a7ffae7dc2baf4"
TEST = Path("tests/integration/test_authority_witness_independent.py")
PROBE = Path("tools/authority_witness_issuance_probe.py")
SELECTOR = "checksumming_does_not_validate_issuance_semantics"


def main() -> None:
    output = Path(
        sys.argv[1] if len(sys.argv) > 1 else "experiments/authority-witness-review-v4"
    )
    output.mkdir(parents=True, exist_ok=True)
    archived = subprocess.check_output(["git", "archive", BEFORE, "src"])
    cases = []
    with tempfile.TemporaryDirectory(prefix="lobmm-witness-before-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
            archive.extractall(temporary, filter="data")
        for label, env in (
            ("before", dict(os.environ, PYTHONPATH=str(Path(temporary) / "src"))),
            ("after", None),
        ):
            result = subprocess.run(
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
            (output / (label + ".txt")).write_text(result.stdout + result.stderr)
            assert result.returncode == (1 if label == "before" else 0), (
                result.stdout + result.stderr
            )
            assert ("1 failed" if label == "before" else "1 passed") in result.stdout
            probe = json.loads(
                subprocess.check_output([sys.executable, str(PROBE)], env=env)
            )
            assert (
                probe["refused"] == probe["authority_unchanged"] == (label == "after")
            )
            (output / (label + "-probe.json")).write_text(
                json.dumps(probe, sort_keys=True, indent=2) + "\n"
            )
            source = (
                Path(temporary) / "src/lobmm/authority_witness.py"
                if label == "before"
                else Path("src/lobmm/authority_witness.py")
            )
            cases.append(
                dict(
                    label=label,
                    regression_exit=result.returncode,
                    refused=probe["refused"],
                    authority_unchanged=probe["authority_unchanged"],
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    unchanged_test_sha256=hashlib.sha256(TEST.read_bytes()).hexdigest(),
                    unchanged_probe_sha256=hashlib.sha256(
                        PROBE.read_bytes()
                    ).hexdigest(),
                )
            )
    report = dict(
        before=BEFORE,
        defect="Checksummed issuance request contradicted token but still authorized cancellation",
        cases=cases,
        limit="Noncanonical semantic-malformation fixture. Does not establish hostile-tamper or live authentication protection; rejection can leave old orders executable.",
    )
    (output / "results.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n"
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
