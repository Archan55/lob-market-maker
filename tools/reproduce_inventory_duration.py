"""Frozen inventory-duration regressions, retained reanalysis and CI wiring.

Retained inputs are the two exact local Nov5 synthetic archives in the contract.
They are not fresh data and are never regenerated or rewritten. Wiring runs only
the fixed300-event CI input. No stage measures performance or economic advantage.
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
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
CONTRACT = "docs/evidence/inventory-duration-20261106/contract.json"
CONTRACT_SHA = "b265a1d9afbb87e28be36e321ad1164e62b05757c709a02bbf4534d433217471"
TEST = "tests/unit/test_inventory_duration_independent.py"
TEST_SHA = "a85759c5139734098259502e554c342acc5ebe7038df66bbd5e5e18f943fc252"
MODULES = ("metrics", "backtest", "case_study", "report", "config")
TABLES = (
    "orders",
    "fills",
    "inventory",
    "pnl",
    "market",
    "markouts",
    "risk_events",
    "quotes",
)
TIME_FIELDS = {
    "time_near_limits_fraction",
    "observations_near_limits_fraction",
    "near_limit_duration_ns",
    "observation_duration_ns",
    "time_window_start_ns",
    "time_window_end_ns",
}
WINDOW = ("session_start_timestamp_ns", "session_end_timestamp_ns")
CLOCKS = {"wall_clock_seconds", "events_per_second", "peak_memory_bytes"}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def frozen(root: Path) -> None:
    if digest(root / CONTRACT) != CONTRACT_SHA or digest(root / TEST) != TEST_SHA:
        raise RuntimeError("frozen contract or independent test hash changed")


def archive(revision: str, destination: Path, retained: Path) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if resolved != revision:
        raise RuntimeError("explicit commit SHA did not resolve to itself")
    content = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(content)) as bundle:
        bundle.extractall(destination, filter="data")
    retained.mkdir(parents=True)
    hashes = {}
    for name in MODULES:
        source = destination / "src/lobmm" / (name + ".py")
        hashes[name] = digest(source)
        shutil.copy2(source, retained / source.name)
    for name in (CONTRACT, TEST):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        shutil.copy2(ROOT / name, retained / Path(name).name)
    frozen(destination)
    return {
        "resolved_revision": resolved,
        "git_archive_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": hashes,
    }


class Outcomes:
    def __init__(self) -> None:
        self.nodes: list[str] = []
        self.reports: list[dict[str, Any]] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.nodes = [item.nodeid for item in session.items]

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


def regressions(output: Path) -> dict[str, Any]:
    import pytest

    outcomes = Outcomes()
    code = pytest.main(
        [TEST, "-q", "--tb=short", f"--junitxml={output.with_suffix('.xml')}"],
        plugins=[outcomes],
    )
    reports = outcomes.reports
    return {
        "pytest_exit_code": int(code),
        "collected_nodeids": outcomes.nodes,
        "reports": reports,
        "failed_nodeids": list(
            dict.fromkeys(
                item["nodeid"] for item in reports if item["outcome"] == "failed"
            )
        ),
        "passed": sum(
            item["when"] == "call" and item["outcome"] == "passed" for item in reports
        ),
        "failed": sum(
            item["when"] == "call" and item["outcome"] == "failed" for item in reports
        ),
        "errors": sum(
            item["when"] != "call" and item["outcome"] == "failed" for item in reports
        ),
        "skipped": sum(item["outcome"] == "skipped" for item in reports),
        "junit_sha256": digest(output.with_suffix(".xml")),
    }


def stable_metrics(metrics: dict[str, Any], *, exclude_clocks: bool) -> dict[str, Any]:
    result = deepcopy(metrics)
    for field in TIME_FIELDS:
        result["inventory"].pop(field, None)
    if exclude_clocks:
        for field in CLOCKS:
            result["engineering"].pop(field, None)
    return result


def read_run(directory: Path) -> tuple[dict[str, Any], dict[str, str]]:
    import polars as pl

    names = {name + ".parquet" for name in TABLES}
    if {path.name for path in directory.glob("*.parquet")} != names:
        raise RuntimeError(
            "run does not contain exactly the frozen eight Parquet tables"
        )
    paths = [directory / name for name in sorted(names)] + [
        directory / name
        for name in (
            "run_config.yaml",
            "metrics.json",
            "diagnostics.json",
            "summary.json",
        )
    ]
    hashes = {path.name: digest(path) for path in paths}
    return (
        {
            "tables": {
                name: pl.read_parquet(directory / (name + ".parquet")).to_dicts()
                for name in TABLES
            },
            **{
                name: json.loads((directory / (name + ".json")).read_text())
                for name in ("metrics", "diagnostics", "summary")
            },
        },
        hashes,
    )


def bounds(tables: dict[str, Any]) -> tuple[int, int]:
    times = [row["timestamp_ns"] for row in tables["market"]]
    if (
        not times
        or any(type(value) is not int or value < 0 for value in times)
        or times != sorted(times)
    ):
        raise RuntimeError("actual market endpoints are absent, invalid or unordered")
    return times[0], times[-1]


def duration_oracle(
    root: Path,
    rows: list[dict[str, Any]],
    start: int,
    end: int,
    limit: int,
    fraction: float,
) -> dict[str, Any]:
    # Consecutive identical band memberships define the same piecewise-constant
    # path. Compress only the independent oracle's copy; production, observation
    # counts and original-row hashes always use every original row.
    compressed = []
    previous = None
    previous_time = None
    for row in rows:
        timestamp = row.get("timestamp_ns")
        if (
            type(timestamp) is not int
            or timestamp < 0
            or (previous_time is not None and timestamp < previous_time)
        ):
            raise RuntimeError("raw inventory timestamps are invalid or unordered")
        previous_time = timestamp
        member = abs(int(row.get("inventory", 0))) >= fraction * limit
        if member != previous:
            compressed.append(row)
        previous = member
    namespace = runpy.run_path(str(root / TEST))
    near, duration, ratio = namespace["partition_duration_oracle"](
        compressed,
        start,
        end,
        maximum_limit=limit,
        near_limit_fraction=fraction,
    )
    return {
        "near_limit_duration_ns": near,
        "observation_duration_ns": duration,
        "time_near_limits_fraction": None if ratio is None else float(ratio),
        "time_window_start_ns": start,
        "time_window_end_ns": end,
        "original_rows": len(rows),
        "oracle_compressed_rows": len(compressed),
        "original_rows_sha256": object_hash(rows),
    }


def validate_duration(
    metrics: dict[str, Any], oracle: dict[str, Any], old_fraction: float
) -> None:
    inventory = metrics["inventory"]
    for field in ("near_limit_duration_ns", "observation_duration_ns"):
        if type(inventory.get(field)) is not int:
            raise RuntimeError(f"candidate {field} is not an exact integer")
    for field in TIME_FIELDS - {"observations_near_limits_fraction"}:
        if inventory[field] != oracle[field]:
            raise RuntimeError(
                f"candidate differs from independent duration oracle: {field}"
            )
    if inventory["observations_near_limits_fraction"] != old_fraction:
        raise RuntimeError(
            "candidate changed the original all-row observation estimator"
        )


def retained(
    root: Path, output: Path, workload: str, modules: dict[str, Any]
) -> dict[str, Any]:
    contract = json.loads((root / CONTRACT).read_text())
    specified = contract["retained_inputs"]
    directory = Path(specified[workload])
    report_path = Path(specified["original_provenance_report"])
    manifest_path = (
        report_path.parents[3]
        / "docs/evidence/exact-risk-20261105/retained-workspace-manifest.json"
    )
    if digest(manifest_path) != specified["original_provenance_manifest_sha256"]:
        raise RuntimeError(
            "original retained manifest hash differs from frozen contract"
        )
    manifest = json.loads(manifest_path.read_text())

    def original_file(path: Path) -> str:
        expected = manifest["files"][str(path.relative_to(Path(manifest["root"])))]
        observed = digest(path)
        if (
            path.stat().st_size != expected["size_bytes"]
            or observed != expected["sha256"]
        ):
            raise RuntimeError(f"original retained file changed: {path}")
        return observed

    report_sha = original_file(report_path)
    provenance = json.loads(report_path.read_text())
    if provenance["status"] != "complete" or not provenance["criterion_passed"]:
        raise RuntimeError("original integration provenance did not complete")
    original = [
        row
        for row in provenance["observations"]
        if row["label"] == "main" and row["workload"] == workload
    ]
    if len(original) != 1:
        raise RuntimeError("original main provenance is ambiguous")
    original = original[0]
    if (
        original["resolved_revision"] != BASELINE
        or Path(original["artifact_root"]) != directory.parent
    ):
        raise RuntimeError("retained path/source differs from original main provenance")
    if original["source_sha256"] != provenance["sources"]["main"]["source_sha256"]:
        raise RuntimeError("original worker and archive source hashes differ")
    data, hashes = read_run(directory)
    for name, observed in hashes.items():
        if original_file(directory / name) != observed:
            raise RuntimeError("retained input changed while reading")
    if {name: hashes[name] for name in hashes if name.endswith(".parquet")} != original[
        "parquet_sha256"
    ]:
        raise RuntimeError("retained tables differ from original worker hashes")
    config = modules["config"].load_config(directory / "run_config.yaml")
    config_json = config.model_dump(mode="json")
    config_hash = hashlib.sha256(
        json.dumps(config_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        config_json != original["effective_config"]
        or config_hash != original["effective_config_sha256"]
    ):
        raise RuntimeError(
            "retained effective configuration differs from original provenance"
        )
    if data["diagnostics"]["event_stream_sha256"] != original["event_stream_sha256"]:
        raise RuntimeError(
            "retained event fingerprint differs from original provenance"
        )
    if any(
        data["metrics"][key] != value
        for key, value in original["selected_metrics"].items()
    ):
        raise RuntimeError("retained metrics differ from original worker observations")
    start, end = bounds(data["tables"])
    diagnostics = dict(
        data["diagnostics"], **dict(zip(WINDOW, (start, end), strict=True))
    )
    tables = data["tables"]
    rows_sha = object_hash(tables)
    metrics = modules["metrics"].build_metrics(
        orders=tables["orders"],
        fills=tables["fills"],
        inventory=tables["inventory"],
        pnl=tables["pnl"],
        market_states=tables["market"],
        markouts=tables["markouts"],
        risk_events=tables["risk_events"],
        quote_messages=tables["quotes"],
        diagnostics=diagnostics,
        tick_size=config.instrument.tick_size,
        near_limit_fraction=config.metrics.near_limit_fraction,
    )
    oracle = duration_oracle(
        root,
        tables["inventory"],
        start,
        end,
        config.risk.max_abs_inventory,
        config.metrics.near_limit_fraction,
    )
    if object_hash(tables) != rows_sha or any(
        digest(directory / name) != value for name, value in hashes.items()
    ):
        raise RuntimeError("reanalysis mutated original rows or retained bytes")
    return {
        "input_path": str(directory),
        "input_sha256": hashes,
        "original_provenance_report_sha256": report_sha,
        "original_manifest_sha256": digest(manifest_path),
        "event_stream_sha256": original["event_stream_sha256"],
        "effective_config_sha256": config_hash,
        "metrics": metrics,
        "original_metrics": data["metrics"],
        "oracle": oracle,
        "actual_window": [start, end],
        "old_observations_fraction": data["metrics"]["inventory"][
            "time_near_limits_fraction"
        ],
        "exposure": "Exact previously inspected Nov5 synthetic main bytes; no fresh holdout or engine regeneration.",
    }


def wiring(root: Path, output: Path, modules: dict[str, Any]) -> dict[str, Any]:
    from lobmm.data import event_stream_sha256
    from lobmm.synthetic import generate_synthetic_events
    from lobmm.validation import validate_event_stream

    config = modules["config"].load_config(root / "configs/ci.yaml")
    if config.synthetic.event_count != 300 or config.random_seed != 7:
        raise RuntimeError("frozen CI300/seed7 configuration changed")
    config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(update={"track_memory": False}),
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )
    events = generate_synthetic_events(config)
    validation = validate_event_stream(events, mode=config.data.validation_mode)
    destination = Path(
        tempfile.mkdtemp(prefix=output.stem + "-artifacts-", dir=output.parent)
    )
    partial = json.loads(output.read_text())
    partial["artifact_root"] = str(destination)
    dump(output, partial)
    result = modules["backtest"].run_backtest(
        config,
        events,
        run_name="inventory-duration-ci",
        output_root=destination,
        generate_plots=False,
        input_validation=validation,
        persist_artifacts=True,
    )
    data, hashes = read_run(result.run_directory)
    start, end = bounds(data["tables"])
    return {
        "artifact_root": str(destination),
        "input_sha256": hashes,
        "parquet_sha256": {
            name: hashes[name] for name in hashes if name.endswith(".parquet")
        },
        "effective_config_sha256": object_hash(config.model_dump(mode="json")),
        "config_file_sha256": digest(root / "configs/ci.yaml"),
        "event_stream_sha256": event_stream_sha256(events),
        "metrics": data["metrics"],
        "diagnostics": data["diagnostics"],
        "summary": data["summary"],
        "oracle": duration_oracle(
            root,
            data["tables"]["inventory"],
            start,
            end,
            config.risk.max_abs_inventory,
            config.metrics.near_limit_fraction,
        ),
        "actual_window": [start, end],
    }


def worker(
    stage: str, root: Path, role: str, revision: str, output: Path, workload: str
) -> None:
    sys.path.insert(0, str(root / "src"))
    frozen(root)
    modules = {name: importlib.import_module("lobmm." + name) for name in MODULES}
    paths = {name: Path(module.__file__).resolve() for name, module in modules.items()}
    if any(
        path != (root / "src/lobmm" / (name + ".py")).resolve()
        for name, path in paths.items()
    ):
        raise RuntimeError("worker imported an external editable production module")
    sources = {name: digest(path) for name, path in paths.items()}
    result = {
        "status": "in_progress",
        "stage": stage,
        "role": role,
        "workload": workload,
        "resolved_revision": revision,
        "source_sha256": sources,
        "imported_sources": {name: str(path) for name, path in paths.items()},
        "test_sha256": TEST_SHA,
        "contract_sha256": CONTRACT_SHA,
        "harness_sha256": digest(Path(__file__).resolve()),
        "python": sys.version,
        "integer_string_guard": sys.get_int_max_str_digits(),
    }
    dump(output, result)
    result.update(
        regressions(output)
        if stage in ("triage", "regression")
        else retained(root, output, workload, modules)
        if stage == "retained"
        else wiring(root, output, modules)
    )
    frozen(root)
    if sources != {name: digest(path) for name, path in paths.items()}:
        raise RuntimeError("archived production source changed during worker")
    result["status"] = "complete"
    dump(output, result)


def compare(stage: str, main: dict[str, Any], candidate: dict[str, Any]) -> None:
    if stage == "regression":
        if (
            candidate["collected_nodeids"] != main["collected_nodeids"]
            or candidate["failed"]
            or candidate["pytest_exit_code"] != 0
        ):
            raise RuntimeError(
                "candidate did not pass the identical frozen regression corpus"
            )
        return
    keys = (
        (
            "input_sha256",
            "event_stream_sha256",
            "effective_config_sha256",
            "actual_window",
            "oracle",
        )
        if stage == "retained"
        else (
            "parquet_sha256",
            "event_stream_sha256",
            "effective_config_sha256",
            "config_file_sha256",
            "actual_window",
            "oracle",
            "summary",
        )
    )
    if any(main[key] != candidate[key] for key in keys):
        raise RuntimeError(
            "source workers used different bytes, windows, oracle or replay outputs"
        )
    if stable_metrics(
        main["metrics"], exclude_clocks=stage == "wiring"
    ) != stable_metrics(candidate["metrics"], exclude_clocks=stage == "wiring"):
        raise RuntimeError("non-time inventory statistics or other metrics changed")
    if stage == "retained" and main["metrics"] != main["original_metrics"]:
        raise RuntimeError(
            "main failed to reproduce the original metrics on unchanged bytes"
        )
    if stage == "wiring":
        allowed = CLOCKS | set(WINDOW)
        if {
            key: value
            for key, value in main["diagnostics"].items()
            if key not in allowed
        } != {
            key: value
            for key, value in candidate["diagnostics"].items()
            if key not in allowed
        }:
            raise RuntimeError("unrelated wiring diagnostics changed")
        if [candidate["diagnostics"].get(key) for key in WINDOW] != candidate[
            "actual_window"
        ]:
            raise RuntimeError(
                "candidate replay did not publish actual selected-market bounds"
            )
    validate_duration(
        candidate["metrics"],
        candidate["oracle"],
        main["metrics"]["inventory"]["time_near_limits_fraction"],
    )


def reproduce(stage: str, candidate: str | None, output: Path) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError("refusing to overwrite retained evidence")
    frozen(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (Path(__file__), "reproducer.py"),
        (ROOT / CONTRACT, "contract.json"),
        (ROOT / TEST, Path(TEST).name),
    ):
        shutil.copy2(source, output / name)
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
    report: dict[str, Any] = {
        "status": "in_progress",
        "stage": stage,
        "harness_sha256": digest(Path(__file__).resolve()),
        "test_sha256": TEST_SHA,
        "contract_sha256": CONTRACT_SHA,
        "process_cap_seconds": CAP_SECONDS,
        "attempts": [],
        "observations": [],
    }
    path = output / "report.json"
    dump(path, report)
    try:
        revisions = {"main": BASELINE}
        if stage != "triage":
            revisions["candidate"] = candidate
        with tempfile.TemporaryDirectory(prefix="lob-inventory-duration-") as temporary:
            roots = {role: Path(temporary) / role for role in revisions}
            sources = {
                role: archive(revision, roots[role], output / "sources" / role)
                for role, revision in revisions.items()
            }
            report["sources"] = sources
            contract = json.loads((ROOT / CONTRACT).read_text())
            if any(
                sources["main"]["source_sha256"][Path(name).stem] != value
                for name, value in contract["baseline_source_sha256"].items()
            ):
                raise RuntimeError(
                    "main archive differs from frozen baseline source hashes"
                )
            for workload in (
                ("ci", "benchmark")
                if stage == "retained"
                else ("ci",)
                if stage == "wiring"
                else ("tests",)
            ):
                main = None
                for role, revision in revisions.items():
                    sample_path = output / f"{workload}-{role}.json"
                    attempt = {
                        "role": role,
                        "workload": workload,
                        "status": "in_progress",
                    }
                    report["attempts"].append(attempt)
                    dump(path, report)
                    env = dict(os.environ, PYTHONPATH=str(roots[role] / "src"))
                    env.pop("PYTEST_ADDOPTS", None)
                    try:
                        process = subprocess.run(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--stage",
                                stage,
                                "--worker",
                                str(roots[role]),
                                role,
                                revision,
                                str(sample_path),
                                workload,
                            ],
                            cwd=roots[role],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=CAP_SECONDS,
                        )
                    except subprocess.TimeoutExpired as exc:
                        sample_path.with_suffix(".log").write_text(
                            "".join(
                                value.decode(errors="replace")
                                if isinstance(value, bytes)
                                else value or ""
                                for value in (exc.stdout, exc.stderr)
                            )
                        )
                        attempt["status"] = "timeout"
                        raise
                    sample_path.with_suffix(".log").write_text(
                        process.stdout + process.stderr
                    )
                    if process.returncode:
                        attempt.update(
                            status="worker_failure", exit_code=process.returncode
                        )
                        raise RuntimeError(f"worker failed: {workload}/{role}")
                    sample = json.loads(sample_path.read_text())
                    report["observations"].append(sample)
                    attempt["status"] = "complete"
                    dump(path, report)
                    expected = {
                        "stage": stage,
                        "role": role,
                        "workload": workload,
                        "resolved_revision": revision,
                        "source_sha256": sources[role]["source_sha256"],
                        "harness_sha256": report["harness_sha256"],
                        "test_sha256": TEST_SHA,
                        "contract_sha256": CONTRACT_SHA,
                        "status": "complete",
                    }
                    if any(sample[key] != value for key, value in expected.items()):
                        raise RuntimeError(
                            "worker source/import/harness identity mismatch"
                        )
                    if stage in ("triage", "regression"):
                        if (
                            sample["pytest_exit_code"] not in (0, 1)
                            or sample["errors"]
                            or sample["skipped"]
                            or not sample["collected_nodeids"]
                            or sample["passed"] + sample["failed"]
                            != len(sample["collected_nodeids"])
                        ):
                            raise RuntimeError(
                                "frozen corpus did not execute completely without errors/skips"
                            )
                        if role == "main" and not any(
                            "test_spacing_contrast_and_same_timestamp_duplicate_invariance"
                            in node
                            for node in sample["failed_nodeids"]
                        ):
                            raise RuntimeError(
                                "main did not reproduce the frozen semantic duration mismatch"
                            )
                    if role == "main":
                        main = sample
                    else:
                        compare(stage, main, sample)
                    print(
                        json.dumps(
                            {"workload": workload, "role": role, "status": "complete"}
                        ),
                        flush=True,
                    )
            report.update(status="complete", criterion_passed=True)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        dump(path, report)
        print(f"Stopped; retained evidence in {output}: {exc}", file=sys.stderr)
        return 1
    dump(path, report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", required=True, choices=("triage", "regression", "retained", "wiring")
    )
    parser.add_argument("--candidate", metavar="SHA")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--worker",
        nargs=5,
        metavar=("SOURCE", "ROLE", "REVISION", "OUTPUT", "WORKLOAD"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        root, role, revision, output, workload = args.worker
        worker(
            args.stage,
            Path(root).resolve(),
            role,
            revision,
            Path(output).resolve(),
            workload,
        )
        return 0
    if args.output is None:
        parser.error("--output is required")
    if args.stage == "triage":
        if args.candidate is not None:
            parser.error("triage uses only the frozen main baseline")
    elif (
        args.candidate is None
        or len(args.candidate) != 40
        or any(char not in "0123456789abcdef" for char in args.candidate)
    ):
        parser.error("--candidate must be an explicit full lowercase Git SHA")
    return reproduce(args.stage, args.candidate, args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
