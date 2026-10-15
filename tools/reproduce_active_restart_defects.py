"""Pinned before/after witnesses for three newly developed boundary defects.

These prepublication implementation defects do not reinterpret the older
explicitly offline-only DurableClient as an active restart feature.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/integration/test_active_restart_independent.py"
GROUPS = (
    (
        "dispatch-evidence-causality",
        "c056a05175f20d6bc1aafa385d593570e92cb1fb",
        ("test_evidence_before_actual_dispatch_fails_closed",),
    ),
    (
        "valid-command-wire",
        "79e8ef272625edafc37dac2eb1557aa93b309a94",
        ("test_reopen_blocks_cached_and_external_commands_before_strategy_mutation",),
    ),
    (
        "strategy-authority",
        "b8e15a125edcbf209ec3d5b192501b3ca1cfadea",
        (
            "test_wrong_strategy_command_cannot_use_bound_session_authority",
            "test_recomputed_checksum_cannot_rebind_persisted_command_strategy",
        ),
    ),
)
LOADER = """
import importlib.util, sys, lobmm, pytest
name = 'lobmm.active_restart'
spec = importlib.util.spec_from_file_location(name, sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
lobmm.active_restart = module
raise SystemExit(pytest.main(sys.argv[2:]))
"""


def run(
    root: Path, label: str, cases: tuple[str, ...], source: Path | None
) -> dict[str, Any]:
    xml = root / f"{label}.xml"
    args = ["-q", f"--junitxml={xml}", *[f"{TEST}::{c}" for c in cases]]
    command = (
        [sys.executable, "-m", "pytest", *args]
        if source is None
        else [sys.executable, "-c", LOADER, str(source), *args]
    )
    done = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    (root / f"{label}.txt").write_text(done.stdout + done.stderr)
    nodes = ET.parse(xml).getroot().findall(".//testcase")
    return dict(
        returncode=done.returncode,
        cases=len(nodes),
        failures=[n.attrib["name"] for n in nodes if n.find("failure") is not None],
        errors=[n.attrib["name"] for n in nodes if n.find("error") is not None],
        skipped=[n.attrib["name"] for n in nodes if n.find("skipped") is not None],
    )


def main() -> None:
    output = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "experiments/active-review-reproduction-v1"
    ).resolve()
    output.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for label, revision, cases in GROUPS:
        initial = subprocess.check_output(
            ["git", "show", f"{revision}:src/lobmm/active_restart.py"], cwd=ROOT
        )
        with tempfile.TemporaryDirectory(prefix="lobmm-active-review-") as temporary:
            source = Path(temporary) / "active_restart.py"
            source.write_bytes(initial)
            before = run(output, label + "-before", cases, source)
        after = run(output, label + "-after", cases, None)
        row = dict(
            defect=label,
            baseline=revision,
            cases=cases,
            baseline_source_sha256=hashlib.sha256(initial).hexdigest(),
            before=before,
            after=after,
        )
        outcomes.append(row)
        assert before == dict(
            returncode=1, cases=len(cases), failures=list(cases), errors=[], skipped=[]
        ), row
        assert after == dict(
            returncode=0, cases=len(cases), failures=[], errors=[], skipped=[]
        ), row
    result = dict(
        version="active-restart-review-v1",
        findings=outcomes,
        independent_test_sha256=hashlib.sha256((ROOT / TEST).read_bytes()).hexdigest(),
        current_source_sha256=hashlib.sha256(
            (ROOT / "src/lobmm/active_restart.py").read_bytes()
        ).hexdigest(),
    )
    (output / "result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    print(
        "Verified three new-path defects: four pinned failures / four corrected passes"
    )


if __name__ == "__main__":
    main()
