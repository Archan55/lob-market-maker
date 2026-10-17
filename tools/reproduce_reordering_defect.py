"""Pinned original strategy versus final strategy on the same full replay delays.

Only the cached quote implementation is replaced in a fresh subprocess. The
bounded delay extension supplies a previously unsupported ordering. This is a
new fault-path defect, not a claim that old reliable/default studies were wrong.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

BASELINE = "3da58b38b49108cc116fa4910d77da689a1ab922"
SUITE = "tests/integration/test_reordering_stress_v1.py"


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--baseline-child":
        import pytest

        spec = importlib.util.spec_from_file_location(
            "lobmm.strategies.base", sys.argv[2]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        raise SystemExit(pytest.main([SUITE, "-q", "-k", "test_stale_partial_defect"]))
    root = Path(
        sys.argv[1] if len(sys.argv) > 1 else "experiments/reordering-defect-v1"
    )
    root.mkdir(parents=True, exist_ok=True)
    source = root / "baseline_strategy.py"
    source.write_bytes(
        subprocess.check_output(
            ["git", "show", f"{BASELINE}:src/lobmm/strategies/base.py"]
        )
    )
    for label, args, code, marker in (
        (
            "before",
            [sys.executable, __file__, "--baseline-child", str(source)],
            1,
            "6 failed",
        ),
        (
            "after",
            [
                sys.executable,
                "-m",
                "pytest",
                SUITE,
                "-q",
                "-k",
                "test_stale_partial_defect",
            ],
            0,
            "6 passed",
        ),
    ):
        result = subprocess.run(args, capture_output=True, text=True)
        (root / f"{label}.txt").write_text(result.stdout + result.stderr)
        assert result.returncode == code and marker in result.stdout, (
            result.stdout + result.stderr
        )
    summary = dict(
        baseline=BASELINE,
        baseline_failed=6,
        fixed_passed=6,
        expected_remaining=2,
        baseline_stale_remaining=4,
        limitation="Original strategy on newly supported reordered deliveries; old default correctness is not contradicted.",
    )
    (root / "results.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
