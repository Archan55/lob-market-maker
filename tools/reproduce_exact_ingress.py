"""Pinned, bounded strict-ingress reproduction; no backtests or data acquisition.

Baseline executes main once. Study rehashes and reuses that complete evidence,
then executes one candidate worker. Verify explicitly repeats both for CI.
Only pytest invokes the frozen observations and adapter controls.
"""

from __future__ import annotations

import argparse
import ast
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
PREFIX = "docs/evidence/exact-ingress-20261111/"
CONTRACT = PREFIX + "contract.json"
FIXTURES = PREFIX + "fixtures.json"
DESIGN_REVIEW = PREFIX + "design-review.json"
TEST = "tests/unit/test_exact_ingress_independent.py"
CONTRACT_SHA = "f28639e107b711ea9c289a22701a1e03275c729049d79b2afafe1f525276d71b"
FIXTURES_SHA = "dbc8f57942b24a94a9fd67816d0e9d350c12cdfe35f40e35b2e5d1c545962144"
DESIGN_REVIEW_SHA = "e22e97dc1c99d4c907a2fd1903555f276524d80a48e0903cbd757972f0eeefd6"
TEST_SHA = "ca2d76e7336206af5dff46e935621c7ede5211757baee47dce25ebbcd168a8e7"
ALLOWED = {"src/lobmm/types.py", "src/lobmm/data/_adapter_utils.py"}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        "ruff",
        "mypy",
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
        "assertions_enabled": __debug__,
        "integer_string_guard": sys.get_int_max_str_digits(),
    }


def frozen(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    pins = {
        CONTRACT: CONTRACT_SHA,
        FIXTURES: FIXTURES_SHA,
        DESIGN_REVIEW: DESIGN_REVIEW_SHA,
        TEST: TEST_SHA,
    }
    for name, sha in pins.items():
        if len(sha) != 64 or digest(root / name) != sha:
            raise RuntimeError("unpinned or changed frozen file: " + name)
    contract = json.loads((root / CONTRACT).read_text())
    pins.update(contract["normal_fixture_sha256"])
    for name, sha in pins.items():
        if digest(root / name) != sha:
            raise RuntimeError("frozen input changed: " + name)
    sources = contract["source_sha256"]
    if len(sources) != 38 or not sources.keys() >= ALLOWED:
        raise RuntimeError("unexpected frozen source inventory")
    return pins, sources


def archive(revision: str, root: Path, retained: Path) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"],
        cwd=ROOT,
        text=True,
    ).strip()
    if resolved != revision:
        raise RuntimeError("use an explicit complete commit SHA")
    content = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        bundle.extractall(root, filter="data")
    pins, baseline_sources = frozen(ROOT)
    actual_paths = {
        str(path.relative_to(root))
        for path in (root / "src").rglob("*")
        if path.is_file()
    }
    if actual_paths != baseline_sources.keys():
        raise RuntimeError("archive source-file inventory differs from frozen main")
    hashes = {}
    for name in baseline_sources:
        target = retained / name
        target.parent.mkdir(parents=True, exist_ok=True)
        hashes[name] = digest(root / name)
        shutil.copy2(root / name, target)
    for name, sha in pins.items():
        target = root / name
        if target.exists() and digest(target) != sha:
            raise RuntimeError("archive contains a changed frozen input: " + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        saved = retained / "frozen" / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, saved)
    frozen(root)
    return {
        "resolved_revision": resolved,
        "git_archive_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": hashes,
        "unchanged_api_sha256": unchanged_api_hashes(root),
    }


def unchanged_api_hashes(root: Path) -> dict[str, str]:
    text = (root / "src/lobmm/types.py").read_text()
    tree = ast.parse(text)
    result = {
        node.name: hashlib.sha256(
            ast.get_source_segment(text, node).encode()
        ).hexdigest()
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("floor_price_to_ticks", "ticks_to_price")
    }
    if set(result) != {"floor_price_to_ticks", "ticks_to_price"}:
        raise RuntimeError("legacy conversion functions missing from source")
    return result


def import_identity(root: Path, expected: dict[str, str]) -> dict[str, Any]:
    result = {}
    for name, sha in expected.items():
        path = root / name
        if digest(path) != sha:
            raise RuntimeError("worker source bytes changed: " + name)
        if path.suffix == ".py":
            parts = list(Path(name).with_suffix("").parts[1:])
            if parts[-1] == "__init__":
                parts.pop()
            module = importlib.import_module(".".join(parts))
            imported = Path(module.__file__).resolve()
            if imported != path.resolve() or digest(imported) != sha:
                raise RuntimeError("wrong imported source: " + name)
            result[name] = {"path": str(imported), "sha256": sha}
        else:
            result[name] = {"path": str(path.resolve()), "sha256": sha}
    for name, module in tuple(sys.modules.items()):
        if name == "lobmm" or name.startswith("lobmm."):
            path = getattr(module, "__file__", None)
            if path and not Path(path).resolve().is_relative_to(root / "src"):
                raise RuntimeError(
                    "editable-package source leaked into worker: " + name
                )
    return result


def worker(root: Path, output: Path, pins_path: Path) -> int:
    import pytest

    if not __debug__:
        raise RuntimeError("assertion-enabled Python required")
    source_pins = json.loads(pins_path.read_text())
    sys.path.insert(0, str(root / "src"))
    evidence = output.parent / (output.stem + "-evidence")
    evidence.mkdir()
    os.environ["LOB_INGRESS_EVIDENCE_DIR"] = str(evidence)
    record: dict[str, Any] = {
        "status": "in_progress",
        "environment": environment(),
        "nodes": [],
        "reports": [],
        "exceptions": [],
        "evidence_directory": evidence.name,
        "imports_before_collection": import_identity(root, source_pins),
    }
    dump(output, record)

    class Audit:
        def pytest_collection_finish(self, session: Any) -> None:
            record["nodes"] = [item.nodeid for item in session.items]
            record["imports_after_collection"] = import_identity(root, source_pins)
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
    record["imports_after_execution"] = import_identity(root, source_pins)
    record["frozen_pins_after_execution"] = frozen(root)[0]
    record["environment_after"] = environment()
    calls = [row for row in record["reports"] if row["when"] == "call"]
    record["counts"] = {
        "collected": len(record["nodes"]),
        "passed": sum(row["outcome"] == "passed" for row in calls),
        "failed": sum(row["outcome"] == "failed" for row in calls),
        "errors": sum(
            row["when"] != "call" and row["outcome"] == "failed"
            for row in record["reports"]
        ),
        "skipped": sum(row["outcome"] == "skipped" for row in record["reports"]),
    }
    record["status"] = "complete"
    dump(output, record)
    return 0


def file_manifest(directory: Path) -> dict[str, dict[str, Any]]:
    return {
        str(path.relative_to(directory)): {
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def validate_saved(sample: dict[str, Any], directory: Path) -> None:
    for name, metadata in sample["files"].items():
        path = directory / name
        if (
            digest(path) != metadata["sha256"]
            or path.stat().st_size != metadata["bytes"]
        ):
            raise RuntimeError("saved evidence changed: " + str(path))


def validate_worker_identity(
    result: dict[str, Any], source_pins: dict[str, str]
) -> None:
    if result["frozen_pins_after_execution"] != frozen(ROOT)[0]:
        raise RuntimeError("worker changed a frozen test/input file")
    for key in (
        "imports_before_collection",
        "imports_after_collection",
        "imports_after_execution",
    ):
        imports = result[key]
        if set(imports) != set(source_pins):
            raise RuntimeError("incomplete worker import/source identity")
        for name, sha in source_pins.items():
            if imports[name]["sha256"] != sha or not imports[name]["path"].endswith(
                "/" + name
            ):
                raise RuntimeError("worker import/source identity differs: " + name)
        if len({row["path"].split("/src/lobmm/")[0] for row in imports.values()}) != 1:
            raise RuntimeError("worker imported from multiple source roots")
    if (
        result["environment"]["integer_string_guard"]
        != result["environment_after"]["integer_string_guard"]
    ):
        raise RuntimeError("worker changed the integer string guard")


def observed_summary(result: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Summarize existing records before applying gates, including failures."""
    evidence = directory / result["evidence_directory"]
    cases = []
    for path in sorted((evidence / "groups").glob("*.json")):
        group = json.loads(path.read_text())
        for row in group.get("observations", []):
            checks = row.get("checks", {})
            actual = row.get("result")
            if row["kind"] == "csv" and isinstance(actual, dict):
                actual = {
                    "row_count": len(actual["rows"]),
                    "parquet_sha256": actual["parquet_sha256"],
                    "event_stream_sha256": actual["event_stream_sha256"],
                }
            cases.append(
                {
                    "profile": group["profile"],
                    "kind": row["kind"],
                    "case": row["case_id"],
                    "status": row.get("status"),
                    "result": actual,
                    "error": row.get("error"),
                    "policy_pass": checks.get("oracle_match"),
                    "context_pass": all(
                        checks.get(name) is True
                        for name in (
                            "context_preserved",
                            "outer_context_preserved",
                            "guard_preserved",
                        )
                    ),
                    "failed_checks": [
                        name for name, value in checks.items() if value is not True
                    ],
                }
            )
    normals = []
    path = evidence / "normal-controls.json"
    if path.exists():
        for row in json.loads(path.read_text()).get("observations", []):
            comparison = {
                name: row.get(name)
                for name in (
                    "rows",
                    "schema",
                    "event_stream_sha256",
                    "diagnostics",
                    "validation",
                    "input_sha256",
                )
            }
            normals.append(
                {
                    "id": row["id"],
                    "status": row.get("status"),
                    "parquet_sha256": row.get("parquet_sha256"),
                    "row_count": len(row.get("rows", [])),
                    "event_stream_sha256": row.get("event_stream_sha256"),
                    "validation": row.get("validation"),
                    "comparison_sha256": hashlib.sha256(
                        json.dumps(
                            comparison, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
    return {
        "counts": result.get("counts"),
        "declared_nodes": result["nodes"],
        "failed_nodes": [
            row["nodeid"]
            for row in result["reports"]
            if row["when"] == "call" and row["outcome"] == "failed"
        ],
        "observed_case_count": len(cases),
        "case_ledger": cases,
        "normal_controls": normals,
    }


def validate_observations(
    result: dict[str, Any], role: str, directory: Path
) -> dict[str, Any]:
    """Read the frozen tests' durable ledger; never invoke their helpers again."""
    counts = result["counts"]
    if (
        result["status"] != "complete"
        or result["pytest_exit_code"] not in (0, 1)
        or counts["errors"]
        or counts["skipped"]
        or not result["nodes"]
        or len(set(result["nodes"])) != len(result["nodes"])
        or counts["passed"] + counts["failed"] != len(result["nodes"])
    ):
        raise RuntimeError("frozen corpus did not execute fully without errors/skips")
    if role == "candidate" and (result["pytest_exit_code"] != 0 or counts["failed"]):
        raise RuntimeError("candidate failed the unchanged frozen corpus")
    evidence = directory / result["evidence_directory"]
    if not evidence.resolve().is_relative_to(directory.resolve()):
        raise RuntimeError("evidence directory escapes the retained result")
    fixtures = json.loads((ROOT / FIXTURES).read_text())
    if (
        counts["collected"] != len(result["nodes"])
        or len(result["nodes"]) != len(fixtures["profiles"]) + 2
    ):
        raise RuntimeError("expected the six context nodes and normal/legacy nodes")
    expected_cases = [
        (kind, row["id"])
        for kind in ("price", "quantity", "csv")
        for row in fixtures[kind + "_cases"]
    ]
    declared = []
    case_ledger = []
    primary = {}

    def strict_certificate(value: dict[str, Any]) -> bool:
        return (
            value.get("valid") is True
            and value.get("mode") == "strict"
            and value.get("reconstructed_book") is True
            and value.get("required_nonempty") is True
            and value.get("issue_count") == 0
        )

    for profile in fixtures["profiles"]:
        group = json.loads(
            (evidence / "groups" / (profile["id"] + ".json")).read_text()
        )
        observations = group["observations"]
        if (
            not group["complete"]
            or group["profile"] != profile["id"]
            or group["profile_spec"] != profile
            or [(row["kind"], row["case_id"]) for row in observations] != expected_cases
        ):
            raise RuntimeError("fixed observation/profile ledger differs")
        for row in observations:
            label = profile["id"] + ":" + row["kind"] + ":" + row["case_id"]
            declared.append(label)
            if row.get("infrastructure_error"):
                raise RuntimeError("observation infrastructure failure: " + label)
            checks = row["checks"]
            if not checks:
                raise RuntimeError("observation has no frozen checks: " + label)
            failed = [name for name, value in checks.items() if value is not True]
            if role == "candidate" and (
                failed
                or row["context_before"] != row["context_after"]
                or row["outer_context_before"] != row["outer_context_after"]
            ):
                raise RuntimeError("candidate observation mismatch: " + label)
            case_ledger.append(
                {
                    "id": label,
                    "status": row["status"],
                    "result": row.get("result"),
                    "error_type": row["error"]["type"] if row["error"] else None,
                    "failed_checks": failed,
                }
            )
            if row["kind"] == "csv":
                for name, sha in row["csv_sha256"].items():
                    if digest(evidence / row["csv_paths"][name]) != sha:
                        raise RuntimeError(
                            "actual synthetic CSV bytes changed: " + label
                        )
                if row["status"] == "admitted":
                    canonical = row["canonical"]
                    if not strict_certificate(canonical["validation"]):
                        raise RuntimeError(
                            "admitted CSV lacks a strict certificate: " + label
                        )
                    if (
                        digest(evidence / canonical["parquet_path"])
                        != canonical["parquet_sha256"]
                    ):
                        raise RuntimeError(
                            "admitted CSV Parquet bytes changed: " + label
                        )
            if profile["id"] == "default28":
                if (
                    row["kind"] in ("price", "quantity")
                    and row["case_id"] == "off_tail_above"
                ):
                    primary[row["kind"]] = row
                if row["kind"] == "csv" and row["case_id"] in (
                    "book_off_price",
                    "book_off_quantity",
                    "trade_off_price",
                    "trade_off_quantity",
                ):
                    primary[row["case_id"]] = row
    if len(declared) != 240 or len(primary) != 6:
        raise RuntimeError("incomplete 240-observation or six-primary ledger")
    if role == "main":
        for kind in ("price", "quantity"):
            row = primary[kind]
            if row["result"] != 1 or row["error"] is not None:
                raise RuntimeError(
                    "predicted main strict scalar false admission absent: " + kind
                )
        for kind in (
            "book_off_price",
            "book_off_quantity",
            "trade_off_price",
            "trade_off_quantity",
        ):
            row = primary[kind]
            literal = next(
                case["literal"] for case in fixtures["csv_cases"] if case["id"] == kind
            )
            if (
                row["status"] != "admitted"
                or row["error"] is not None
                or row["strict_valid"] is not True
                or row["rawliteral"]["preserved"] is not True
                or row["rawliteral"]["expected"] != literal
                or not any(
                    entry["value"] == literal and entry["type"] == "str"
                    for entry in row["rawliteral"]["observed"]
                )
            ):
                raise RuntimeError(
                    "predicted main public CSV false admission absent: " + kind
                )
        if not counts["failed"] or result["pytest_exit_code"] != 1:
            raise RuntimeError("main did not expose any frozen semantic mismatch")
    normal = json.loads((evidence / "normal-controls.json").read_text())
    if not normal["complete"]:
        raise RuntimeError("normal controls did not complete")
    normals = {}
    if [row["id"] for row in normal["observations"]] != [
        row["id"] for row in fixtures["normal_controls"]
    ]:
        raise RuntimeError("normal control ledger differs")
    for row in normal["observations"]:
        if (
            row["status"] != "admitted"
            or row["error"] is not None
            or not strict_certificate(row["validation"])
        ):
            raise RuntimeError("normal adapter did not strict-validate: " + row["id"])
        if digest(evidence / row["parquet_path"]) != row["parquet_sha256"]:
            raise RuntimeError("actual normal Parquet bytes changed: " + row["id"])
        normals[row["id"]] = {
            key: row[key]
            for key in (
                "parquet_sha256",
                "rows",
                "schema",
                "event_stream_sha256",
                "diagnostics",
                "validation",
                "input_sha256",
            )
        }
    legacy = json.loads((evidence / "legacy-controls.json").read_text())
    if (
        not legacy["complete"]
        or not legacy["checks"]
        or not all(legacy["checks"].values())
    ):
        raise RuntimeError("unchanged legacy conversion API control failed")
    return {
        "observation_count": len(declared),
        "declared_cases": declared,
        "declared_nodes": result["nodes"],
        "case_ledger": case_ledger,
        "failed_observations": [
            row["id"] for row in case_ledger if row["failed_checks"]
        ],
        "primary_gate_passed": True,
        "normal_controls": normals,
        "legacy_controls": legacy,
    }


def compare(main: dict[str, Any], candidate: dict[str, Any]) -> None:
    if main["result"]["nodes"] != candidate["result"]["nodes"]:
        raise RuntimeError("sources collected different frozen test nodes")
    if main["criteria"]["normal_controls"] != candidate["criteria"]["normal_controls"]:
        raise RuntimeError(
            "normal adapter bytes/rows/schema/fingerprint/diagnostics/certificate changed"
        )
    if main["criteria"]["declared_cases"] != candidate["criteria"]["declared_cases"]:
        raise RuntimeError("sources executed different fixed observation IDs")
    if main["unchanged_api_sha256"] != candidate["unchanged_api_sha256"]:
        raise RuntimeError("floor/ticks-to-price source changed")


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
        or report["sources"]["main"]["resolved_revision"] != BASELINE
        or report["sources"]["main"]["source_sha256"] != sources
    ):
        raise RuntimeError("baseline is not complete pinned main evidence")
    sample = report["observations"][0]
    if sample["role"] != "main" or sample["status"] != "complete":
        raise RuntimeError("baseline did not execute precisely one main worker")
    validate_saved(sample, directory)
    validate_saved({"files": report["evidence_manifest"]}, directory)
    if json.loads((directory / "main.json").read_text()) != sample["result"]:
        raise RuntimeError("baseline result record differs from its raw worker file")
    validate_worker_identity(sample["result"], sources)
    if (
        hashlib.sha256(
            subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
        ).hexdigest()
        != report["sources"]["main"]["git_archive_sha256"]
    ):
        raise RuntimeError("baseline Git archive hash differs")
    for name, sha in sources.items():
        if digest(directory / "sources/main" / name) != sha:
            raise RuntimeError("baseline source copy changed: " + name)
    if validate_observations(sample["result"], "main", directory) != sample["criteria"]:
        raise RuntimeError("baseline cached criteria differ from actual saved ledger")
    return sample, report["sources"]["main"]


def reproduce(
    stage: str, candidate: str | None, reuse: Path | None, output: Path
) -> int:
    if not __debug__ or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise RuntimeError("require assertions and a new/empty output directory")
    pins, baseline_sources = frozen(ROOT)
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
        "scope": "Original fictional ingress strings/CSVs and two exposed fictional adapter controls; no strategy, execution calibration, market data or economic claim.",
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
        plan = [("main", BASELINE)] if stage in ("baseline", "verify") else []
        if stage in ("study", "verify"):
            plan.append(("candidate", candidate))
        with tempfile.TemporaryDirectory(prefix="lob-exact-ingress-") as temporary:
            if previous_source:
                report["sources"]["main"] = previous_source
            for role, revision in plan:
                sample: dict[str, Any] = {"role": role, "status": "in_progress"}
                report["observations"].append(sample)
                dump(target, report)
                source_root = Path(temporary) / role
                source = archive(revision, source_root, output / "sources" / role)
                for name, sha in source["source_sha256"].items():
                    if (
                        role == "main" or name not in ALLOWED
                    ) and sha != baseline_sources[name]:
                        raise RuntimeError("unapproved source difference: " + name)
                report["sources"][role] = source
                sample.update(source)
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
                    sample["files"] = file_manifest(output)
                    raise
                result_path.with_suffix(".log").write_text(
                    process.stdout + process.stderr
                )
                sample["worker_returncode"] = process.returncode
                if process.returncode:
                    raise RuntimeError("worker infrastructure failure: " + role)
                result = json.loads(result_path.read_text())
                sample["result"] = result
                sample["observed_summary"] = observed_summary(result, output)
                dump(target, report)
                print(
                    "EXACT_INGRESS_OBSERVATIONS "
                    + json.dumps(
                        {
                            "role": role,
                            "revision": revision,
                            "source_sha256": source["source_sha256"],
                            **sample["observed_summary"],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                validate_worker_identity(result, source["source_sha256"])
                sample["criteria"] = validate_observations(result, role, output)
                sample["files"] = {
                    name: metadata
                    for name, metadata in file_manifest(output).items()
                    if name.startswith(role + "-")
                    or name in (role + ".json", role + ".xml", role + ".log")
                }
                sample["status"] = "complete"
                dump(target, report)
                if role == "main":
                    previous = sample
                elif previous:
                    compare(previous, sample)
                else:
                    raise RuntimeError("candidate has no verified main comparator")
                print(
                    "EXACT_INGRESS_WORKER "
                    + json.dumps(
                        {
                            "role": role,
                            "revision": revision,
                            "counts": result["counts"],
                            "observation_count": sample["criteria"][
                                "observation_count"
                            ],
                            "primary_gate_passed": sample["criteria"][
                                "primary_gate_passed"
                            ],
                            "normal_controls": sample["observed_summary"][
                                "normal_controls"
                            ],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        report.update(status="complete", criterion_passed=True)
        report["evidence_manifest"] = {
            name: metadata
            for name, metadata in file_manifest(output).items()
            if name != "report.json"
        }
        dump(target, report)
        print(
            "EXACT_INGRESS_COMPLETE "
            + json.dumps(
                {
                    "stage": stage,
                    "criterion_passed": True,
                    "sources": report["sources"],
                    "note": "Automated saved-byte/ledger comparison; independent actual-Parquet audit is separate.",
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        report["partial_file_manifest"] = {
            name: metadata
            for name, metadata in file_manifest(output).items()
            if name != "report.json"
        }
        dump(target, report)
        print(
            "EXACT_INGRESS_STOPPED "
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
            parser.error("worker requires explicit output and source pins")
        return worker(
            args.worker_root.resolve(),
            args.worker_output.resolve(),
            args.worker_pins.resolve(),
        )
    if args.stage is None or args.output is None:
        parser.error("--stage and --output are required")
    if (args.stage == "study") != (args.baseline_evidence is not None):
        parser.error("only study requires --baseline-evidence")
    if (args.stage in ("study", "verify")) != (args.candidate is not None):
        parser.error("study/verify require --candidate")
    return reproduce(
        args.stage,
        args.candidate,
        args.baseline_evidence.resolve() if args.baseline_evidence else None,
        args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
