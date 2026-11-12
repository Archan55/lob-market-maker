"""Bounded archived PR9/candidate LOBSTER fidelity reproduction.

One pytest worker per source records the frozen 36 public observations and one
normal conversion. Study rehashes/reuses baseline evidence. Verify repeats both
sources for CI. No controller call invokes an adapter or the independent oracle.
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
BASELINE = "253abee735c02d7805e2a4377c12626742108d84"
PREFIX = "docs/evidence/lobster-fidelity-20261112/"
CONTRACT, FIXTURES = PREFIX + "contract.json", PREFIX + "fixtures.json"
REVIEW = PREFIX + "design-review.json"
TEST = "tests/unit/test_lobster_fidelity_independent.py"
CONTRACT_SHA = "513f9b2be85b582c020e575b32dded6df123f10d0aaeee4e51d7aceed755de8b"
FIXTURES_SHA = "1987016a419c065535b2ac5590305e1966328c39e495feda8eb487dbf2dbf670"
REVIEW_SHA = "215f4f923ad4f2868cc23b2d3344cee3cf12a4fefa98264490d6c393ac960936"
TEST_SHA = "a29f7842ff5565f7a9f80b46544333208684a251fc192cbd19c756dad6e76906"
ALLOWED = "src/lobmm/data/lobster.py"
CAP_SECONDS = 120
HISTORICAL = (
    "docs/evidence/exact-ingress-20261111",
    "docs/EXACT_INGRESS_CONVERSION.md",
    "tests/unit/test_exact_ingress_independent.py",
    "tools/reproduce_exact_ingress.py",
    "tests/fixtures/adapters",
)


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
    }
    for name, sha in pins.items():
        if len(sha) != 64 or digest(root / name) != sha:
            raise RuntimeError("unpinned/changed new frozen input: " + name)
    contract = json.loads((root / CONTRACT).read_text())
    pins.update(contract["control_input_sha256"])
    if any(digest(root / name) != sha for name, sha in pins.items()):
        raise RuntimeError("normal-control source bytes changed")
    sources = contract["source_sha256"]
    if len(sources) != 38 or ALLOWED not in sources:
        raise RuntimeError("unexpected production source inventory")
    return pins, sources


def historical_pins() -> dict[str, str]:
    """Hash historical PR9 bytes, without interpreting its publication manifest."""
    content = subprocess.check_output(
        ["git", "archive", BASELINE, *HISTORICAL], cwd=ROOT
    )
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        return {
            member.name: hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
            for member in bundle.getmembers()
            if member.isfile()
        }


def archive(
    revision: str, root: Path, saved: Path, history: dict[str, str]
) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if resolved != revision:
        raise RuntimeError("explicit complete commit SHA required")
    content = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        bundle.extractall(root, filter="data")
    pins, expected = frozen(ROOT)
    if {
        str(p.relative_to(root)) for p in (root / "src").rglob("*") if p.is_file()
    } != expected.keys():
        raise RuntimeError("source inventory differs from the 38 frozen files")
    for name, sha in history.items():
        if digest(root / name) != sha:
            raise RuntimeError("historical PR9 input/evidence changed: " + name)
    hashes = {}
    for name in expected:
        hashes[name] = digest(root / name)
        target = saved / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    for name, sha in pins.items():
        target = root / name
        if target.exists() and digest(target) != sha:
            raise RuntimeError("archive changed a frozen input: " + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        target = saved / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return {
        "resolved_revision": resolved,
        "git_archive_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": hashes,
        "historical_pr9_manifest_sha256": object_hash(history),
    }


def imports(root: Path, expected: dict[str, str]) -> dict[str, Any]:
    identities = {}
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
                raise RuntimeError("wrong editable/imported source: " + name)
        identities[name] = {"path": str(path.resolve()), "sha256": sha}
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if (
            (name == "lobmm" or name.startswith("lobmm."))
            and path
            and not Path(path).resolve().is_relative_to(root / "src")
        ):
            raise RuntimeError("foreign lobmm import: " + name)
    return identities


def worker(root: Path, output: Path, source_pins: Path, history_pins: Path) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled interpreter required")
    sys.path.insert(0, str(root / "src"))
    import pytest

    expected = json.loads(source_pins.read_text())
    evidence = output.parent / (output.stem + "-evidence")
    evidence.mkdir()
    os.environ["LOB_LOBSTER_FIDELITY_EVIDENCE_DIR"] = str(evidence)
    record: dict[str, Any] = {
        "status": "in_progress",
        "nodes": [],
        "reports": [],
        "exceptions": [],
        "environment": environment(),
        "evidence_directory": evidence.name,
    }
    dump(output, record)
    record["imports_before_collection"] = imports(root, expected)

    class Audit:
        def pytest_collection_finish(self, session: Any) -> None:
            record["nodes"] = [item.nodeid for item in session.items]
            record["imports_after_collection"] = imports(root, expected)
            dump(output, record)

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_makereport(self, item: Any, call: Any) -> None:
            if call.when == "call" and call.excinfo is not None:
                record["exceptions"].append(
                    {
                        "nodeid": item.nodeid,
                        "type": call.excinfo.type.__name__,
                        "message": str(call.excinfo.value),
                    }
                )

        def pytest_runtest_logreport(self, report: Any) -> None:
            record["reports"].append(
                {
                    "nodeid": report.nodeid,
                    "when": report.when,
                    "outcome": report.outcome,
                    "longrepr": report.longreprtext
                    if report.failed or report.skipped
                    else None,
                }
            )
            dump(output, record)

    record["pytest_exit_code"] = int(
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
    record["imports_after_execution"] = imports(root, expected)
    record["frozen_pins_after_execution"] = frozen(root)[0]
    history = json.loads(history_pins.read_text())
    if any(digest(root / name) != sha for name, sha in history.items()):
        raise RuntimeError("worker changed historical PR9 tests/inputs/evidence")
    record["historical_pr9_manifest_sha256_after"] = object_hash(history)
    record["environment_after"] = environment()
    calls = [row for row in record["reports"] if row["when"] == "call"]
    record["counts"] = {
        "collected": len(record["nodes"]),
        "passed": sum(x["outcome"] == "passed" for x in calls),
        "failed": sum(x["outcome"] == "failed" for x in calls),
        "errors": sum(
            x["when"] != "call" and x["outcome"] == "failed" for x in record["reports"]
        ),
        "skipped": sum(x["outcome"] == "skipped" for x in record["reports"]),
    }
    record["status"] = "complete"
    dump(output, record)
    return 0


def files(directory: Path) -> dict[str, Any]:
    return {
        str(p.relative_to(directory)): {"sha256": digest(p), "bytes": p.stat().st_size}
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p != directory / "report.json"
    }


def rehash(directory: Path, manifest: dict[str, Any]) -> None:
    for name, info in manifest.items():
        path = directory / name
        if digest(path) != info["sha256"] or path.stat().st_size != info["bytes"]:
            raise RuntimeError("retained evidence changed: " + name)


def strict_certificate(value: dict[str, Any]) -> bool:
    return (
        isinstance(value, dict)
        and value.get("valid") is True
        and value.get("mode") == "strict"
        and value.get("reconstructed_book") is True
        and value.get("required_nonempty") is True
        and value.get("issue_count") == 0
    )


def worker_identity(
    result: dict[str, Any], expected: dict[str, str], history: dict[str, str]
) -> None:
    for phase in (
        "imports_before_collection",
        "imports_after_collection",
        "imports_after_execution",
    ):
        proof = result[phase]
        if (
            set(proof) != set(expected)
            or len({v["path"].split("/src/lobmm/")[0] for v in proof.values()}) != 1
        ):
            raise RuntimeError("incomplete/mixed worker source identities")
        for name, sha in expected.items():
            if proof[name]["sha256"] != sha or not proof[name]["path"].endswith(
                "/" + name
            ):
                raise RuntimeError("wrong imported source hash/path: " + name)
    if (
        result["frozen_pins_after_execution"] != frozen(ROOT)[0]
        or result["historical_pr9_manifest_sha256_after"] != object_hash(history)
        or result["environment"]["integer_string_guard"]
        != result["environment_after"]["integer_string_guard"]
    ):
        raise RuntimeError("worker modified a frozen input or integer guard")


def ledgers(
    result: dict[str, Any], directory: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence = directory / result["evidence_directory"]
    if not evidence.resolve().is_relative_to(directory.resolve()):
        raise RuntimeError("evidence path escapes the result directory")
    spec = json.loads((ROOT / FIXTURES).read_text())
    groups = [
        json.loads((evidence / "groups" / (profile["id"] + ".json")).read_text())
        for profile in spec["profiles"]
    ]
    normal = json.loads((evidence / "normal-controls.json").read_text())
    return groups, normal


def compact(groups: list[dict[str, Any]], normal: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for group in groups:
        for row in group.get("observations", []):
            canonical = row.get("canonical")
            cases.append(
                {
                    "profile": group["profile"],
                    "case": row["case_id"],
                    "status": row["status"],
                    "actual_rows": canonical["rows"] if canonical else None,
                    "expected_status": row["expected"]["status"],
                    "expected_rows": row["expected"].get("rows"),
                    "actual_error": row["error"],
                    "expected_error_reason": row["expected"].get("error_reason"),
                    "diagnostics": canonical["diagnostics"] if canonical else None,
                    "failed_checks": [
                        k for k, v in row["checks"].items() if v is not True
                    ],
                }
            )
    controls = []
    for row in normal.get("observations", []):
        controls.append(
            {
                "id": row.get("id"),
                "status": row["status"],
                "parquet_sha256": row.get("parquet_sha256"),
                "row_count": len(row.get("rows", [])),
                "event_stream_sha256": row.get("event_stream_sha256"),
                "comparison_sha256": object_hash(
                    {
                        k: row.get(k)
                        for k in (
                            "rows",
                            "schema",
                            "event_stream_sha256",
                            "diagnostics",
                            "validation",
                            "input_sha256",
                        )
                    }
                ),
            }
        )
    return {"case_ledger": cases, "normal_controls": controls}


def validate(result: dict[str, Any], role: str, directory: Path) -> dict[str, Any]:
    spec = json.loads((ROOT / FIXTURES).read_text())
    count = result["counts"]
    if (
        result["status"] != "complete"
        or result["pytest_exit_code"] not in (0, 1)
        or count["errors"]
        or count["skipped"]
        or len(result["nodes"]) != len(spec["profiles"]) + 1
        or len(set(result["nodes"])) != len(result["nodes"])
        or count["passed"] + count["failed"] != len(result["nodes"])
        or count["collected"] != len(result["nodes"])
    ):
        raise RuntimeError("corpus did not execute fully without errors/skips")
    if role == "candidate" and (result["pytest_exit_code"] != 0 or count["failed"]):
        raise RuntimeError("candidate failed the unchanged independent corpus")
    evidence = directory / result["evidence_directory"]
    groups, normal = ledgers(result, directory)
    actual = {}
    for group, profile in zip(groups, spec["profiles"], strict=True):
        if (
            not group["complete"]
            or group["profile_spec"] != profile
            or group["profile"] != profile["id"]
            or [x["case_id"] for x in group["observations"]]
            != [x["id"] for x in spec["cases"]]
        ):
            raise RuntimeError("profile/case ledger differs from the frozen fixture")
        for row in group["observations"]:
            label = profile["id"] + ":" + row["case_id"]
            if row.get("infrastructure_error") or not row["checks"]:
                raise RuntimeError("missing/infrastructure observation: " + label)
            for name, sha in row["csv_sha256"].items():
                if digest(evidence / row["csv_paths"][name]) != sha:
                    raise RuntimeError("actual owned CSV changed: " + label)
            if row["status"] == "admitted":
                value = row["canonical"]
                if (
                    row["error"] is not None
                    or not strict_certificate(value["validation"])
                    or value["dictionary_reconstruction"]["valid"] is not True
                    or digest(evidence / value["parquet_path"])
                    != value["parquet_sha256"]
                ):
                    raise RuntimeError(
                        "admitted stream lacks actual bytes/strict certification: "
                        + label
                    )
            if role == "candidate" and (
                not all(v is True for v in row["checks"].values())
                or row["context_before"] != row["context_after"]
                or row["outer_context_before"] != row["outer_context_after"]
                or row["guard_before"] != row["guard_after"]
            ):
                raise RuntimeError(
                    "candidate row/type/cause/time/error/raw/context mismatch: " + label
                )
            actual[(profile["id"], row["case_id"])] = row
    if len(actual) != 36:
        raise RuntimeError("incomplete fixed public observation ledger")
    if role == "pr9":
        for name, column in (
            ("price_pair", "price_ticks"),
            ("time_pair", "timestamp_ns"),
        ):
            low, ordinary = actual[("reduced3", name)], actual[("default28", name)]
            if (
                low["status"] != "admitted"
                or low["expected"]["status"] != "admit"
                or [x[column] for x in low["canonical"]["rows"]]
                == [x[column] for x in low["expected"]["rows"]]
                or ordinary["status"] != "admitted"
                or ordinary["canonical"]["rows"] != ordinary["expected"]["rows"]
            ):
                raise RuntimeError(
                    "predicted valid-format mismatch/default control absent: " + name
                )
        invalid = actual[("default28", "subns_long")]
        if invalid["expected"]["status"] != "reject" or invalid["status"] != "admitted":
            raise RuntimeError("predicted invalid-source default28 admission absent")
        if result["pytest_exit_code"] != 1 or not count["failed"]:
            raise RuntimeError("baseline did not expose a semantic mismatch")
    if not normal["complete"] or len(normal["observations"]) != 1:
        raise RuntimeError("one normal control required")
    row = normal["observations"][0]
    if (
        row["status"] != "admitted"
        or row["error"] is not None
        or not strict_certificate(row["validation"])
        or digest(evidence / row["parquet_path"]) != row["parquet_sha256"]
        or len(row["rows"]) != 17
    ):
        raise RuntimeError("ordinary 17-row control failed actual-byte/strict gate")
    control_pins = json.loads((ROOT / CONTRACT).read_text())["control_input_sha256"]
    if row["input_sha256"] != control_pins:
        raise RuntimeError("ordinary original input hashes differ")
    for name, sha in row["csv_sha256"].items():
        if (
            digest(evidence / row["csv_paths"][name]) != sha
            or sha != control_pins[spec["normal_control"][name]]
        ):
            raise RuntimeError("ordinary copied CSV bytes differ: " + name)
    comparison = {
        k: row[k]
        for k in (
            "parquet_sha256",
            "rows",
            "schema",
            "event_stream_sha256",
            "diagnostics",
            "validation",
            "input_sha256",
        )
    }
    return {
        "observation_count": len(actual),
        "declared_cases": [a + ":" + b for a, b in actual],
        "declared_nodes": result["nodes"],
        "primary_gate_passed": True,
        "normal_control": comparison,
    }


def baseline_evidence(
    directory: Path, history: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = json.loads((directory / "report.json").read_text())
    pins, sources = frozen(ROOT)
    if (
        report["status"] != "complete"
        or report["stage"] != "baseline"
        or not report["criterion_passed"]
        or report["pins"] != pins
        or report["harness_sha256"] != digest(Path(__file__).resolve())
        or report["historical_pr9_files"] != history
        or set(report["sources"]) != {"pr9"}
        or len(report["observations"]) != 1
    ):
        raise RuntimeError("baseline is not complete frozen PR9 evidence")
    sample = report["observations"][0]
    if (
        sample["role"] != "pr9"
        or sample["status"] != "complete"
        or sample["resolved_revision"] != BASELINE
        or sample["source_sha256"] != sources
    ):
        raise RuntimeError("baseline did not execute one pinned PR9 source")
    rehash(directory, report["evidence_manifest"])
    if json.loads((directory / "pr9.json").read_text()) != sample["result"]:
        raise RuntimeError("raw baseline worker record differs")
    worker_identity(sample["result"], sources, history)
    if any(
        sample[key] != report["sources"]["pr9"][key]
        for key in (
            "resolved_revision",
            "source_sha256",
            "git_archive_sha256",
            "historical_pr9_manifest_sha256",
        )
    ):
        raise RuntimeError("baseline worker/source metadata differ")
    if (
        hashlib.sha256(
            subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
        ).hexdigest()
        != sample["git_archive_sha256"]
    ):
        raise RuntimeError("baseline Git archive hash differs")
    if validate(sample["result"], "pr9", directory) != sample["criteria"]:
        raise RuntimeError("baseline criteria differ from its actual saved records")
    return sample, report["sources"]["pr9"]


def reproduce(
    stage: str, candidate: str | None, reuse: Path | None, output: Path
) -> int:
    if not __debug__ or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise RuntimeError("require assertions and a new/empty output directory")
    pins, expected = frozen(ROOT)
    history = historical_pins()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, output / "reproducer.py")
    for name in pins:
        target = output / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    dump(output / "environment.json", environment())
    history_path = output / "historical-pr9-source-pins.json"
    dump(history_path, history)
    report: dict[str, Any] = {
        "status": "in_progress",
        "stage": stage,
        "pins": pins,
        "harness_sha256": digest(Path(__file__).resolve()),
        "historical_pr9_files": history,
        "process_cap_seconds": CAP_SECONDS,
        "sources": {},
        "observations": [],
        "criterion_passed": False,
        "scope": "Original fictional LOBSTER-format fidelity cases and one exposed ordinary control; no real data/economic/performance result.",
    }
    target = output / "report.json"
    dump(target, report)
    try:
        previous, old_source = (
            baseline_evidence(reuse, history) if reuse else (None, None)
        )
        if reuse:
            report["reused_baseline_evidence"] = {
                "directory": str(reuse),
                "report_sha256": digest(reuse / "report.json"),
            }
            report["sources"]["pr9"] = old_source
        plan = [("pr9", BASELINE)] if stage in ("baseline", "verify") else []
        if stage in ("study", "verify"):
            plan.append(("candidate", candidate))
        with tempfile.TemporaryDirectory(prefix="lob-lobster-fidelity-") as temporary:
            for role, revision in plan:
                sample: dict[str, Any] = {"role": role, "status": "in_progress"}
                report["observations"].append(sample)
                dump(target, report)
                source_root = Path(temporary) / role
                source = archive(
                    revision, source_root, output / "sources" / role, history
                )
                for name, sha in source["source_sha256"].items():
                    if (role == "pr9" or name != ALLOWED) and sha != expected[name]:
                        raise RuntimeError("unapproved production difference: " + name)
                sample.update(source)
                report["sources"][role] = source
                pin_path = output / (role + "-source-pins.json")
                dump(pin_path, source["source_sha256"])
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
                    str(pin_path),
                    "--worker-history",
                    str(history_path),
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
                            x.decode(errors="replace")
                            if isinstance(x, bytes)
                            else x or ""
                            for x in (exc.stdout, exc.stderr)
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
                groups, normal = ledgers(result, output)
                sample["observed_summary"] = compact(groups, normal)
                dump(target, report)
                print(
                    "LOBSTER_FIDELITY_OBSERVATIONS "
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
                worker_identity(result, source["source_sha256"], history)
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
                            "candidate nodes/cases or ordinary exact-byte/row/schema/fingerprint/diagnostic/certificate control differ"
                        )
                else:
                    previous = sample
                sample["status"] = "complete"
                dump(target, report)
                print(
                    "LOBSTER_FIDELITY_WORKER "
                    + json.dumps(
                        {
                            "role": role,
                            "revision": revision,
                            "counts": result["counts"],
                            "observation_count": sample["criteria"][
                                "observation_count"
                            ],
                            "primary_gate_passed": True,
                            "normal_controls": sample["observed_summary"][
                                "normal_controls"
                            ],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        report.update(status="complete", criterion_passed=True)
        report["evidence_manifest"] = files(output)
        dump(target, report)
        print(
            "LOBSTER_FIDELITY_COMPLETE "
            + json.dumps(
                {
                    "stage": stage,
                    "criterion_passed": True,
                    "sources": report["sources"],
                    "historical_pr9_manifest_sha256": object_hash(history),
                    "note": "Automated retained-byte/record checks; independent actual-Parquet audit is separate.",
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
            partial_file_manifest=files(output),
        )
        dump(target, report)
        print(
            "LOBSTER_FIDELITY_STOPPED "
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
    parser.add_argument("--worker-history", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root is not None:
        if (
            args.worker_output is None
            or args.worker_pins is None
            or args.worker_history is None
        ):
            parser.error("worker requires output/source pins")
        return worker(
            args.worker_root.resolve(),
            args.worker_output.resolve(),
            args.worker_pins.resolve(),
            args.worker_history.resolve(),
        )
    if (
        args.stage is None
        or args.output is None
        or (args.stage == "study") != (args.baseline_evidence is not None)
        or (args.stage in ("study", "verify")) != (args.candidate is not None)
    ):
        parser.error(
            "require --stage/--output, --candidate for study/verify, --baseline-evidence only for study"
        )
    return reproduce(
        args.stage,
        args.candidate,
        args.baseline_evidence.resolve() if args.baseline_evidence else None,
        args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
