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
RENDER_BASELINE = "116a8618e4fe5157597015e3bf08f793ac7b720b"
RENDER_TESTS = {
    "tests/unit/test_portfolio_decimal_growth_independent.py": "84f49ec867b9f71c5882500654355adc0d64bfd6d32590c7c3299e5b84567430",
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


def run(
    root: Path,
    output: Path,
    label: str,
    tests: dict[str, str] = TESTS,
) -> dict[str, object]:
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    command = [
        sys.executable,
        "-c",
        RUNNER,
        str(root),
        str(output),
        label,
        *tests,
        "-q",
        "--tb=short",
        f"--junitxml={output / (label + '.xml')}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=180 if label.startswith("growth-") else None,
        )
    except subprocess.TimeoutExpired as exc:
        captured = "".join(
            value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            for value in (exc.stdout, exc.stderr)
        )
        (output / (label + ".log")).write_text(captured + "\nTimed out after 180s.\n")
        raise RuntimeError(f"{label}: bounded rendering regression timed out") from exc
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
    elif label == "growth-before":
        required = {
            "test_public_small_fill_cycles_survive_terminating_decimal_growth",
            "test_reachable_4301_digit_decimal_renders_exactly_without_guard_override[1]",
            "test_reachable_4301_digit_decimal_renders_exactly_without_guard_override[-1]",
        }
        if (
            result.returncode != 1
            or len(cases) != 16
            or {case.attrib["name"] for case in failures} != required
            or any(
                "ValueError: Exceeds the limit (4300 digits) for integer string conversion"
                not in str(case.find("failure").text)
                for case in failures  # type: ignore[union-attr]
            )
        ):
            raise RuntimeError(
                "pinned PR3 source did not reproduce the declared integer-string guard failures"
            )
    elif result.returncode != 0 or failures:
        raise RuntimeError("corrected source failed the unchanged regressions")
    if label == "growth-after" and len(cases) != 16:
        raise RuntimeError("corrected source did not execute all rendering regressions")
    return {
        "exit_code": result.returncode,
        "tests": len(cases),
        "failed": len(failures),
        "passed": len(cases) - len(failures),
        "failed_cases": [case.attrib["name"] for case in failures],
    }


def reproduce_rendering(output: Path, head: str, dirty: bool) -> dict[str, object]:
    """Retain the separate PR3 regression without replacing the main comparison."""

    output = output / "decimal-growth"
    output.mkdir(parents=True, exist_ok=True)
    for name, expected in RENDER_TESTS.items():
        if digest(ROOT / name) != expected:
            raise RuntimeError(f"independent rendering test hash changed: {name}")
    archive = subprocess.check_output(["git", "archive", RENDER_BASELINE], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="lob-decimal-growth-") as directory:
        baseline = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(baseline, filter="data")
        for name in RENDER_TESTS:
            shutil.copy2(ROOT / name, baseline / name)
        before = run(baseline, output, "growth-before", RENDER_TESTS)
        for name in ("portfolio.py", "exchange.py"):
            shutil.copy2(
                baseline / "src/lobmm" / name, output / ("growth-before-" + name)
            )
    after = run(ROOT, output, "growth-after", RENDER_TESTS)
    for name in ("portfolio.py", "exchange.py"):
        shutil.copy2(ROOT / "src/lobmm" / name, output / ("growth-after-" + name))
    for name in RENDER_TESTS:
        shutil.copy2(ROOT / name, output / Path(name).name)
    source_hashes = {}
    for label in ("growth-before", "growth-after"):
        imports = json.loads((output / (label + "-imports.json")).read_text())
        for name in ("portfolio.py", "exchange.py"):
            saved = label + "-" + name
            source_hashes[saved] = digest(output / saved)
            if imports[name]["sha256"] != source_hashes[saved]:
                raise RuntimeError(f"source changed during {label} tests: {name}")
    report = {
        "baseline": RENDER_BASELINE,
        "checkout": head,
        "working_tree_dirty": dirty,
        "python": sys.version,
        "tests_sha256": RENDER_TESTS,
        "source_sha256": source_hashes,
        "subprocess_timeout_seconds": 180,
        "before": before,
        "after": after,
        "scope": "Fixed synthetic partial-close sequence and exact rendering; no market-data or profitability claim.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


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
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()
    )
    report = {
        "baseline": BASELINE,
        "checkout": head,
        "working_tree_dirty": dirty,
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
    rendering = reproduce_rendering(output, head, dirty)
    print(
        json.dumps(
            {"before": before, "after": after, "decimal_growth": rendering}, indent=2
        )
    )


if __name__ == "__main__":
    main()
