"""Reproduce the frozen PR3-11 source composition without merging branches.

Run only in a clean worktree at main plus the preregistration. Historical source
objects must already be available locally; this program never accesses network.
It refuses ambiguous patch application and writes provenance, not trial results.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

MAIN = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
SOURCES = (
    (3, MAIN, "ceccf0c69a3afbc09b07c993b65614ab09fef838"),
    (4, MAIN, "f5a86c5d18dc9d63634fdea95bbb60db3c5f8eec"),
    (5, MAIN, "22ef318adb1292e6a4f76535a73bae854ed5cca8"),
    (6, MAIN, "c4f3611b650faca290d0946b98620604c5d9cfd6"),
    (7, MAIN, "79aec058363a04a8ed6b6f6bdbe200d6e9679d13"),
    (8, MAIN, "1884d4c2e778d00797fbd97fed5b09ba113ed55e"),
    (9, MAIN, "253abee735c02d7805e2a4377c12626742108d84"),
    (
        10,
        "253abee735c02d7805e2a4377c12626742108d84",
        "a105e73cf608c1ff24cdead3dd3c50ee18d8916e",
    ),
    (11, MAIN, "38a17f09746903938fa76b1d42268c8a75c7d3f6"),
)
PREFIX = Path("docs/evidence/component-integration-20261115")
DEPENDENCIES = {
    7: ("docs/evidence/markout-aggregation-20261108/fixtures.json",),
    8: ("docs/evidence/positive-price-20261109/fixtures.json",),
    9: (
        "docs/evidence/exact-ingress-20261111/fixtures.json",
        "docs/evidence/exact-ingress-20261111/contract.json",
    ),
    10: (
        "docs/evidence/lobster-fidelity-20261112/fixtures.json",
        "docs/evidence/lobster-fidelity-20261112/contract.json",
    ),
    11: (
        "docs/evidence/canonical-fidelity-20261113/fixtures.json",
        "docs/evidence/canonical-fidelity-20261113/contract.json",
    ),
}


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], input=data, check=True, capture_output=True
    ).stdout


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> None:
    if PREFIX.exists():
        raise SystemExit("refusing to overwrite prior assembly evidence")
    for name in (
        git("ls-tree", "-r", "--name-only", MAIN, "src/lobmm").decode().splitlines()
    ):
        if Path(name).read_bytes() != git("show", f"{MAIN}:{name}"):
            raise SystemExit(f"source is not pinned main before assembly: {name}")
    records: list[dict[str, object]] = []
    # Persist the attempt before applying any patch; a failure stays visible.
    write_json(PREFIX / "assembly.json", {"complete": False, "patches": records})
    copied: list[dict[str, object]] = []
    for number, base, head in SOURCES:
        patch = git("diff", "--binary", base, head, "--", "src/lobmm")
        record: dict[str, object] = {
            "pr": number,
            "base": base,
            "head": head,
            "patch_sha256": digest(patch),
            "applied": False,
        }
        records.append(record)
        write_json(PREFIX / "assembly.json", {"complete": False, "patches": records})
        git("apply", "--check", data=patch)
        git("apply", data=patch)
        record["applied"] = True
        changed = (
            git("diff", "--name-status", base, head, "--", "tests")
            .decode()
            .splitlines()
        )
        tests: list[str] = []
        for line in changed:
            status, name = line.split("\t")
            if status != "A" and not (
                number == 5 and name == "tests/unit/test_metrics.py"
            ):
                raise SystemExit(f"unplanned test delta: {number} {line}")
            tests.append(name)
        for name in [*tests, *DEPENDENCIES.get(number, ())]:
            original = git("show", f"{head}:{name}")
            target = Path(name)
            if target.exists() and name != "tests/unit/test_metrics.py":
                raise SystemExit(f"unplanned existing copied path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
            copied.append({"path": name, "head": head, "sha256": digest(original)})
    names = git("ls-tree", "-r", "--name-only", MAIN, "src/lobmm").decode().splitlines()
    c1 = {name: digest(Path(name).read_bytes()) for name in names}
    original_risk = git("show", f"{MAIN}:src/lobmm/risk.py")
    (PREFIX / "risk-main.py").write_bytes(original_risk)
    c0 = {**c1, "src/lobmm/risk.py": digest(original_risk)}
    write_json(PREFIX / "source-manifests.json", {"c0": c0, "c1": c1})
    write_json(
        PREFIX / "assembly.json",
        {
            "complete": True,
            "main": MAIN,
            "design_commit": "cc42192",
            "patches": records,
            "copied_dependencies": copied,
            "baseline_risk_sha256": digest(original_risk),
            "changed_production_paths": [
                name
                for name in names
                if digest(git("show", f"{MAIN}:{name}")) != c1[name]
            ],
            "source_file_count_per_role": len(names),
        },
    )
    print(
        f"Assembled {len(records)} pinned patches; source files per role: {len(names)}"
    )


if __name__ == "__main__":
    main()
