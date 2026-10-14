"""Reproduce five prepublication durable-path review findings before/after.

The initial durable implementation is pinned in repository history. It is not
the previous simulator baseline, and these findings make no claim of newly
discovered defects in that older engine. Only the durable module is substituted;
the independently authored current regression tests are identical in both runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

INITIAL = "56049ce2a816be3f41ff7a8f87c4d0fd6aeba353"
ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/integration/test_durable_recovery_independent.py"
CASES = (
    "test_exact_costs_and_restart_are_independent_of_decimal_context",
    "test_independent_impossible_evidence_fails_before_inbox_publication[false_complete_detail]",
    "test_independent_impossible_evidence_fails_before_inbox_publication[terminal_quantity_contradiction]",
    "test_independent_impossible_evidence_fails_before_inbox_publication[one_intent_two_venue_ids]",
    "test_independent_impossible_evidence_fails_before_inbox_publication[report_status_mismatch]",
)
LOADER = """
import importlib.util
import sys
import lobmm
import pytest

name = 'lobmm.durable_recovery'
spec = importlib.util.spec_from_file_location(name, sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
lobmm.durable_recovery = module
raise SystemExit(pytest.main(sys.argv[2:]))
"""


def _run(output: Path, label: str, source: Path | None) -> dict[str, Any]:
    xml_path = output / f"{label}.xml"
    selections = [f"{TEST}::{case}" for case in CASES]
    pytest_args = ["-q", f"--junitxml={xml_path}", *selections]
    command = (
        [sys.executable, "-m", "pytest", *pytest_args]
        if source is None
        else [sys.executable, "-c", LOADER, str(source), *pytest_args]
    )
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    (output / f"{label}.txt").write_text(completed.stdout + completed.stderr)
    cases = ET.parse(xml_path).getroot().findall(".//testcase")
    failures = [
        case.attrib["name"] for case in cases if case.find("failure") is not None
    ]
    errors = [case.attrib["name"] for case in cases if case.find("error") is not None]
    skipped = [
        case.attrib["name"] for case in cases if case.find("skipped") is not None
    ]
    return dict(
        returncode=completed.returncode,
        cases=len(cases),
        failures=failures,
        errors=errors,
        skipped=skipped,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "experiments/durable-review-v1"
    )
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    initial_source = subprocess.run(
        ["git", "show", f"{INITIAL}:src/lobmm/durable_recovery.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    current_source = (ROOT / "src/lobmm/durable_recovery.py").read_bytes()
    with tempfile.TemporaryDirectory(prefix="lobmm-durable-review-") as temporary:
        source = Path(temporary) / "initial_durable_recovery.py"
        source.write_bytes(initial_source)
        before = _run(output, "before", source)
    after = _run(output, "after", None)
    result = dict(
        version="durable-independent-review-v1",
        scope="five prepublication defects in the newly developed durable path",
        initial_revision=INITIAL,
        initial_source_sha256=hashlib.sha256(initial_source).hexdigest(),
        current_source_sha256=hashlib.sha256(current_source).hexdigest(),
        independent_tests_sha256=hashlib.sha256((ROOT / TEST).read_bytes()).hexdigest(),
        cases=CASES,
        before=before,
        after=after,
    )
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if (
        before["returncode"] != 1
        or before["cases"] != 5
        or set(before["failures"]) != set(CASES)
        or before["errors"]
        or before["skipped"]
        or after["returncode"] != 0
        or after["cases"] != 5
        or after["failures"]
        or after["errors"]
        or after["skipped"]
    ):
        raise SystemExit(
            f"unexpected before/after outcome; inspect {output / 'result.json'}"
        )
    print(
        f"Verified five initial failures and five corrected passes: {output / 'result.json'}"
    )


if __name__ == "__main__":
    main()
