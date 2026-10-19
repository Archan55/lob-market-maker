"""Reproduce pre-fix failures in isolated subprocesses without changing checkout.

Run from the repository root with the installed development environment.
The original source is read from the pinned PR1 commit, never downloaded anew.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

BASELINE = "d4104b70660976c6d59fe8915a75ac5616bdecd3"
SUITES = {
    "marks": ("backtest", "tests/integration/test_conservative_fill_marks.py", 8, 2),
    "reduce_only": ("risk", "tests/unit/test_reduce_only_reservations.py", 14, 2),
}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--baseline-child":
        import pytest

        name, source = sys.argv[2:]
        spec = importlib.util.spec_from_file_location(f"lobmm.{name}", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"lobmm.{name}"] = module
        spec.loader.exec_module(module)
        suite = next(v[1] for v in SUITES.values() if v[0] == name)
        return int(pytest.main([suite, "-q"]))

    root = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("experiments/exposure-defects-v1")
    )
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    for label, (name, suite, failed, passed) in SUITES.items():
        source = subprocess.check_output(
            ["git", "show", f"{BASELINE}:src/lobmm/{name}.py"]
        )
        path = root / f"baseline_{name}.py"
        path.write_bytes(source)
        before = subprocess.run(
            [sys.executable, __file__, "--baseline-child", name, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        (root / f"{label}_before.txt").write_text(before.stdout)
        assert (
            before.returncode == 1
            and f"{failed} failed, {passed} passed" in before.stdout
        ), before.stdout
        after = subprocess.run(
            [sys.executable, "-m", "pytest", suite, "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        (root / f"{label}_after.txt").write_text(after.stdout)
        assert after.returncode == 0 and f"{failed + passed} passed" in after.stdout, (
            after.stdout
        )
        results[label] = dict(
            baseline_failed=failed,
            baseline_controls_passed=passed,
            fixed_passed=failed + passed,
        )
    (root / "results.json").write_text(
        json.dumps(dict(baseline=BASELINE, suites=results), indent=2) + "\n"
    )
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
