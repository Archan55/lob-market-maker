"""Frozen warm-cache canonical ingestion comparison and read-only verification.

Application imports, input generation and measurements occur only in explicit
isolated workers launched by --run. --verify-evidence audits retained bytes and
recomputes exact paired arithmetic; it never calls an application operation.
"""

from __future__ import annotations

import argparse
import functools
import gc
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "docs/evidence/canonical-ingestion-cost-20261114/"
CONTROLLER = "tools/benchmark_canonical_ingestion.py"
ORACLE = "tools/canonical_ingestion_oracle.py"
FROZEN = {
    PREFIX
    + "contract.json": "51f5ee22d2fa868dfa0f8e136be6404c3ea1ae44b5e30dc72ea990820137c05c",
    PREFIX
    + "workload.json": "03e89610811f5cb663265d9dc1a1bf5ad951d2f8fb72576aac48737059b392a6",
    PREFIX
    + "design-review.json": "3b3ba613ce782a747c85e86181d44129d11bb07b3701d8caf8b3f9caf98ed033",
}
REVISIONS = {
    "main": "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f",
    "pr11": "38a17f09746903938fa76b1d42268c8a75c7d3f6",
}
WORKER_CAP = 120
NORMAL_CAP = 900
THREAD_KEYS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "POLARS_MAX_THREADS",
    "PYTHONHASHSEED",
)
AUDIT_KEYS = (
    "row_count",
    "event_stream_sha256",
    "rows_sha256",
    "field_types",
    "schema",
    "event_type_counts",
    "book",
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def pinned(root: Path, pins: dict[str, str]) -> None:
    for name, expected in pins.items():
        require(digest(root / name) == expected, "changed retained file: " + name)


def source_pins(root: Path, expected: dict[str, str]) -> None:
    inventory = {
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    require(
        len(expected) == 38 and inventory == expected.keys(), "source inventory drift"
    )
    pinned(root, expected)


def protocol(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pinned(root, FROZEN)
    contract = read(root / (PREFIX + "contract.json"))
    workload = read(root / (PREFIX + "workload.json"))
    require(contract["source_revisions"] == REVISIONS, "revision drift")
    require(
        contract["workload_sha256"] == FROZEN[PREFIX + "workload.json"],
        "workload drift",
    )
    require(len(contract["schedule"]) == 40, "incomplete frozen schedule")
    require(workload["event_count"] == 100000, "workload size drift")
    return contract, workload


def diagnostics(sampling_point: str) -> dict[str, Any]:
    dependencies = {}
    for name in ("polars", "pyarrow", "numpy", "pydantic", "PyYAML"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    status = Path("/proc/self/status")
    threads = None
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("Threads:"):
                threads = int(line.split()[1])
    cpu = Path("/proc/cpuinfo")
    cpu_model = None
    if cpu.exists():
        cpu_model = next(
            (
                line.split(":", 1)[1].strip()
                for line in cpu.read_text().splitlines()
                if line.startswith("model name")
            ),
            None,
        )
    tracing = sys.modules.get("tracemalloc")
    return {
        "sampling_point": sampling_point,
        "utc": timestamp(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "process_threads": threads,
        "dependencies": dependencies,
        "thread_environment": {name: os.environ.get(name) for name in THREAD_KEYS},
        "gc_enabled": gc.isenabled(),
        "gc_thresholds": list(gc.get_threshold()),
        "gc_counts": list(gc.get_count()),
        "integer_string_guard": sys.get_int_max_str_digits(),
        "trace_hook": sys.gettrace() is not None,
        "profile_hook": sys.getprofile() is not None,
        "tracemalloc_active": tracing is not None and tracing.is_tracing(),
        "whole_worker_peak_rss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "rss_units": "KiB"
        if sys.platform.startswith("linux")
        else "platform resource units",
        "rss_scope": "Whole-worker high-water including imports, setup, warmup and postprocessing before this sampling point; not incremental timed-operation memory",
        "assertions_enabled": __debug__,
    }


def uninstrumented(state: dict[str, Any]) -> None:
    require(state["gc_enabled"], "inherited GC is disabled; do not change policy")
    require(
        not state["trace_hook"]
        and not state["profile_hook"]
        and not state["tracemalloc_active"],
        "normal worker is instrumented",
    )


def oracle(root: Path) -> Any:
    specification = importlib.util.spec_from_file_location(
        "canonical_ingestion_oracle", root / ORACLE
    )
    require(
        specification is not None and specification.loader is not None,
        "missing independent oracle",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def imports(source: Path, expected: dict[str, str]) -> dict[str, Any]:
    observed = {}
    for name, module in sorted(sys.modules.items()):
        if name != "lobmm" and not name.startswith("lobmm."):
            continue
        path = Path(module.__file__).resolve()
        require(
            path.is_relative_to(source), "application imported outside archive: " + name
        )
        relative = path.relative_to(source).as_posix()
        require(
            relative in expected and digest(path) == expected[relative],
            "import source drift: " + name,
        )
        observed[name] = {
            "relative_path": relative,
            "observed_absolute_path": str(path),
            "sha256": expected[relative],
        }
    require(bool(observed), "no archived application imports recorded")
    return observed


def check_imports(
    observed: dict[str, Any], source: Path, expected: dict[str, str]
) -> None:
    require(bool(observed), "missing retained imports")
    for name, entry in observed.items():
        relative = entry["relative_path"]
        require(
            name == "lobmm" or name.startswith("lobmm."), "unexpected import identity"
        )
        require(
            relative in expected and entry["sha256"] == expected[relative],
            "retained import pin drift",
        )
        require(
            digest(source / relative) == entry["sha256"],
            "imported source bytes changed",
        )
        # Absolute observations describe the original execution; relative source
        # identity remains verifiable after evidence is copied for publication.
        require(
            Path(entry["observed_absolute_path"]).as_posix().endswith("/" + relative),
            "invalid observed import path",
        )


def archive(role: str, destination: Path, contract: dict[str, Any]) -> dict[str, Any]:
    revision = REVISIONS[role]
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    require(resolved == revision, "fixed source revision did not resolve")
    bundle = destination / "sources" / (role + ".tar")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with bundle.open("wb") as output:
        subprocess.run(
            [
                "git",
                "archive",
                revision,
                "src",
                "configs/benchmark.yaml",
                "pyproject.toml",
            ],
            cwd=ROOT,
            stdout=output,
            check=True,
        )
    source = destination / "sources" / role
    source.mkdir()
    with tarfile.open(bundle) as stream:
        require(
            stream.pax_headers.get("comment") == revision,
            "archive commit provenance missing",
        )
        stream.extractall(source, filter="data")
    source_pins(source, contract["source_sha256"][role])
    return {
        "revision": revision,
        "archive": bundle.relative_to(destination).as_posix(),
        "archive_sha256": digest(bundle),
        "directory": source.relative_to(destination).as_posix(),
        "archive_scope": ["src", "configs/benchmark.yaml", "pyproject.toml"],
    }


def audit_value(
    audit: dict[str, Any], expected: dict[str, Any], representation: str
) -> None:
    require(
        all(audit[key] == expected[key] for key in AUDIT_KEYS),
        "actual output/input audit differs",
    )
    require(
        audit["representation"] == representation,
        "actual public return representation differs",
    )
    require(
        audit["checks"]["full_rows_equal"] and all(audit["checks"].values()),
        "actual result audit failed",
    )
    if representation == "events":
        original = audit["original_field_types"]
        for name in ("timestamp_ns", "sequence_number", "price_ticks", "quantity"):
            require(
                original[name] == {"builtins.int": expected["row_count"]},
                "original result numeric types differ",
            )
        require(
            original["event_type"] == {"lobmm.enums.EventType": expected["row_count"]},
            "original event enum type differs",
        )
        side_types = {}
        for name, count in expected["field_types"]["side"].items():
            side_types["lobmm.enums.Side" if name == "builtins.int" else name] = count
        require(original["side"] == side_types, "original side enum/null type differs")
    elif representation == "frame":
        require(audit["original_field_types"] is None, "unexpected frame type proof")


def worker(output: Path, kind: str, sample: int | None) -> None:
    manifest = read(output / "manifest.json")
    contract, workload = protocol(ROOT)
    pinned(ROOT, manifest["harness_pins"])
    if kind == "prepare":
        require(
            manifest["status"] == "preparing"
            and not (output / "input_manifest.json").exists(),
            "input preparation is not authorized or was already attempted",
        )
        role = "main"
        directory = output / "preparation"
        entry: dict[str, Any] = {"kind": kind, "role": role}
    elif kind == "profile":
        require(
            len(manifest["samples"]) == 40
            and all(item["status"] == "complete" for item in manifest["samples"])
            and read(output / "summary.json")["primary_material_cost"]
            and manifest["profile"]["status"] == "started",
            "profile requires all40 validated normal samples and the frozen trigger",
        )
        role = "pr11"
        directory = output / "profile"
        entry = {"kind": kind, "role": role, "operation": "parquet_load_events"}
    else:
        entry = dict(contract["schedule"][sample - 1]) if sample is not None else {}
        require(entry.get("sample") == sample, "invalid fixed sample")
        require(
            manifest["status"] == "normal"
            and len(manifest["samples"]) == sample
            and manifest["samples"][-1]["status"] == "started",
            "sample is not the single authorized next worker",
        )
        role = entry["role"]
        directory = output / "samples" / f"{sample:03d}"
    result = {
        **entry,
        "status": "started",
        "started_utc": timestamp(),
        "harness_pins": manifest["harness_pins"],
        "source_revision": REVISIONS[role],
    }
    result_path = directory / "result.json"
    dump(result_path, result)
    try:
        source = output / manifest["sources"][role]["directory"]
        expected_source = contract["source_sha256"][role]
        result["source_sha256"] = expected_source
        result["archive_sha256"] = manifest["sources"][role]["archive_sha256"]
        result["observed_source_directory"] = str(source)
        source_pins(source, expected_source)
        require(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application preimported before archive selection",
        )
        sys.path.insert(0, str(source / "src"))
        audit = oracle(ROOT)
        state_before = diagnostics(
            "after source selection; before input setup and warmup"
        )
        uninstrumented(state_before)
        result["environment_before"] = state_before
        if kind == "prepare":
            from lobmm.config import load_config
            from lobmm.synthetic import generate_synthetic_events

            config_path = source / workload["config"]
            require(
                digest(config_path) == workload["config_sha256"],
                "config source changed",
            )
            config = load_config(config_path)
            require(
                config.synthetic.event_count == workload["event_count"]
                and config.random_seed == workload["seed"]
                and config.synthetic.start_timestamp_ns
                == workload["start_timestamp_ns"]
                and config.synthetic.interval_ns == workload["interval_ns"],
                "normalized config differs from frozen workload",
            )
            result["imports_before"] = imports(source, expected_source)
            result["stage"] = "single archived-main input generation"
            dump(result_path, result)
            events = generate_synthetic_events(config)
            rows = [
                {
                    "timestamp_ns": event.timestamp_ns,
                    "sequence_number": event.sequence_number,
                    "event_type": event.event_type.value,
                    "side": None if event.side is None else event.side.value,
                    "price_ticks": event.price_ticks,
                    "quantity": event.quantity,
                }
                for event in events
            ]
            generated = audit.audit_returned(events, rows)
            inputs = audit.prepare_inputs(
                rows, output / "inputs", config.model_dump(mode="json")
            )
            audit_value(generated, inputs["audit"], "events")
            dump(output / "input_manifest.json", inputs)
            result["input_manifest_sha256"] = digest(output / "input_manifest.json")
            result["generated_audit"] = generated
        else:
            from lobmm.data.loaders import load_events
            from lobmm.data.schema import events_to_frame
            from lobmm.enums import EventType, Side
            from lobmm.events import MarketEvent

            expected_inputs = read(output / "input_manifest.json")
            rows, inputs = audit.read_inputs(output / "inputs", expected_inputs)
            result["input_manifest_sha256"] = digest(output / "input_manifest.json")
            result["input_manifest_before"] = object_hash(inputs)
            operation = entry["operation"]
            typed = operation == "typed_events_to_frame"
            events = None
            if typed:
                events = [
                    MarketEvent(
                        timestamp_ns=row["timestamp_ns"],
                        sequence_number=row["sequence_number"],
                        event_type=EventType(row["event_type"]),
                        side=None if row["side"] is None else Side(row["side"]),
                        price_ticks=row["price_ticks"],
                        quantity=row["quantity"],
                    )
                    for row in rows
                ]
                result["prepared_events_before"] = audit.audit_returned(events, rows)
                public = events_to_frame
                call = functools.partial(public, events)
            else:
                filename = {
                    "parquet_load_events": "canonical.parquet",
                    "integer_csv_load_events": "integer.csv",
                    "decimal_csv_load_events": "decimal.csv",
                }[operation]
                public = load_events
                call = functools.partial(public, output / "inputs" / filename)
            result["public_function"] = public.__module__ + "." + public.__qualname__
            result["imports_before"] = imports(source, expected_source)
            result["stage"] = "one public warmup"
            dump(result_path, result)
            warmup = call()
            result["warmup_audit"] = audit.audit_returned(warmup, rows)
            audit_value(
                result["warmup_audit"], inputs["audit"], "frame" if typed else "events"
            )
            del warmup
            result["stage"] = (
                "single public profile call"
                if kind == "profile"
                else "single public timed call"
            )
            dump(result_path, result)
            before_call = diagnostics(
                "after warmup release; before final gc.collect and clocks"
            )
            uninstrumented(before_call)
            result["environment_before_call"] = before_call
            gc.collect()
            if kind == "profile":
                import cProfile

                profiler = cProfile.Profile()
                actual = profiler.runcall(call)
                profiler.dump_stats(directory / "primary.prof")
                result["profile_excluded_from_normal_ratios"] = True
            else:
                cpu_started = time.process_time_ns()
                wall_started = time.perf_counter_ns()
                actual = call()
                wall_ended = time.perf_counter_ns()
                cpu_ended = time.process_time_ns()
                result["wall_ns"] = wall_ended - wall_started
                result["cpu_ns"] = cpu_ended - cpu_started
            result["timed_audit"] = audit.audit_returned(
                actual, rows, directory / "actual.parquet"
            )
            audit_value(
                result["timed_audit"], inputs["audit"], "frame" if typed else "events"
            )
            result["actual_output"] = {
                "path": (directory / "actual.parquet").relative_to(output).as_posix(),
                "sha256": digest(directory / "actual.parquet"),
            }
            if events is not None:
                result["prepared_events_after"] = audit.audit_returned(events, rows)
                require(
                    result["prepared_events_before"] == result["prepared_events_after"],
                    "prepared input events changed",
                )
            _, final_inputs = audit.read_inputs(output / "inputs", expected_inputs)
            result["input_manifest_after"] = object_hash(final_inputs)
            require(
                result["input_manifest_before"] == result["input_manifest_after"],
                "physical inputs changed",
            )
        result["imports_after"] = imports(source, expected_source)
        source_pins(source, expected_source)
        pinned(ROOT, manifest["harness_pins"])
        state_after = diagnostics(
            "after result row/type/book audit and Parquet serialization; before final result JSON"
        )
        uninstrumented(state_after)
        require(
            state_before["thread_environment"] == state_after["thread_environment"]
            and state_before["gc_thresholds"] == state_after["gc_thresholds"]
            and state_before["integer_string_guard"]
            == state_after["integer_string_guard"],
            "worker policy changed",
        )
        result["environment_after"] = state_after
        result["status"] = "complete"
    except BaseException as exc:
        result.update(
            status="failed", error=repr(exc), traceback=traceback.format_exc()
        )
        raise
    finally:
        result["finished_utc"] = timestamp()
        dump(result_path, result)


def launch(
    output: Path, kind: str, sample: int | None = None, remaining: float = WORKER_CAP
) -> dict[str, Any]:
    directory = output / (
        "preparation"
        if kind == "prepare"
        else "profile"
        if kind == "profile"
        else f"samples/{sample:03d}"
    )
    directory.mkdir(parents=True)
    command = [
        sys.executable,
        "-I",
        "-B",
        str(output / "harness" / CONTROLLER),
        "--_worker",
        kind,
        "--_evidence",
        str(output),
    ]
    if sample is not None:
        command.extend(["--_sample", str(sample)])
    process: dict[str, Any] = {
        "command": command,
        "started_utc": timestamp(),
        "cap_seconds": min(WORKER_CAP, remaining),
    }
    started = time.monotonic_ns()
    try:
        with (directory / "worker.log").open("w") as log:
            completed = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=min(WORKER_CAP, remaining),
                check=False,
            )
        process["returncode"] = completed.returncode
        require(completed.returncode == 0, "worker failed: " + directory.name)
        result = read(directory / "result.json")
        require(result["status"] == "complete", "worker did not complete")
        return result
    except BaseException as exc:
        process.update(error=repr(exc), traceback=traceback.format_exc())
        raise
    finally:
        process["elapsed_ns"] = time.monotonic_ns() - started
        process["finished_utc"] = timestamp()
        process["worker_log_sha256"] = digest(directory / "worker.log")
        dump(directory / "process.json", process)


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path != root / "manifest.json"
        and "__pycache__" not in path.parts
    }


def run(output: Path) -> dict[str, Any]:
    contract, _ = protocol(ROOT)
    source_pins(ROOT, contract["source_sha256"]["pr11"])
    pinned(ROOT, contract["protected_pr11_files"])
    require(not output.exists(), "refusing to overwrite any retained evidence")
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "format_version": 1,
        "status": "preparing",
        "started_utc": timestamp(),
        "controller_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "harness_pins": {
            **FROZEN,
            CONTROLLER: digest(ROOT / CONTROLLER),
            ORACLE: digest(ROOT / ORACLE),
        },
        "sources": {},
        "samples": [],
        "profile": None,
        "environment": diagnostics(
            "controller before preparation; never a timed-operation memory measurement"
        ),
    }
    dump(output / "manifest.json", manifest)
    try:
        preparation_started = time.monotonic_ns()
        for name in manifest["harness_pins"]:
            target = output / "harness" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        for role in REVISIONS:
            manifest["sources"][role] = archive(role, output, contract)
        dump(output / "manifest.json", manifest)
        preparation_remaining = (
            WORKER_CAP - (time.monotonic_ns() - preparation_started) / 1_000_000_000
        )
        require(preparation_remaining > 0, "input preparation budget exhausted")
        prepared = launch(output, "prepare", remaining=preparation_remaining)
        manifest["preparation_stage_elapsed_ns"] = (
            time.monotonic_ns() - preparation_started
        )
        require(
            manifest["preparation_stage_elapsed_ns"] <= WORKER_CAP * 1_000_000_000,
            "input preparation budget exceeded",
        )
        manifest["preparation_result"] = "preparation/result.json"
        manifest["input_manifest_sha256"] = prepared["input_manifest_sha256"]
        manifest["status"] = "normal"
        dump(output / "manifest.json", manifest)
        started = time.monotonic_ns()
        try:
            for entry in contract["schedule"]:
                remaining = NORMAL_CAP - (time.monotonic_ns() - started) / 1_000_000_000
                require(remaining > 0, "total normal-stage budget exhausted")
                manifest["samples"].append(
                    {
                        **entry,
                        "directory": f"samples/{entry['sample']:03d}",
                        "status": "started",
                    }
                )
                dump(output / "manifest.json", manifest)
                launch(output, "sample", entry["sample"], remaining)
                manifest["samples"][-1]["status"] = "complete"
                dump(output / "manifest.json", manifest)
            # Independently reread every physical output and every recorded
            # source/import/input/type proof before any decision or profile.
            records = validate_records(
                output, manifest, contract, include_profile=False
            )
            summary = oracle(output / "harness").summarize(records, contract)
            dump(output / "summary.json", summary)
        finally:
            manifest["normal_stage_elapsed_ns"] = time.monotonic_ns() - started
            dump(output / "manifest.json", manifest)
        require(
            manifest["normal_stage_elapsed_ns"] <= NORMAL_CAP * 1_000_000_000,
            "total normal-stage budget exceeded",
        )
        if summary["primary_material_cost"]:
            manifest["profile"] = {
                "status": "started",
                "directory": "profile",
                "reason": "all40 normal complete; frozen primary material-cost trigger true",
            }
            dump(output / "manifest.json", manifest)
            launch(output, "profile")
            manifest["profile"]["status"] = "complete"
        source_pins(ROOT, contract["source_sha256"]["pr11"])
        pinned(ROOT, contract["protected_pr11_files"])
        pinned(ROOT, manifest["harness_pins"])
        manifest["status"] = "complete"
        manifest["finished_utc"] = timestamp()
        manifest["files"] = inventory(output)
        dump(output / "manifest.json", manifest)
        return summary
    except BaseException as exc:
        manifest.update(
            status="failed",
            error=repr(exc),
            traceback=traceback.format_exc(),
            finished_utc=timestamp(),
        )
        manifest["files"] = inventory(output)
        dump(output / "manifest.json", manifest)
        raise


def validate_records(
    output: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    *,
    include_profile: bool,
) -> list[dict[str, Any]]:
    """Read back all raw proofs; shared by the predecision gate and CI."""
    source_pins(ROOT, contract["source_sha256"]["pr11"])
    pinned(ROOT, contract["protected_pr11_files"])
    require(
        manifest["harness_pins"]
        == {
            **FROZEN,
            CONTROLLER: digest(ROOT / CONTROLLER),
            ORACLE: digest(ROOT / ORACLE),
        },
        "executed/current harness differs",
    )
    pinned(output / "harness", manifest["harness_pins"])
    require(set(manifest["sources"]) == set(REVISIONS), "missing source archive")
    for role, entry in manifest["sources"].items():
        require(entry["revision"] == REVISIONS[role], "archive revision differs")
        bundle = output / entry["archive"]
        require(digest(bundle) == entry["archive_sha256"], "archive bytes differ")
        expected = contract["source_sha256"][role]
        source = output / entry["directory"]
        source_pins(source, expected)
        require(
            digest(source / "configs/benchmark.yaml")
            == read(ROOT / (PREFIX + "workload.json"))["config_sha256"],
            "archived benchmark config differs",
        )
        with tarfile.open(bundle) as stream:
            require(
                stream.pax_headers.get("comment") == REVISIONS[role],
                "archive commit metadata differs",
            )
            for name, sha in expected.items():
                item = stream.extractfile(name)
                require(item is not None, "missing source in retained archive")
                assert item is not None
                require(
                    hashlib.sha256(item.read()).hexdigest() == sha,
                    "archive/extracted source differs",
                )
    require(
        manifest["preparation_stage_elapsed_ns"] <= WORKER_CAP * 1_000_000_000,
        "preparation-stage cap exceeded",
    )
    audit = oracle(ROOT)
    expected_inputs = read(output / "input_manifest.json")
    require(
        digest(output / "input_manifest.json") == manifest["input_manifest_sha256"],
        "input manifest changed",
    )
    rows, inputs = audit.read_inputs(output / "inputs", expected_inputs)
    samples = manifest["samples"]
    require(
        [
            {key: sample[key] for key in ("operation", "pair", "role", "sample")}
            for sample in samples
        ]
        == contract["schedule"]
        and all(sample["status"] == "complete" for sample in samples),
        "retained schedule differs",
    )
    retained = [output / "preparation"] + [
        output / sample["directory"] for sample in samples
    ]
    if include_profile and manifest["profile"] is not None:
        require(manifest["profile"]["status"] == "complete", "partial profile")
        retained.append(output / "profile")
    records = []
    for directory in retained:
        record = read(directory / "result.json")
        process = read(directory / "process.json")
        require(
            record["status"] == "complete"
            and process["returncode"] == 0
            and process["elapsed_ns"] <= WORKER_CAP * 1_000_000_000,
            "worker status/cap differs",
        )
        require(
            digest(directory / "worker.log") == process["worker_log_sha256"],
            "worker log differs",
        )
        require(
            record["harness_pins"] == manifest["harness_pins"]
            and record["source_revision"] == REVISIONS[record["role"]],
            "worker pins differ",
        )
        require(
            record["input_manifest_sha256"] == manifest["input_manifest_sha256"],
            "worker input pin differs",
        )
        source = output / manifest["sources"][record["role"]]["directory"]
        expected_source = contract["source_sha256"][record["role"]]
        require(
            record["source_sha256"] == expected_source
            and record["archive_sha256"]
            == manifest["sources"][record["role"]]["archive_sha256"],
            "worker source/archive proofs differ",
        )
        check_imports(record["imports_before"], source, expected_source)
        check_imports(record["imports_after"], source, expected_source)
        for snapshot in (record["imports_before"], record["imports_after"]):
            for entry in snapshot.values():
                require(
                    Path(entry["observed_absolute_path"])
                    == Path(record["observed_source_directory"])
                    / entry["relative_path"],
                    "worker imported outside its observed source root",
                )
        uninstrumented(record["environment_before"])
        uninstrumented(record["environment_after"])
        for key in ("thread_environment", "gc_thresholds", "integer_string_guard"):
            require(
                record["environment_before"][key] == record["environment_after"][key],
                "retained worker policy changed",
            )
        if record.get("kind") == "prepare":
            audit_value(record["generated_audit"], inputs["audit"], "events")
            continue
        require(
            record["input_manifest_before"]
            == record["input_manifest_after"]
            == object_hash(inputs),
            "worker input audit differs",
        )
        uninstrumented(record["environment_before_call"])
        typed = record["operation"] == "typed_events_to_frame"
        representation = "frame" if typed else "events"
        require(
            record["public_function"]
            == (
                "lobmm.data.schema.events_to_frame"
                if typed
                else "lobmm.data.loaders.load_events"
            ),
            "timed public function differs",
        )
        audit_value(record["warmup_audit"], inputs["audit"], representation)
        audit_value(record["timed_audit"], inputs["audit"], representation)
        actual = directory / "actual.parquet"
        require(
            record["actual_output"]
            == {
                "path": actual.relative_to(output).as_posix(),
                "sha256": digest(actual),
            },
            "output file pin differs",
        )
        actual_audit = audit.audit_parquet(actual, rows)
        require(
            all(actual_audit[key] == record["timed_audit"][key] for key in AUDIT_KEYS),
            "physical output audit differs",
        )
        if typed:
            audit_value(record["prepared_events_before"], inputs["audit"], "events")
            require(
                record["prepared_events_before"] == record["prepared_events_after"],
                "prepared typed inputs changed",
            )
        if record.get("kind") != "profile":
            records.append(record)
        else:
            require(
                record["role"] == "pr11"
                and record["operation"] == "parquet_load_events"
                and record["profile_excluded_from_normal_ratios"]
                and (directory / "primary.prof").is_file(),
                "profile provenance missing",
            )
    return records


def verify_evidence(output: Path) -> dict[str, Any]:
    """Read-only verification. No imports or calls to lobmm occur on this path."""
    contract, _ = protocol(ROOT)
    manifest = read(output / "manifest.json")
    require(manifest["status"] == "complete", "evidence run is incomplete/failed")
    require(
        manifest["files"] == inventory(output),
        "retained artifact inventory/hash differs",
    )
    require(
        manifest["normal_stage_elapsed_ns"] <= NORMAL_CAP * 1_000_000_000,
        "normal-stage cap exceeded",
    )
    records = validate_records(output, manifest, contract, include_profile=True)
    audit = oracle(ROOT)
    summary = audit.summarize(records, contract)
    require(
        summary == read(output / "summary.json"), "raw paired arithmetic/gates differ"
    )
    require(
        bool(manifest["profile"]) == summary["primary_material_cost"],
        "conditional profile policy differs",
    )
    return {
        "verified": True,
        "normal_workers": len(records),
        "source_revisions": REVISIONS,
        "source_sha256": contract["source_sha256"],
        "harness_pins": manifest["harness_pins"],
        "input_manifest_sha256": manifest["input_manifest_sha256"],
        "summary": summary,
        "no_application_operations_or_timings": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run",
        action="store_true",
        help="execute the frozen one-time generation and 40-worker schedule",
    )
    mode.add_argument(
        "--verify-evidence",
        type=Path,
        help="read-only retained evidence verification; never runs timings",
    )
    mode.add_argument(
        "--_worker", choices=("prepare", "sample", "profile"), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="new evidence directory required by --run; overwrite refused",
    )
    parser.add_argument("--_evidence", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_sample", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args._worker:
            require(
                args._evidence is not None and args.output is None,
                "invalid internal worker arguments",
            )
            worker(args._evidence.resolve(), args._worker, args._sample)
            return 0
        if args.run:
            require(args.output is not None, "--run requires --output PATH")
            result = run(args.output.resolve())
        else:
            require(
                args.output is None, "verification is read-only; do not pass --output"
            )
            result = verify_evidence(args.verify_evidence.resolve())
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
