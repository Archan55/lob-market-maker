"""Unchanged independent malformed-input regressions against pinned prototype."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROTOTYPE = "f1442de234cf0b92d25b3ef341d655e94ca9653a"
REGRESSION_COMMIT = "292b08a"
SOURCE = "tools/pending_intent_evidence.py"
TEST = "tests/integration/test_pending_intent_independent.py"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/pending-intent-evidence/defect"
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    pinned_test = subprocess.check_output(
        ["git", "show", f"{REGRESSION_COMMIT}:{TEST}"], cwd=root
    )
    assert (root / TEST).read_bytes() == pinned_test, "independent regression changed"
    outcomes = {}
    for label, revision in (("before", PROTOTYPE), ("after", "HEAD")):
        source = (
            subprocess.check_output(["git", "show", f"{revision}:{SOURCE}"], cwd=root)
            if label == "before"
            else (root / SOURCE).read_bytes()
        )
        with tempfile.TemporaryDirectory(prefix="lob-pending-regression-") as temporary:
            sandbox = Path(temporary)
            (sandbox / "tools").mkdir()
            (sandbox / SOURCE).write_bytes(source)
            test = sandbox / "test_pending_intent_independent.py"
            test.write_bytes(pinned_test)
            junit = output / f"{label}.xml"
            environment = dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(
                    [str(sandbox), str(root / "src"), str(root)]
                ),
                PYTHONDONTWRITEBYTECODE="1",
            )
            command = [
                sys.executable,
                "-m",
                "pytest",
                str(test),
                "-q",
                "-k",
                "negative_order_quantity or boolean_trade",
                "--junitxml",
                str(junit),
                "-o",
                f"cache_dir={sandbox / 'cache'}",
            ]
            completed = subprocess.run(
                command, cwd=sandbox, env=environment, capture_output=True, text=True
            )
            (output / f"{label}.log").write_text(completed.stdout + completed.stderr)
            suite = ET.parse(junit).getroot().find("testsuite")
            assert suite is not None
            counts = {
                name: int(suite.attrib[name])
                for name in ("tests", "failures", "errors")
            }
            assert counts == dict(
                tests=3, failures=3 if label == "before" else 0, errors=0
            ), completed.stdout
            assert completed.returncode == (1 if label == "before" else 0)
            (output / f"{label}-classifier.py").write_bytes(source)
            outcomes[label] = dict(
                source_revision=revision,
                source_sha256=hashlib.sha256(source).hexdigest(),
                exit_code=completed.returncode,
                counts=counts,
            )
    result = dict(
        synthetic_uncalibrated=True,
        defect="permissive raw auditor accepted negative quantity and bool trade inputs",
        regression_commit=REGRESSION_COMMIT,
        unchanged_test_sha256=hashlib.sha256(pinned_test).hexdigest(),
        outcomes=outcomes,
        effect="experimental classification only; both versions authorize zero recovery writes",
        production_witness_unchanged=True,
    )
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
