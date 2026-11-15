"""One disclosed repeat of the six exposed component-integration cases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_exact_portfolio_to_risk_component_composition(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    output = Path(
        os.environ.get(
            "LOB_COMPONENT_INTEGRATION_EVIDENCE_DIR",
            str(tmp_path / "component-integration"),
        )
    ).resolve()
    command = [
        sys.executable,
        "-I",
        "-B",
        str(root / "tools/check_component_integration.py"),
        "--c1-source",
        str(root),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command, cwd=root, text=True, capture_output=True, timeout=920, check=False
    )
    # Retain both streams before assertions, including partial failed attempts.
    for label, text in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        log = output.with_name(output.name + f"-test-controller.{label}.log")
        with log.open("x") as stream:
            stream.write(text)
    assert (output / "report.json").is_file(), completed.stderr
    report = json.loads((output / "report.json").read_text())
    assert len(report["observations"]) == 6
    assert completed.returncode == 0, json.dumps(report["checks"], indent=2)
    assert report["criterion"], report["checks"]
