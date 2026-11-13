"""Frozen canonical integer-boundary reproduction, using pytest-only workers.

Baseline executes main once; study rehashes/reuses it and executes only the
candidate. Verify repeats both sources for CI. No controller call converts data
or invokes the independent oracle. JSON retains Python integer precision.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
PREFIX = "docs/evidence/canonical-fidelity-20261113/"
CONTRACT, FIXTURES, REVIEW = (
    PREFIX + "contract.json",
    PREFIX + "fixtures.json",
    PREFIX + "design-review.json",
)
TEST = "tests/unit/test_canonical_fidelity_independent.py"
CONTROLLER = "tools/reproduce_canonical_fidelity.py"
CONTRACT_SHA = "0deb4423843996239e62f09342e88bce9a09176700ab9e0056aff192c5ad1730"
FIXTURES_SHA = "bbd59beaae6feb0cac0d0cf965ace94b055b113b984d8e505a008f00bbe9a3e7"
REVIEW_SHA = "605e3690d18a082784246719be711f3e3fc05de7e05bc7f64882b6e962bfb6e1"
TEST_SHA = "9c07f5cce83d632ac5d648a31ef38d5a2fba5b107f032b3d5032ecb5ac130855"
ALLOWED = {"src/lobmm/data/schema.py", "src/lobmm/data/loaders.py"}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def environment() -> dict[str, Any]:
    dependencies = {}
    for name in (
        "pytest",
        "polars",
        "pyarrow",
        "pydantic",
        "PyYAML",
        "numpy",
        "matplotlib",
        "typer",
    ):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "dependencies": dependencies,
        "integer_string_guard": sys.get_int_max_str_digits(),
        "assertions_enabled": __debug__,
    }


def frozen(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    pins = {
        CONTRACT: CONTRACT_SHA,
        FIXTURES: FIXTURES_SHA,
        REVIEW: REVIEW_SHA,
        TEST: TEST_SHA,
        CONTROLLER: digest(Path(__file__).resolve()),
    }
    for name, sha in pins.items():
        if len(sha) != 64 or digest(root / name) != sha:
            raise RuntimeError("unpinned or changed frozen input: " + name)
    sources = json.loads((root / CONTRACT).read_text())["source_sha256"]
    if len(sources) != 38 or not sources.keys() >= ALLOWED:
        raise RuntimeError("unexpected production source inventory")
    return pins, sources


def archive(revision: str, root: Path, retained: Path) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if resolved != revision:
        raise RuntimeError("explicit full commit SHA required")
    content = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        bundle.extractall(root, filter="data")
    pins, expected = frozen(ROOT)
    if {
        str(p.relative_to(root)) for p in (root / "src").rglob("*") if p.is_file()
    } != expected.keys():
        raise RuntimeError("archive differs from frozen 38-file source inventory")
    hashes = {}
    for name in expected:
        hashes[name] = digest(root / name)
        target = retained / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    for name, sha in pins.items():
        target = root / name
        if target.exists() and digest(target) != sha:
            raise RuntimeError("archive changed a frozen input: " + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        target = retained / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return {
        "resolved_revision": resolved,
        "git_archive_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": hashes,
    }


def import_identity(root: Path, expected: dict[str, str]) -> dict[str, Any]:
    proof = {}
    for name, sha in expected.items():
        path = root / name
        if digest(path) != sha:
            raise RuntimeError("worker source bytes changed: " + name)
        if path.suffix == ".py":
            parts = list(Path(name).with_suffix("").parts[1:])
            if parts[-1] == "__init__":
                parts.pop()
            module = importlib.import_module(".".join(parts))
            if Path(module.__file__).resolve() != path.resolve():
                raise RuntimeError("wrong imported/editable source: " + name)
        proof[name] = {"path": str(path.resolve()), "sha256": sha}
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if (
            (name == "lobmm" or name.startswith("lobmm."))
            and path
            and not Path(path).resolve().is_relative_to(root / "src")
        ):
            raise RuntimeError("foreign lobmm import: " + name)
    return proof


def worker(root: Path, output: Path, source_pins: Path) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled interpreter required")
    sys.path.insert(0, str(root / "src"))
    import pytest

    expected = json.loads(source_pins.read_text())
    evidence = output.parent / (output.stem + "-evidence")
    evidence.mkdir()
    os.environ["LOB_CANONICAL_FIDELITY_EVIDENCE_DIR"] = str(evidence)
    result: dict[str, Any] = {
        "status": "in_progress",
        "nodes": [],
        "reports": [],
        "exceptions": [],
        "environment": environment(),
        "evidence_directory": evidence.name,
    }
    dump(output, result)
    result["imports_before_collection"] = import_identity(root, expected)

    class Audit:
        def pytest_collection_finish(self, session: Any) -> None:
            result["nodes"] = [item.nodeid for item in session.items]
            result["imports_after_collection"] = import_identity(root, expected)
            dump(output, result)

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
            if call.when == "call" and call.excinfo is not None:
                result["exceptions"].append(
                    {
                        "nodeid": item.nodeid,
                        "type": call.excinfo.type.__name__,
                        "message": str(call.excinfo.value),
                    }
                )

        def pytest_runtest_logreport(self, report: Any) -> None:
            result["reports"].append(
                {
                    "nodeid": report.nodeid,
                    "when": report.when,
                    "outcome": report.outcome,
                    "longrepr": report.longreprtext
                    if report.failed or report.skipped
                    else None,
                }
            )
            dump(output, result)

    result["pytest_exit_code"] = int(
        pytest.main(
            [
                str(root / TEST),
                "-q",
                "--tb=short",
                "--junitxml=" + str(output.with_suffix(".xml")),
            ],
            plugins=[Audit()],
        )
    )
    result["imports_after_execution"] = import_identity(root, expected)
    result["frozen_pins_after_execution"] = frozen(root)[0]
    result["environment_after"] = environment()
    calls = [x for x in result["reports"] if x["when"] == "call"]
    result["counts"] = {
        "collected": len(result["nodes"]),
        "passed": sum(x["outcome"] == "passed" for x in calls),
        "failed": sum(x["outcome"] == "failed" for x in calls),
        "errors": sum(
            x["when"] != "call" and x["outcome"] == "failed" for x in result["reports"]
        ),
        "skipped": sum(x["outcome"] == "skipped" for x in result["reports"]),
    }
    result["status"] = "complete"
    dump(output, result)
    return 0


def manifest(directory: Path) -> dict[str, Any]:
    return {
        str(p.relative_to(directory)): {"sha256": digest(p), "bytes": p.stat().st_size}
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p != directory / "report.json"
    }


def rehash(directory: Path, expected: dict[str, Any]) -> None:
    for name, item in expected.items():
        path = directory / name
        if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise RuntimeError("retained evidence changed: " + name)


def identity(result: dict[str, Any], source: dict[str, str]) -> None:
    for phase in (
        "imports_before_collection",
        "imports_after_collection",
        "imports_after_execution",
    ):
        proof = result[phase]
        if (
            set(proof) != set(source)
            or len({p["path"].split("/src/lobmm/")[0] for p in proof.values()}) != 1
        ):
            raise RuntimeError("incomplete or mixed source identities")
        if any(
            proof[name]["sha256"] != sha or not proof[name]["path"].endswith("/" + name)
            for name, sha in source.items()
        ):
            raise RuntimeError("worker source path/hash differs")
    if (
        result["frozen_pins_after_execution"] != frozen(ROOT)[0]
        or result["environment"]["integer_string_guard"]
        != result["environment_after"]["integer_string_guard"]
    ):
        raise RuntimeError("worker modified frozen inputs or the integer guard")


def records(
    result: dict[str, Any], directory: Path
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    evidence = directory / result["evidence_directory"]
    if not evidence.resolve().is_relative_to(directory.resolve()):
        raise RuntimeError("evidence path escapes the result")
    return (
        evidence,
        json.loads((evidence / "corpus.json").read_text()),
        json.loads((evidence / "normal-control.json").read_text()),
    )


def strict_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("valid") is True
        and value.get("mode") == "strict"
        and value.get("reconstructed_book") is True
        and value.get("required_nonempty") is True
        and value.get("issue_count") == 0
    )


def actual_files(row: dict[str, Any], evidence: Path) -> None:
    for group in ("source_files", "output_files"):
        for item in row.get(group, {}).values():
            path = evidence / item["path"]
            if (
                not path.resolve().is_relative_to(evidence.resolve())
                or digest(path) != item["sha256"]
            ):
                raise RuntimeError("actual case bytes differ: " + row["case_id"])
    value = row.get("result")
    if value is not None:
        path = evidence / value["parquet_path"]
        if (
            not path.resolve().is_relative_to(evidence.resolve())
            or digest(path) != value["parquet_sha256"]
        ):
            raise RuntimeError(
                "actual canonical audit Parquet differs: " + row["case_id"]
            )


def compact(corpus: dict[str, Any], normal: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for row in corpus.get("observations", []):
        value = row.get("result")
        cases.append(
            {
                "case": row["case_id"],
                "route": row["route"],
                "expected": row.get("expected"),
                "status": row["status"],
                "actual_rows": value["rows"] if value else None,
                "actual_error": row["error"],
                "strict_valid": row.get("strict_valid"),
                "boundary_returned": row.get("boundary_returned"),
                "writer_output_exists": row.get("writer_output_exists"),
                "failed_checks": [
                    name for name, passed in row["checks"].items() if passed is not True
                ],
            }
        )
    control = normal.get("observation")
    return {
        "case_ledger": cases,
        "normal_control": {
            "status": control["status"],
            "rows": control["result"]["rows"] if control.get("result") else None,
            "output_files": control.get("output_files"),
            "record_sha256": object_hash(control),
        }
        if control
        else None,
    }


def validate(result: dict[str, Any], role: str, directory: Path) -> dict[str, Any]:
    spec = json.loads((ROOT / FIXTURES).read_text())
    count = result["counts"]
    if (
        result["status"] != "complete"
        or result["pytest_exit_code"] not in (0, 1)
        or count["errors"]
        or count["skipped"]
        or len(result["nodes"]) != 2
        or len(set(result["nodes"])) != 2
        or count["collected"] != 2
        or count["passed"] + count["failed"] != 2
    ):
        raise RuntimeError("two-node corpus did not execute without errors/skips")
    if role == "candidate" and (result["pytest_exit_code"] != 0 or count["failed"]):
        raise RuntimeError("candidate failed the frozen independent tests")
    evidence, corpus, normal = records(result, directory)
    if (
        not corpus["complete"]
        or len(corpus["observations"]) != 33
        or [r["case_id"] for r in corpus["observations"]]
        != [c["id"] for c in spec["cases"]]
    ):
        raise RuntimeError("fixed 33-case ledger differs")
    if (
        sum(c["expect"] == "admit" for c in spec["cases"]) != 12
        or sum(c["expect"] == "reject" for c in spec["cases"]) != 21
    ):
        raise RuntimeError("frozen admit/reject classification differs")
    primary = {}
    for row, case in zip(corpus["observations"], spec["cases"], strict=True):
        label = case["id"]
        if (
            row["infrastructure_error"]
            or row["input"] != case
            or row["route"] != case["route"]
            or not row["checks"]
        ):
            raise RuntimeError("case setup/oracle/infrastructure mismatch: " + label)
        actual_files(row, evidence)
        if row["status"] == "admitted":
            value = row["result"]
            if (
                row["error"] is not None
                or not strict_valid(value["validation"])
                or value["dictionary_reconstruction"]["valid"] is not True
            ):
                raise RuntimeError(
                    "admitted output lacks strict book certification: " + label
                )
        if role == "candidate":
            if (
                not all(v is True for v in row["checks"].values())
                or row["raw_input"] != row["raw_input_after"]
                or row["source_files"] != row["source_files_after"]
                or row["context_before"] != row["context_after"]
                or row["guard_before"] != row["guard_after"]
            ):
                raise RuntimeError(
                    "candidate fidelity/mutation/context check failed: " + label
                )
            if case["expect"] == "reject":
                if (
                    row["status"] != "rejected"
                    or row["result"] is not None
                    or row["boundary_returned"] is not False
                ):
                    raise RuntimeError(
                        "candidate did not reject before canonical output: " + label
                    )
                if (
                    case["route"].startswith("write_")
                    and row["writer_output_exists"] is not False
                ):
                    raise RuntimeError(
                        "rejected writer left a canonical file: " + label
                    )
            elif (
                row["status"] != "admitted"
                or row["result"]["rows"] != row["expected"]["rows"]
            ):
                raise RuntimeError("candidate exact admission differs: " + label)
        if label in ("mapping_fractional_quantity", "frame_fractional_quantity"):
            primary[label] = row
    if role == "main":
        if len(primary) != 2 or any(
            row["status"] != "admitted"
            or row["error"] is not None
            or row["result"]["rows"][1]["quantity"] != 10
            or not strict_valid(row["result"]["validation"])
            for row in primary.values()
        ):
            raise RuntimeError(
                "predicted strict-valid quantity10 false admissions absent"
            )
        if result["pytest_exit_code"] != 1 or not count["failed"]:
            raise RuntimeError("main did not expose any frozen mismatch")
    if not normal["complete"] or not normal.get("observation"):
        raise RuntimeError("ordinary control did not complete")
    control = normal["observation"]
    actual_files(control, evidence)
    if (
        control["infrastructure_error"]
        or control["status"] != "admitted"
        or control["error"] is not None
        or not control["checks"]
        or not all(control["checks"].values())
        or control["result"]["rows"] != spec["base_rows"]
        or len(control["result"]["rows"]) != 6
        or not strict_valid(control["result"]["validation"])
        or set(control["output_files"]) != {"csv", "parquet"}
    ):
        raise RuntimeError("ordinary six-event CSV/Parquet control failed")
    return {
        "observation_count": 33,
        "declared_cases": [x["case_id"] for x in corpus["observations"]],
        "declared_nodes": result["nodes"],
        "primary_gate_passed": True,
        "normal_control": control,
    }


def baseline_evidence(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = json.loads((directory / "report.json").read_text())
    pins, sources = frozen(ROOT)
    if (
        report["status"] != "complete"
        or report["stage"] != "baseline"
        or not report["criterion_passed"]
        or report["pins"] != pins
        or report["harness_sha256"] != digest(Path(__file__).resolve())
        or len(report["observations"]) != 1
        or set(report["sources"]) != {"main"}
    ):
        raise RuntimeError("baseline is not complete frozen main evidence")
    sample = report["observations"][0]
    if (
        sample["role"] != "main"
        or sample["status"] != "complete"
        or sample["resolved_revision"] != BASELINE
        or sample["source_sha256"] != sources
    ):
        raise RuntimeError("baseline did not execute one pinned main worker")
    if any(
        sample[key] != report["sources"]["main"][key]
        for key in ("resolved_revision", "source_sha256", "git_archive_sha256")
    ):
        raise RuntimeError("baseline worker/source metadata differ")
    rehash(directory, report["evidence_manifest"])
    if json.loads((directory / "main.json").read_text()) != sample["result"]:
        raise RuntimeError("raw baseline worker file differs")
    identity(sample["result"], sources)
    if (
        validate(sample["result"], "main", directory) != sample["criteria"]
        or hashlib.sha256(
            subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
        ).hexdigest()
        != sample["git_archive_sha256"]
    ):
        raise RuntimeError("baseline raw criteria/archive metadata differ")
    return sample, report["sources"]["main"]


def reproduce(
    stage: str, candidate: str | None, reuse: Path | None, output: Path
) -> int:
    if not __debug__ or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise RuntimeError("require assertions and a new/empty output directory")
    pins, expected = frozen(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, output / "reproducer.py")
    for name in pins:
        target = output / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    dump(output / "environment.json", environment())
    report: dict[str, Any] = {
        "status": "in_progress",
        "stage": stage,
        "pins": pins,
        "harness_sha256": digest(Path(__file__).resolve()),
        "process_cap_seconds": CAP_SECONDS,
        "sources": {},
        "observations": [],
        "criterion_passed": False,
        "scope": "Owned synthetic canonical-boundary correctness and one exposed six-event control, no economic/data/performance result.",
    }
    target = output / "report.json"
    dump(target, report)
    try:
        previous, previous_source = baseline_evidence(reuse) if reuse else (None, None)
        if reuse:
            report["reused_baseline_evidence"] = {
                "directory": str(reuse),
                "report_sha256": digest(reuse / "report.json"),
            }
            report["sources"]["main"] = previous_source
        plan = [("main", BASELINE)] if stage in ("baseline", "verify") else []
        if stage in ("study", "verify"):
            plan.append(("candidate", candidate))
        with tempfile.TemporaryDirectory(prefix="lob-canonical-fidelity-") as temporary:
            for role, revision in plan:
                sample: dict[str, Any] = {"role": role, "status": "in_progress"}
                report["observations"].append(sample)
                dump(target, report)
                source_root = Path(temporary) / role
                source = archive(revision, source_root, output / "sources" / role)
                for name, sha in source["source_sha256"].items():
                    if (role == "main" or name not in ALLOWED) and sha != expected[
                        name
                    ]:
                        raise RuntimeError("unapproved production difference: " + name)
                sample.update(source)
                report["sources"][role] = source
                pins_path = output / (role + "-source-pins.json")
                dump(pins_path, source["source_sha256"])
                result_path = output / (role + ".json")
                dump(target, report)
                env = dict(
                    os.environ,
                    PYTHONPATH=str(source_root / "src"),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                env.pop("PYTEST_ADDOPTS", None)
                command = [
                    sys.executable,
                    str(output / "reproducer.py"),
                    "--worker-root",
                    str(source_root),
                    "--worker-output",
                    str(result_path),
                    "--worker-pins",
                    str(pins_path),
                ]
                try:
                    process = subprocess.run(
                        command,
                        cwd=source_root,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=CAP_SECONDS,
                    )
                except subprocess.TimeoutExpired as exc:
                    result_path.with_suffix(".log").write_text(
                        "".join(
                            v.decode(errors="replace")
                            if isinstance(v, bytes)
                            else v or ""
                            for v in (exc.stdout, exc.stderr)
                        )
                    )
                    sample["status"] = "timeout"
                    raise
                result_path.with_suffix(".log").write_text(
                    process.stdout + process.stderr
                )
                sample["worker_returncode"] = process.returncode
                if process.returncode:
                    raise RuntimeError("worker infrastructure failure: " + role)
                result = json.loads(result_path.read_text())
                sample["result"] = result
                _, corpus, normal = records(result, output)
                sample["observed_summary"] = compact(corpus, normal)
                dump(target, report)
                print(
                    "CANONICAL_FIDELITY_OBSERVATIONS "
                    + json.dumps(
                        {
                            "role": role,
                            "revision": revision,
                            "source_sha256": source["source_sha256"],
                            "counts": result["counts"],
                            "nodes": result["nodes"],
                            "failed_nodes": [
                                x["nodeid"]
                                for x in result["reports"]
                                if x["when"] == "call" and x["outcome"] == "failed"
                            ],
                            **sample["observed_summary"],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                identity(result, source["source_sha256"])
                sample["criteria"] = validate(result, role, output)
                if role == "candidate":
                    if (
                        previous is None
                        or previous["result"]["nodes"] != result["nodes"]
                        or previous["criteria"]["declared_cases"]
                        != sample["criteria"]["declared_cases"]
                        or previous["criteria"]["normal_control"]
                        != sample["criteria"]["normal_control"]
                    ):
                        raise RuntimeError(
                            "candidate nodes/cases or normal actual CSV/Parquet/full record differ"
                        )
                else:
                    previous = sample
                sample["status"] = "complete"
                dump(target, report)
                print(
                    "CANONICAL_FIDELITY_WORKER "
                    + json.dumps(
                        {
                            "role": role,
                            "revision": revision,
                            "counts": result["counts"],
                            "observation_count": 33,
                            "primary_gate_passed": True,
                            "normal_control": sample["observed_summary"][
                                "normal_control"
                            ],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        report.update(status="complete", criterion_passed=True)
        report["evidence_manifest"] = manifest(output)
        dump(target, report)
        print(
            "CANONICAL_FIDELITY_COMPLETE "
            + json.dumps(
                {
                    "stage": stage,
                    "criterion_passed": True,
                    "sources": report["sources"],
                    "note": "Automated actual-byte/record checks; independent actual-artifact audit is separate. Python integer-safe JSON, no JavaScript numeric parsing.",
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report.update(
            status="stopped",
            error_type=type(exc).__name__,
            error=str(exc),
            partial_file_manifest=manifest(output),
        )
        dump(target, report)
        print(
            "CANONICAL_FIDELITY_STOPPED "
            + json.dumps(
                {"stage": stage, "type": type(exc).__name__, "error": str(exc)}
            ),
            file=sys.stderr,
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("baseline", "study", "verify"))
    parser.add_argument("--candidate")
    parser.add_argument("--baseline-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-pins", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root is not None:
        if args.worker_output is None or args.worker_pins is None:
            parser.error("worker requires output/source pins")
        return worker(
            args.worker_root.resolve(),
            args.worker_output.resolve(),
            args.worker_pins.resolve(),
        )
    if (
        args.stage is None
        or args.output is None
        or (args.stage == "study") != (args.baseline_evidence is not None)
        or (args.stage in ("study", "verify")) != (args.candidate is not None)
    ):
        parser.error(
            "require --stage/--output, --candidate for study/verify and --baseline-evidence only for study"
        )
    return reproduce(
        args.stage,
        args.candidate,
        args.baseline_evidence.resolve() if args.baseline_evidence else None,
        args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
