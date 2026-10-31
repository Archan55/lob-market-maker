"""Retain the same independent regressions against pinned and corrected source."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
TESTS = {
    "tests/unit/test_portfolio_exact_independent.py": "fea37b29e4e6510bc363eb023cf1dd16507b31f5e4093c631e6cc1d9fd44bbcb",
    "tests/integration/test_exact_portfolio_backtest.py": "c146b47b6b7c68643d9b733cb90776995a5b35dfc5aadebc62125bdebcfd6b17",
    "tests/unit/test_portfolio_presentation_guard_independent.py": "809cb6917a3ac5a1bc4b2d8d60a83dfabe75430e7ff2e9e3388bdb127b0487a6",
}

# Import identity is recorded in the very process that executes the tests.
RUNNER = """
import hashlib, importlib, json, pathlib, sys
import pytest
root, output, label = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
sources = {}
for name in ('portfolio', 'exchange'):
    module = importlib.import_module('lobmm.' + name)
    path = pathlib.Path(module.__file__).resolve()
    assert path == (root / 'src/lobmm' / (name + '.py')).resolve(), str(path)
    sources[name + '.py'] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
(output / (label + '-imports.json')).write_text(json.dumps(sources, indent=2) + '\\n')
raise SystemExit(pytest.main(sys.argv[4:]))
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(root: Path, output: Path, label: str) -> dict[str, object]:
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    command = [
        sys.executable,
        "-c",
        RUNNER,
        str(root),
        str(output),
        label,
        *TESTS,
        "-q",
        "--tb=short",
        f"--junitxml={output / (label + '.xml')}",
    ]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    (output / (label + ".log")).write_text(result.stdout + result.stderr)
    xml = ET.parse(output / (label + ".xml"))
    cases = list(xml.iter("testcase"))
    errors = [case for case in cases if case.find("error") is not None]
    failures = [case for case in cases if case.find("failure") is not None]
    if errors:
        raise RuntimeError(f"{label}: test collection/runtime setup errors")
    if label == "before":
        required = [
            case
            for case in failures
            if "valid_int64_scaled_fills" in case.attrib["name"]
            or (
                "full_backtest_preserves_cash_identity" in case.attrib["name"]
                and "large-int64" in case.attrib["name"]
            )
        ]
        if (
            result.returncode != 1
            or len(required) != 6
            or any(
                "average-cost identity failed" not in str(case.find("failure").text)
                for case in required  # type: ignore[union-attr]
            )
        ):
            raise RuntimeError(
                "pinned source did not reproduce the declared accounting failures"
            )
    elif result.returncode != 0 or failures:
        raise RuntimeError("corrected source failed the unchanged regressions")
    return {
        "exit_code": result.returncode,
        "tests": len(cases),
        "failed": len(failures),
        "passed": len(cases) - len(failures),
        "failed_cases": [case.attrib["name"] for case in failures],
    }


def main() -> None:
    output = (ROOT / "experiments/exact-accounting-review").resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, expected in TESTS.items():
        if digest(ROOT / name) != expected:
            raise RuntimeError(f"independent test hash changed: {name}")
    archive = subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="lob-exact-accounting-") as directory:
        baseline = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(baseline, filter="data")
        for name in TESTS:
            shutil.copy2(ROOT / name, baseline / name)
        before = run(baseline, output, "before")
        for name in ("portfolio.py", "exchange.py"):
            shutil.copy2(baseline / "src/lobmm" / name, output / ("before-" + name))
    after = run(ROOT, output, "after")
    for name in ("portfolio.py", "exchange.py"):
        shutil.copy2(ROOT / "src/lobmm" / name, output / ("after-" + name))
    for name in TESTS:
        shutil.copy2(ROOT / name, output / Path(name).name)
    for label in ("before", "after"):
        imports = json.loads((output / (label + "-imports.json")).read_text())
        for name in ("portfolio.py", "exchange.py"):
            if imports[name]["sha256"] != digest(output / (label + "-" + name)):
                raise RuntimeError(f"source changed during {label} tests: {name}")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    report = {
        "baseline": BASELINE,
        "checkout": head,
        "working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "python": sys.version,
        "tests_sha256": TESTS,
        "source_sha256": {
            name: digest(output / name)
            for name in (
                "before-portfolio.py",
                "before-exchange.py",
                "after-portfolio.py",
                "after-exchange.py",
            )
        },
        "before": before,
        "after": after,
        "scope": "Original synthetic regressions only; no market-data or profitability claim.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"before": before, "after": after}, indent=2))


if __name__ == "__main__":
    main()
