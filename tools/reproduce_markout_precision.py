"""One pinned quantity-precision witness, isolated from the original study runner.

Local before/after stages each run one source once. Verify replicates the two
sources for CI. No engines, additional cases, performance or data acquisition.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = "d6d9e6a2b1a4a2dd3a7b718cb0ce1acac2f18e1b"
PROTOTYPE_METRICS = "022f60f4f2bb7edd4d7fb9aa0ef780be01f344d8e97ae90ded82b6a7b2d5ae5b"
TEST = "tests/unit/test_markout_quantity_precision_independent.py"
TEST_SHA = "c695fa3fa8c1e22d045c02b21e2b32aa3d7cba371535a9544f86884f0c50625a"
NODE = (
    TEST
    + "::test_large_quantity_split_preserves_exact_observed_markout_amount_and_means"
)
PREFIX = "docs/evidence/markout-aggregation-20261108/"
CONTRACT = PREFIX + "contract.json"
AMENDMENT = PREFIX + "quantity-precision-amendment.json"
PINS = {
    TEST: TEST_SHA,
    CONTRACT: "f01dfc365c4784b363c856dc2895d55609cd8249d140759eb159cf3336bd7ddf",
    AMENDMENT: "b52c49842c488ee13fe50288871350b9c314df1c50c52f6280aa003c34e1c5bc",
    "tools/reproduce_markout_aggregation.py": "fd0c9f0309541f5f579bffb55a9156f886fd998c28c4bd010175e280fac92fbf",
    "tests/unit/test_markout_aggregation_independent.py": "f25de34f65497fef3df68306f81206737e520421b43804faa268a6a02b9eca29",
}

# Import identity is checked after pytest collects the frozen test, in that
# same process. This catches an already-imported editable package as well.
WORKER = r"""
import hashlib, importlib, json, pathlib, sys
import pytest
root, output, pins_path, test = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(root / 'src'))
expected = json.loads(pins_path.read_text())
record = {'python': sys.version, 'pytest': pytest.__version__, 'nodes': [], 'reports': [], 'exceptions': []}
class Audit:
    def pytest_collection_finish(self, session):
        record['nodes'] = [item.nodeid for item in session.items]
        imported = {}
        for name, sha in expected.items():
            module = importlib.import_module('lobmm.' + name)
            path = pathlib.Path(module.__file__).resolve()
            if path != (root / 'src/lobmm' / (name + '.py')).resolve():
                raise RuntimeError('wrong imported source: ' + str(path))
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed != sha:
                raise RuntimeError('wrong imported source hash: ' + name)
            imported[name] = {'path': str(path), 'sha256': observed}
        record['imports'] = imported
        output.write_text(json.dumps(record, indent=2) + '\n')
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        if call.when == 'call' and call.excinfo is not None:
            record['exceptions'].append({'nodeid': item.nodeid,
                'type': call.excinfo.type.__name__, 'message': str(call.excinfo.value)})
    def pytest_runtest_logreport(self, report):
        record['reports'].append({'nodeid': report.nodeid, 'when': report.when,
            'outcome': report.outcome,
            'longrepr': report.longreprtext if report.failed or report.skipped else None})
if not __debug__:
    raise RuntimeError('assertion-enabled Python required')
record['exit_code'] = int(pytest.main([str(test), '-q', '--tb=short',
    '--junitxml=' + str(output.with_suffix('.xml'))], plugins=[Audit()]))
output.write_text(json.dumps(record, indent=2) + '\n')
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def reproduce(stage: str, candidate: str | None, output: Path) -> int:
    if not __debug__ or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise RuntimeError("require assertion-enabled Python and new/empty output")
    for name, sha in PINS.items():
        if digest(ROOT / name) != sha:
            raise RuntimeError("frozen input changed: " + name)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, output / "reproducer.py")
    for name in (TEST, CONTRACT, AMENDMENT):
        shutil.copy2(ROOT / name, output / Path(name).name)
    report: dict = {
        "status": "in_progress",
        "stage": stage,
        "harness_sha256": digest(Path(__file__).resolve()),
        "pins": PINS,
        "prototype": PROTOTYPE,
        "prototype_metrics_sha256": PROTOTYPE_METRICS,
        "python": sys.version,
        "process_cap_seconds": 120,
        "observations": [],
    }
    target = output / "report.json"
    dump(target, report)
    sources = [("before", PROTOTYPE)] if stage == "before" else []
    if stage == "verify":
        sources.append(("before", PROTOTYPE))
    if stage != "before":
        sources.append(("after", candidate))
    try:
        base_pins = json.loads((ROOT / CONTRACT).read_text())["source_sha256"]
        for label, revision in sources:
            row: dict = {"label": label, "revision": revision, "status": "in_progress"}
            report["observations"].append(row)
            dump(target, report)
            with tempfile.TemporaryDirectory(
                prefix="lob-markout-precision-"
            ) as temporary:
                root = Path(temporary)
                resolved = subprocess.check_output(
                    ["git", "rev-parse", "--verify", revision + "^{commit}"],
                    cwd=ROOT,
                    text=True,
                ).strip()
                if resolved != revision:
                    raise RuntimeError("explicit commit SHA did not resolve to itself")
                bundle = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
                with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
                    archive.extractall(root, filter="data")
                retained = output / (label + "-sources")
                retained.mkdir()
                hashes = {
                    name: digest(root / "src/lobmm" / (name + ".py"))
                    for name in base_pins
                }
                for name, expected in base_pins.items():
                    if name != "metrics" and hashes[name] != expected:
                        raise RuntimeError("non-metrics source differs: " + name)
                    shutil.copy2(
                        root / "src/lobmm" / (name + ".py"), retained / (name + ".py")
                    )
                if label == "before" and hashes["metrics"] != PROTOTYPE_METRICS:
                    raise RuntimeError("prototype metrics hash differs")
                test = root / TEST
                test.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / TEST, test)
                pins_path = output / (label + "-source-pins.json")
                dump(pins_path, hashes)
                result_path = output / (label + ".json")
                row.update(
                    source_sha256=hashes,
                    git_archive_sha256=hashlib.sha256(bundle).hexdigest(),
                )
                dump(target, report)
                env = dict(os.environ, PYTHONPATH=str(root / "src"))
                env.pop("PYTEST_ADDOPTS", None)
                try:
                    process = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            WORKER,
                            str(root),
                            str(result_path),
                            str(pins_path),
                            str(test),
                        ],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except subprocess.TimeoutExpired as exc:
                    result_path.with_suffix(".log").write_text(
                        "".join(
                            value.decode(errors="replace")
                            if isinstance(value, bytes)
                            else value or ""
                            for value in (exc.stdout, exc.stderr)
                        )
                    )
                    raise
                result_path.with_suffix(".log").write_text(
                    process.stdout + process.stderr
                )
                if process.returncode:
                    raise RuntimeError("precision worker failed: " + label)
                result = json.loads(result_path.read_text())
                calls = [item for item in result["reports"] if item["when"] == "call"]
                bad = [
                    item
                    for item in result["reports"]
                    if item["outcome"] == "skipped"
                    or (item["when"] != "call" and item["outcome"] == "failed")
                ]
                expected = "failed" if label == "before" else "passed"
                if (
                    result["nodes"] != [NODE]
                    or len(calls) != 1
                    or calls[0]["outcome"] != expected
                    or bad
                    or result["exit_code"] != (1 if label == "before" else 0)
                ):
                    raise RuntimeError(
                        "precision witness did not have the prescribed one-fail/one-pass outcome"
                    )
                if label == "before" and [
                    item["type"] for item in result["exceptions"]
                ] != ["AssertionError"]:
                    raise RuntimeError(
                        "prototype failure was not the frozen assertion witness"
                    )
                if label == "before" and "[0.0,0.25]==[0.25,0.25]" not in "".join(
                    result["exceptions"][0]["message"].split()
                ):
                    raise RuntimeError(
                        "prototype did not reproduce the frozen amount pair"
                    )
                if label == "after" and result["exceptions"]:
                    raise RuntimeError(
                        "candidate retained a precision-witness exception"
                    )
                if digest(test) != TEST_SHA or hashes != {
                    name: digest(root / "src/lobmm" / (name + ".py"))
                    for name in base_pins
                }:
                    raise RuntimeError(
                        "source or test changed during precision witness"
                    )
                row.update(
                    status="complete",
                    result=result,
                    result_sha256=digest(result_path),
                    junit_sha256=digest(result_path.with_suffix(".xml")),
                )
                dump(target, report)
                print(
                    json.dumps(
                        {
                            "label": label,
                            "revision": revision,
                            "source_sha256": hashes,
                            "nodes": result["nodes"],
                            "passed": int(expected == "passed"),
                            "failed": int(expected == "failed"),
                            "failed_nodeids": [NODE] if label == "before" else [],
                            "assertion_failures": result["exceptions"],
                        }
                    ),
                    flush=True,
                )
        report["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        dump(target, report)
        print(str(exc), file=sys.stderr)
        return 1
    dump(target, report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("before", "after", "verify"), required=True)
    parser.add_argument("--candidate", metavar="SHA")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.stage == "before":
        if args.candidate is not None:
            parser.error("before uses only the pinned prototype")
    elif (
        args.candidate is None
        or len(args.candidate) != 40
        or any(char not in "0123456789abcdef" for char in args.candidate)
    ):
        parser.error("after/verify require an explicit full lowercase candidate SHA")
    return reproduce(args.stage, args.candidate, args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
