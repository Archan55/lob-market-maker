"""Bounded archive-based exact-risk triage, regression and integration evidence.

Integration workloads were previously exposed synthetic controls, not fresh
market data. No stage measures performance or supports an economic claim.
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
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINES = {
    "main": "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f",
    "pr3": "ceccf0c69a3afbc09b07c993b65614ab09fef838",
}
CONTRACT_NAME = "docs/evidence/exact-risk-20261105/contract.json"
CONTRACT_SHA256 = "16bcd80f4b714df925d14a25a0455f6e55c0488d79477214f3e090e594e12fa9"
TEST_NAME = "tests/unit/test_risk_exact_thresholds_independent.py"
TEST_SHA256 = "1467bd43ad1e9822d26c89b7a52aea5280878cf6e40f19b74e123109ac8306bc"
SOURCE_NAMES = ("risk.py", "config.py", "backtest.py", "portfolio.py", "exchange.py")
TABLES = {
    name + ".parquet"
    for name in (
        "orders",
        "fills",
        "inventory",
        "pnl",
        "quotes",
        "risk_events",
        "market",
        "markouts",
    )
}
PROCESS_CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def frozen_inputs(root: Path) -> None:
    if digest(root / CONTRACT_NAME) != CONTRACT_SHA256:
        raise RuntimeError("frozen exact-risk contract hash changed")
    if len(TEST_SHA256) != 64 or digest(root / TEST_NAME) != TEST_SHA256:
        raise RuntimeError(
            "independent regression has not been frozen or its hash changed"
        )


def archive(reference: str, destination: Path, retained: Path) -> dict[str, Any]:
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", reference + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if revision != reference:
        raise RuntimeError("requested Git SHA did not resolve to itself")
    bundle = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(bundle)) as source:
        source.extractall(destination, filter="data")
    retained.mkdir(parents=True)
    sources = {}
    for name in SOURCE_NAMES:
        path = destination / "src/lobmm" / name
        sources[name] = digest(path)
        shutil.copy2(path, retained / name)
    for name in (TEST_NAME, CONTRACT_NAME):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        shutil.copy2(ROOT / name, retained / Path(name).name)
    frozen_inputs(destination)
    return {
        "resolved_revision": revision,
        "git_archive_sha256": hashlib.sha256(bundle).hexdigest(),
        "source_sha256": sources,
        "test_sha256": TEST_SHA256,
        "contract_sha256": CONTRACT_SHA256,
    }


class Outcomes:
    """Retain actual pytest node IDs, including parameter IDs and failure phase."""

    def __init__(self) -> None:
        self.collected: list[str] = []
        self.reports: list[dict[str, Any]] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "longrepr": report.longreprtext
                if report.failed or report.skipped
                else None,
            }
        )


def regression(root: Path, output: Path) -> dict[str, Any]:
    import pytest

    outcomes = Outcomes()
    code = pytest.main(
        [TEST_NAME, "-q", "--tb=short", f"--junitxml={output.with_suffix('.xml')}"],
        plugins=[outcomes],
    )
    failures = [item for item in outcomes.reports if item["outcome"] == "failed"]
    return {
        "pytest_exit_code": int(code),
        "collected_nodeids": outcomes.collected,
        "failed_nodeids": list(dict.fromkeys(item["nodeid"] for item in failures)),
        "reports": outcomes.reports,
        "counts": {
            "collected": len(outcomes.collected),
            "call_passed": sum(
                item["when"] == "call" and item["outcome"] == "passed"
                for item in outcomes.reports
            ),
            "call_failed": sum(
                item["when"] == "call" and item["outcome"] == "failed"
                for item in outcomes.reports
            ),
            "errors": sum(
                item["when"] != "call" and item["outcome"] == "failed"
                for item in outcomes.reports
            ),
            "skipped": sum(item["outcome"] == "skipped" for item in outcomes.reports),
        },
        "junit_sha256": digest(output.with_suffix(".xml")),
    }


def integration(
    root: Path, output: Path, workload: str, modules: dict[str, Any]
) -> dict[str, Any]:
    from lobmm.data import event_stream_sha256
    from lobmm.synthetic import generate_synthetic_events
    from lobmm.validation import validate_event_stream

    trace: list[Any] = []
    manager = modules["risk.py"].RiskManager
    original = manager.observe_pnl

    def observe(self: Any, net_pnl: Any, *, timestamp_ns: int) -> Any:
        if type(net_pnl) is not Decimal:
            raise TypeError("risk input was not an exact builtin Decimal")
        trace.append([timestamp_ns, list(net_pnl.as_tuple())])
        return original(self, net_pnl, timestamp_ns=timestamp_ns)

    manager.observe_pnl = observe
    config_path = (
        root / "configs" / ("ci.yaml" if workload == "ci" else "benchmark.yaml")
    )
    event_count = 300 if workload == "ci" else 20000
    config = modules["config.py"].load_config(config_path)
    if config.random_seed != 7:
        raise RuntimeError("integration config changed the frozen seed7")
    if workload == "ci" and config.synthetic.event_count != 300:
        raise RuntimeError("CI config changed the frozen300-event workload")
    config = config.model_copy(
        update={
            "synthetic": config.synthetic.model_copy(
                update={"event_count": event_count}
            ),
            "backtest": config.backtest.model_copy(update={"track_memory": False}),
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )
    config_text = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    events = generate_synthetic_events(config)
    validation = validate_event_stream(events, mode=config.data.validation_mode)
    artifact_root = Path(
        tempfile.mkdtemp(prefix=output.stem + "-artifacts-", dir=output.parent)
    )
    partial = json.loads(output.read_text())
    partial.update(
        artifact_root=str(artifact_root),
        config_file_sha256=digest(config_path),
        effective_config_sha256=hashlib.sha256(config_text.encode()).hexdigest(),
        event_stream_sha256=event_stream_sha256(events),
    )
    dump(output, partial)
    trace_path = output.with_suffix(".risk-trace.json")
    try:
        result = modules["backtest.py"].run_backtest(
            config,
            events,
            run_name="exact-risk-" + workload,
            output_root=artifact_root,
            generate_plots=False,
            input_validation=validation,
            persist_artifacts=True,
        )
    finally:
        trace_path.write_text(
            json.dumps(trace, separators=(",", ":"), allow_nan=False) + "\n"
        )
    tables = {
        path.name: digest(path) for path in result.run_directory.glob("*.parquet")
    }
    if set(tables) != TABLES:
        raise RuntimeError(f"expected exactly8Parquet files, got {sorted(tables)}")
    if result.diagnostics["events_processed"] != event_count or not trace:
        raise RuntimeError(
            "integration event/risk-input coverage differed from frozen workload"
        )
    return {
        "artifact_root": str(artifact_root),
        "config_file_sha256": digest(config_path),
        "effective_config": json.loads(config_text),
        "effective_config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
        "event_stream_sha256": event_stream_sha256(events),
        "parquet_sha256": tables,
        "risk_trace_sha256": digest(trace_path),
        "risk_trace_count": len(trace),
        "selected_metrics": {
            name: result.metrics[name]
            for name in ("pnl", "trading_activity", "inventory", "risk")
        },
        "selected_diagnostics": {
            name: result.diagnostics[name]
            for name in (
                "events_processed",
                "scheduler_events_processed",
                "end_inventory",
            )
        },
        "interpretation": "Previously exposed untimed synthetic integration control. Raw timing diagnostics retained in artifacts but excluded from comparison/inference.",
    }


def worker(
    stage: str, root: Path, label: str, revision: str, output: Path, workload: str
) -> None:
    sys.path.insert(0, str(root / "src"))
    frozen_inputs(root)
    modules = {
        name: importlib.import_module("lobmm." + name.removesuffix(".py"))
        for name in SOURCE_NAMES
    }
    paths = {name: Path(module.__file__).resolve() for name, module in modules.items()}
    for name, path in paths.items():
        if path != (root / "src/lobmm" / name).resolve():
            raise RuntimeError(f"wrong imported source: {path}")
    hashes = {name: digest(path) for name, path in paths.items()}
    guard, context = sys.get_int_max_str_digits(), repr(getcontext())
    result: dict[str, Any] = {
        "status": "in_progress",
        "stage": stage,
        "label": label,
        "workload": workload,
        "resolved_revision": revision,
        "imported_sources": {name: str(path) for name, path in paths.items()},
        "source_sha256": hashes,
        "test_sha256": TEST_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "harness_sha256": digest(Path(__file__).resolve()),
        "python": sys.version,
        "guard_before": guard,
        "decimal_context_before": context,
    }
    dump(output, result)
    result.update(
        integration(root, output, workload, modules)
        if stage == "integration"
        else regression(root, output)
    )
    frozen_inputs(root)
    if hashes != {name: digest(path) for name, path in paths.items()}:
        raise RuntimeError("archived source changed during worker execution")
    if any(
        Path(module.__file__).resolve() != paths[name]
        for name, module in modules.items()
    ):
        raise RuntimeError("worker module import identity changed")
    result.update(
        status="complete",
        guard_after=sys.get_int_max_str_digits(),
        decimal_context_after=repr(getcontext()),
        context_unchanged=repr(getcontext()) == context,
        guard_unchanged=sys.get_int_max_str_digits() == guard,
    )
    dump(output, result)


def validate_regression(sample: dict[str, Any], candidate: bool) -> None:
    counts = sample["counts"]
    if (
        sample["pytest_exit_code"] not in (0, 1)
        or counts["collected"] != 113
        or counts["errors"]
        or counts["skipped"]
        or counts["call_passed"] + counts["call_failed"] != counts["collected"]
    ):
        raise RuntimeError(
            "regression did not complete the unchanged test corpus without setup errors/skips"
        )
    if candidate and (
        sample["pytest_exit_code"] != 0
        or counts["call_passed"] != counts["collected"]
        or not sample["context_unchanged"]
        or not sample["guard_unchanged"]
    ):
        raise RuntimeError(
            "candidate failed independent regressions or ambient-context/guard preservation"
        )


def reproduce(stage: str, candidate: str | None, output: Path) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required for regression evidence")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError(f"refusing to overwrite retained evidence: {output}")
    frozen_inputs(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, output / "reproducer.py")
    shutil.copy2(ROOT / TEST_NAME, output / Path(TEST_NAME).name)
    shutil.copy2(ROOT / CONTRACT_NAME, output / "contract.json")
    dump(
        output / "environment.json",
        {
            "python": sys.version,
            "platform": platform.platform(),
            "dependencies": sorted(
                (item.metadata["Name"], item.version)
                for item in importlib.metadata.distributions()
                if item.metadata["Name"]
            ),
        },
    )
    revisions = {"main": BASELINES["main"]}
    if stage != "integration":
        revisions["pr3"] = BASELINES["pr3"]
    if stage != "triage":
        if candidate is None:
            raise RuntimeError("candidate SHA is required outside triage")
        revisions["candidate"] = candidate
    report: dict[str, Any] = {
        "status": "in_progress",
        "stage": stage,
        "test_sha256": TEST_SHA256,
        "contract_sha256": CONTRACT_SHA256,
        "harness_sha256": digest(Path(__file__).resolve()),
        "process_cap_seconds": PROCESS_CAP_SECONDS,
        "observations": [],
        "attempts": [],
        "scope": "Original frozen exact-risk witnesses and exposed synthetic integration controls; no timing or economic claim.",
    }
    report_path = output / "report.json"
    dump(report_path, report)
    try:
        with tempfile.TemporaryDirectory(prefix="lob-exact-risk-source-") as temporary:
            paths = {label: Path(temporary) / label for label in revisions}
            sources = {
                label: archive(revision, paths[label], output / "sources" / label)
                for label, revision in revisions.items()
            }
            report["sources"] = sources
            dump(report_path, report)
            if "pr3" in sources and any(
                sources["main"]["source_sha256"][name]
                != sources["pr3"]["source_sha256"][name]
                for name in ("risk.py", "config.py")
            ):
                raise RuntimeError("main and PR3 risk/config baseline sources differ")
            workloads = ("ci", "benchmark") if stage == "integration" else ("tests",)
            for workload in workloads:
                reference = None
                for label, revision in revisions.items():
                    sample_path = output / f"{workload}-{label}.json"
                    attempt = {
                        "workload": workload,
                        "label": label,
                        "status": "in_progress",
                    }
                    report["attempts"].append(attempt)
                    dump(report_path, report)
                    env = dict(os.environ, PYTHONPATH=str(paths[label] / "src"))
                    env.pop("PYTEST_ADDOPTS", None)
                    try:
                        process = subprocess.run(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--stage",
                                stage,
                                "--worker",
                                str(paths[label]),
                                label,
                                revision,
                                str(sample_path),
                                workload,
                            ],
                            cwd=paths[label],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=PROCESS_CAP_SECONDS,
                        )
                    except subprocess.TimeoutExpired as exc:
                        captured = "".join(
                            value.decode(errors="replace")
                            if isinstance(value, bytes)
                            else value or ""
                            for value in (exc.stdout, exc.stderr)
                        )
                        sample_path.with_suffix(".log").write_text(captured)
                        attempt["status"] = "timeout"
                        raise
                    sample_path.with_suffix(".log").write_text(
                        process.stdout + process.stderr
                    )
                    if process.returncode:
                        attempt.update(
                            status="worker_failure", exit_code=process.returncode
                        )
                        raise RuntimeError(f"worker failed: {workload} {label}")
                    sample = json.loads(sample_path.read_text())
                    report["observations"].append(sample)
                    attempt["status"] = "completed"
                    dump(report_path, report)
                    if (
                        sample["status"] != "complete"
                        or sample["stage"] != stage
                        or sample["label"] != label
                        or sample["workload"] != workload
                        or sample["resolved_revision"] != revision
                        or sample["source_sha256"] != sources[label]["source_sha256"]
                        or any(
                            sample[key] != report[key]
                            for key in (
                                "harness_sha256",
                                "test_sha256",
                                "contract_sha256",
                            )
                        )
                    ):
                        raise RuntimeError(
                            "worker source/import/harness/frozen-input identity mismatch"
                        )
                    if reference is None:
                        reference = sample
                    if stage == "integration":
                        for key in (
                            "config_file_sha256",
                            "effective_config_sha256",
                            "event_stream_sha256",
                            "parquet_sha256",
                            "risk_trace_sha256",
                            "risk_trace_count",
                            "selected_metrics",
                            "selected_diagnostics",
                        ):
                            if sample[key] != reference[key]:
                                raise RuntimeError(
                                    f"{workload} integration mismatch: {key}"
                                )
                        if (
                            not sample["context_unchanged"]
                            or not sample["guard_unchanged"]
                        ):
                            raise RuntimeError(
                                "integration changed caller context or integer-string guard"
                            )
                    else:
                        validate_regression(sample, label == "candidate")
                        if (
                            sample["collected_nodeids"]
                            != reference["collected_nodeids"]
                        ):
                            raise RuntimeError(
                                "source workers collected different regression node IDs"
                            )
                        if (
                            label == "pr3"
                            and sample["failed_nodeids"] != reference["failed_nodeids"]
                        ):
                            raise RuntimeError(
                                "identical main/PR3 risk/config sources produced different failed nodes"
                            )
                    print(
                        json.dumps(
                            {"workload": workload, "label": label, "status": "complete"}
                        ),
                        flush=True,
                    )
            if stage == "integration":
                report.update(
                    criterion_passed=True,
                    matched_workloads=["ci300", "benchmark20000"],
                    interpretation="Both exposed controls have identical8Parquetbytes, exact ordered risk inputs and selected metrics. No timing measured or inferred.",
                )
            else:
                baselines = [
                    row for row in report["observations"] if row["label"] in BASELINES
                ]
                reproduced = all(row["counts"]["call_failed"] > 0 for row in baselines)
                report.update(
                    baseline_failures_reproduced=reproduced,
                    criterion_passed=reproduced,
                    interpretation="All frozen nodes executed once per pinned source; baseline failures retained, not counted as candidate success. Candidate must pass every node in final regression.",
                )
            report["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        dump(report_path, report)
        print(f"Stopped; retained evidence in {output}: {exc}", file=sys.stderr)
        return 1
    dump(report_path, report)
    print(
        json.dumps(
            {key: report[key] for key in ("status", "stage", "criterion_passed")}
        )
    )
    return 0 if report["criterion_passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("triage", "regression", "integration"), required=True
    )
    parser.add_argument(
        "--candidate",
        metavar="SHA",
        help="Explicit full candidate commit SHA; omitted for initial triage.",
    )
    parser.add_argument(
        "--output", type=Path, help="New or empty retained evidence directory."
    )
    parser.add_argument(
        "--worker",
        nargs=5,
        metavar=("SOURCE", "LABEL", "REVISION", "OUTPUT", "WORKLOAD"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        source, label, revision, output, workload = args.worker
        worker(
            args.stage,
            Path(source).resolve(),
            label,
            revision,
            Path(output).resolve(),
            workload,
        )
        return 0
    if args.output is None:
        parser.error("--output is required for explicit stage execution")
    if args.stage == "triage":
        if args.candidate is not None:
            parser.error("triage uses only the two frozen baselines")
    elif (
        args.candidate is None
        or len(args.candidate) != 40
        or any(character not in "0123456789abcdef" for character in args.candidate)
    ):
        parser.error(
            "--candidate must be an explicit full lowercase Git SHA outside triage"
        )
    return reproduce(args.stage, args.candidate, args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
