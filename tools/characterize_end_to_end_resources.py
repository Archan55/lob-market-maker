"""Frozen whole-lifecycle resource observations and read-only evidence audits.

No application imports, generation or measurements occur on module import.
--write-plan freezes metadata only; --run starts the four fixed workers;
--verify-evidence audits saved observations without starting any worker.
"""

from __future__ import annotations

import argparse
import ast
import copy
import functools
import hashlib
import importlib.metadata
import io
import itertools
import json
import os
import platform
import resource
import shutil
import signal
import struct
import subprocess
import sys
import time
import traceback
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/characterize_end_to_end_resources.py"
TEST = "tests/unit/test_resource_characterization.py"
DESIGN = "docs/END_TO_END_RESOURCE_DESIGN.md"
SOURCE_MANIFEST = "docs/evidence/component-integration-20261115/source-manifests.json"
CONFIG = "configs/benchmark.yaml"
SOURCE_REVISION = "0866bf13e449cd66e3862454eaaf424a5172c33a"
BOUNDARIES = (
    "worker_entry",
    "imports_config_complete",
    "input_complete",
    "scheduler_enter",
    "scheduler_return",
    "outputs_enter",
    "outputs_return",
    "application_complete",
)
STAGES = (
    "bootstrap",
    "input",
    "engine_setup",
    "simulation",
    "postprocessing",
    "persistence",
    "reporting",
)
CLASSES = (
    "input_snapshot",
    "engine_json_yaml",
    "engine_parquet",
    "plot_png",
    "report_summary",
    "cache",
    "bookkeeping",
)
TABLES = (
    "orders",
    "fills",
    "inventory",
    "pnl",
    "quotes",
    "risk_events",
    "market",
    "markouts",
)
RESOURCE_FIELDS = ("wall_clock_seconds", "events_per_second", "peak_memory_bytes")
SIZES = (2000, 20000, 20000, 2000)
RSS_CAP = 1024**3
DISK_CAP = 512 * 1024**2
WORKER_CAP_NS = 120 * 10**9
TOTAL_CAP_NS = 600 * 10**9
SAMPLE_NS = 20_000_000
DISK_POLL_NS = 200_000_000


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append(stream: Any, value: Any) -> None:
    stream.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def parse_proc_status(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "proc_state": None,
        "unit": "kB",
        "vmrss_raw": None,
        "vmhwm_raw": None,
        "vmrss_bytes": None,
        "vmhwm_bytes": None,
    }
    for line in text.splitlines():
        if line.startswith("State:"):
            result["proc_state"] = line.split()[1]
        for label, key in (("VmRSS:", "vmrss"), ("VmHWM:", "vmhwm")):
            if line.startswith(label):
                parts = line.split()
                require(
                    len(parts) == 3 and parts[2] == "kB", "unexpected /proc memory unit"
                )
                require(parts[1].isdigit(), "invalid /proc memory counter")
                result[key + "_raw"] = int(parts[1])
                result[key + "_bytes"] = int(parts[1]) * 1024
    return result


def assign_stage(timestamp_ns: int, boundaries: list[dict[str, Any]]) -> str:
    require(
        [row["name"] for row in boundaries] == list(BOUNDARIES),
        "incomplete or reordered boundaries",
    )
    times = [row["monotonic_ns"] for row in boundaries]
    require(
        all(type(value) is int for value in times)
        and all(a < b for a, b in itertools.pairwise(times)),
        "nonmonotonic boundaries",
    )
    if timestamp_ns < times[0]:
        return "startup"
    for index, stage in enumerate(STAGES):
        if timestamp_ns < times[index + 1]:
            return stage
    return "after_application_complete"


def classify_file(relative_path: str) -> str:
    path = Path(relative_path)
    if path.parts[:1] == ("input",):
        return "input_snapshot"
    if path.parts[:1] == ("cache",):
        return "cache"
    if path.parts[:2] == ("engine", "resource-study"):
        if path.suffix == ".parquet":
            return "engine_parquet"
        if path.suffix == ".png":
            return "plot_png"
        if path.name == "report_summary.txt":
            return "report_summary"
        if path.suffix in (".json", ".yaml"):
            return "engine_json_yaml"
    return "bookkeeping"


def normalize_document(name: str, value: Any) -> Any:
    result = copy.deepcopy(value)
    target = (
        result
        if name == "diagnostics.json"
        else result.get("engineering", {})
        if name == "metrics.json"
        else {}
    )
    for key in RESOURCE_FIELDS:
        target.pop(key, None)
    return result


def check_caps(
    rss_bytes: int | None, logical_bytes: int, elapsed_ns: int, total_elapsed_ns: int
) -> str | None:
    for observed, limit, reason in (
        (rss_bytes or 0, RSS_CAP, "worker_rss_cap"),
        (logical_bytes, DISK_CAP, "output_logical_cap"),
        (elapsed_ns, WORKER_CAP_NS, "worker_time_cap"),
        (total_elapsed_ns, TOTAL_CAP_NS, "total_time_cap"),
    ):
        if observed > limit:
            return reason
    return None


def file_sizes(root: Path) -> dict[str, dict[str, int]]:
    result = {
        name: {"files": 0, "logical_bytes": 0, "allocated_bytes": 0} for name in CLASSES
    }
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            # Live polling may race a task-local cache temporary-file rename.
            # It remains a soft retained-size observation, not peak disk usage.
            continue
        group = result[classify_file(path.relative_to(root).as_posix())]
        group["files"] += 1
        group["logical_bytes"] += stat.st_size
        group["allocated_bytes"] += stat.st_blocks * 512
    return result


def verify_png(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), "invalid PNG signature")
    offset, header, payload, ended = 8, None, bytearray(), False
    while offset < len(data):
        require(offset + 12 <= len(data), "truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        require(end <= len(data), "truncated PNG payload")
        body = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        require(zlib.crc32(kind + body) & 0xFFFFFFFF == crc, "invalid PNG CRC")
        if kind == b"IHDR":
            require(
                header is None and offset == 8 and length == 13, "invalid PNG header"
            )
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            payload.extend(body)
        elif kind == b"IEND":
            require(length == 0 and end == len(data), "invalid PNG ending")
            ended = True
        offset = end
    require(header is not None and ended, "incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = header
    require(
        width > 0
        and height > 0
        and depth == 8
        and color in (2, 6)
        and (compression, filtering, interlace) == (0, 0, 0),
        "unexpected PNG image layout",
    )
    channels = 4 if color == 6 else 3
    row_bytes = width * channels
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(payload) + decoder.flush()
    except zlib.error as exc:
        raise ValueError("invalid PNG zlib data") from exc
    require(
        decoder.eof
        and not decoder.unused_data
        and len(raw) == height * (row_bytes + 1),
        "invalid PNG decoded row length",
    )
    for y in range(height):
        start = y * (row_bytes + 1)
        filter_type = raw[start]
        require(filter_type in range(5), "invalid PNG row filter")
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            require(image.size == (width, height), "decoded PNG dimensions differ")
    except (OSError, SyntaxError) as exc:
        raise ValueError("PNG pixel decoding failed") from exc
    return {
        "valid": True,
        "width": width,
        "height": height,
        "bit_depth": depth,
        "color_type": color,
    }


def source_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted((root / "src/lobmm").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def verify_source(root: Path, pins: dict[str, str]) -> None:
    require(
        len(pins) == 38 and source_inventory(root) == pins, "application source drift"
    )


def plot_names(root: Path) -> list[str]:
    tree = ast.parse((root / "src/lobmm/report.py").read_text())
    node = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "REQUIRED_PLOT_FILENAMES"
    )
    names = list(ast.literal_eval(node.value))
    require(len(names) == len(set(names)) == 12, "unexpected declared plot set")
    return names


def make_plan() -> dict[str, Any]:
    import yaml

    source = read(ROOT / SOURCE_MANIFEST)["c1"]
    verify_source(ROOT, source)
    base = yaml.safe_load((ROOT / CONFIG).read_text())
    require(base["random_seed"] == 7, "benchmark seed drift")
    attempts = []
    for index, count in enumerate(SIZES, 1):
        raw = copy.deepcopy(base)
        raw["synthetic"]["event_count"] = count
        raw.setdefault("backtest", {})["track_memory"] = True
        raw["output"]["write_plots"] = True
        attempts.append(
            {"id": f"{index:03d}", "event_count": count, "effective_input": raw}
        )
    pins = {
        name: digest(ROOT / name)
        for name in (TOOL, TEST, DESIGN, SOURCE_MANIFEST, CONFIG)
    }
    return {
        "version": 1,
        "source_revision": SOURCE_REVISION,
        "source_sha256": source,
        "pins": pins,
        "attempts": attempts,
        "run_name": "resource-study",
        "boundaries": list(BOUNDARIES),
        "plot_names": plot_names(ROOT),
        "sample_period_ns": SAMPLE_NS,
        "disk_poll_period_ns": DISK_POLL_NS,
        "caps": {
            "sampled_worker_rss_bytes": RSS_CAP,
            "attempt_logical_bytes": DISK_CAP,
            "worker_ns": WORKER_CAP_NS,
            "stage_ns": TOTAL_CAP_NS,
        },
    }


def validate_plan(plan: dict[str, Any], root: Path = ROOT) -> None:
    import yaml

    require(
        set(plan["pins"]) == {TOOL, TEST, DESIGN, SOURCE_MANIFEST, CONFIG},
        "frozen input inventory differs",
    )
    require(
        plan["source_revision"] == SOURCE_REVISION
        and [item["event_count"] for item in plan["attempts"]] == list(SIZES),
        "fixed workload drift",
    )
    require(
        [item["id"] for item in plan["attempts"]] == ["001", "002", "003", "004"],
        "attempt order drift",
    )
    require(
        plan["boundaries"] == list(BOUNDARIES)
        and plan["sample_period_ns"] == SAMPLE_NS
        and plan["disk_poll_period_ns"] == DISK_POLL_NS,
        "measurement policy drift",
    )
    require(
        plan["caps"]
        == {
            "sampled_worker_rss_bytes": RSS_CAP,
            "attempt_logical_bytes": DISK_CAP,
            "worker_ns": WORKER_CAP_NS,
            "stage_ns": TOTAL_CAP_NS,
        },
        "cap drift",
    )
    for name, expected in plan["pins"].items():
        require(
            digest(root / name) == expected, "frozen metadata/harness changed: " + name
        )
    require(
        plan["source_sha256"] == read(root / SOURCE_MANIFEST)["c1"],
        "source manifest drift",
    )
    require(plan["run_name"] == "resource-study", "run name drift")
    base = yaml.safe_load((root / CONFIG).read_text())
    require(base["random_seed"] == 7, "seed drift")
    for selected in plan["attempts"]:
        expected = copy.deepcopy(base)
        expected["synthetic"]["event_count"] = selected["event_count"]
        expected.setdefault("backtest", {})["track_memory"] = True
        expected["output"]["write_plots"] = True
        require(
            selected["effective_input"] == expected, "unapproved effective input change"
        )


def imports(source: Path, pins: dict[str, str]) -> dict[str, Any]:
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name != "lobmm" and not name.startswith("lobmm."):
            continue
        path = Path(module.__file__).resolve()
        require(
            path.is_relative_to(source), "editable installation import leak: " + name
        )
        relative = path.relative_to(source).as_posix()
        require(
            relative in pins and digest(path) == pins[relative],
            "import bytes changed: " + name,
        )
        result[name] = {
            "path": str(path),
            "relative_path": relative,
            "sha256": pins[relative],
        }
    require(bool(result), "no application imports recorded")
    return result


def worker(plan_path: Path, attempt_id: str, source: Path, output: Path) -> int:
    require(
        not (output / "result.json").exists()
        and not (output / "boundaries.jsonl").exists(),
        "worker output already used",
    )
    plan = read(plan_path)
    selected = next(item for item in plan["attempts"] if item["id"] == attempt_id)
    result: dict[str, Any] = {
        "status": "started",
        "attempt_id": attempt_id,
        "event_count": selected["event_count"],
        "pid": os.getpid(),
        "source_root": str(source),
        "plan_sha256": digest(plan_path),
    }
    write(output / "result.json", result)
    boundaries: list[dict[str, Any]] = []
    stream = (output / "boundaries.jsonl").open("x")

    def checkpoint(name: str) -> None:
        require(
            len(boundaries) < 8 and name == BOUNDARIES[len(boundaries)],
            "unexpected stage order",
        )
        moment = time.monotonic_ns()
        raw = Path("/proc/self/status").read_text()
        status = parse_proc_status(raw)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        record = {
            "name": name,
            "monotonic_ns": moment,
            "status_raw": raw,
            **status,
            "ru_maxrss_raw": usage.ru_maxrss,
            "ru_maxrss_unit": "KiB",
            "ru_maxrss_bytes": int(usage.ru_maxrss) * 1024,
            "report_imported": "lobmm.report" in sys.modules,
            "matplotlib_imported": "matplotlib" in sys.modules,
            "products": file_sizes(output),
        }
        boundaries.append(record)
        append(stream, record)

    scheduler_original = outputs_original = None
    app = scheduler = None
    try:
        checkpoint("worker_entry")
        require(
            sys.platform.startswith("linux"), "this protocol requires Linux /proc units"
        )
        validate_plan(plan)
        verify_source(source, plan["source_sha256"])
        require(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "premature application import",
        )
        require(
            os.environ.get("MPLCONFIGDIR") == str(output / "cache")
            and not any((output / "cache").iterdir()),
            "font cache is not cold and task-local",
        )
        sys.path.insert(0, str(source / "src"))
        import lobmm.backtest as app
        from lobmm.config import AppConfig
        from lobmm.data.fingerprint import event_stream_sha256
        from lobmm.data.loaders import write_parquet
        from lobmm.scheduler import Scheduler
        from lobmm.synthetic import generate_synthetic_events
        from lobmm.validation import validate_event_stream

        config = AppConfig.model_validate(selected["effective_input"])
        require(
            config.random_seed == 7
            and config.backtest.track_memory
            and config.output.write_plots,
            "effective validated configuration differs",
        )
        require(
            "lobmm.report" not in sys.modules and "matplotlib" not in sys.modules,
            "report imported before reporting stage",
        )
        result["effective_config"] = config.model_dump(mode="json")
        result["imports_before"] = imports(source, plan["source_sha256"])
        result["environment"] = {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
            "thread_environment": {
                key: os.environ.get(key)
                for key in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "POLARS_MAX_THREADS",
                )
            },
            "MPLCONFIGDIR": os.environ["MPLCONFIGDIR"],
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("polars", "pyarrow", "matplotlib", "pydantic")
            },
        }
        checkpoint("imports_config_complete")
        write(output / "result.json", result)
        events = generate_synthetic_events(config)
        write_parquet(events, output / "input/events.parquet")
        certificate = validate_event_stream(events, mode=config.data.validation_mode)
        fingerprint = event_stream_sha256(events)
        require(
            certificate.valid
            and certificate.event_count == selected["event_count"]
            and certificate.reconstructed_book
            and certificate.required_nonempty
            and certificate.event_stream_sha256 == fingerprint,
            "generated input certificate differs",
        )
        result["input"] = {
            "event_count": len(events),
            "event_stream_sha256": fingerprint,
            "certificate": {
                "mode": certificate.mode.value,
                "event_count": certificate.event_count,
                "reconstructed_book": certificate.reconstructed_book,
                "required_nonempty": certificate.required_nonempty,
                "issue_count": certificate.issue_count,
            },
        }
        write(output / "result.json", result)
        scheduler = Scheduler
        scheduler_original, outputs_original = Scheduler.run, app._write_outputs

        @functools.wraps(scheduler_original)
        def observed_scheduler(*args: Any, **kwargs: Any) -> Any:
            checkpoint("scheduler_enter")
            returned = scheduler_original(*args, **kwargs)
            checkpoint("scheduler_return")
            return returned

        @functools.wraps(outputs_original)
        def observed_outputs(*args: Any, **kwargs: Any) -> Any:
            checkpoint("outputs_enter")
            returned = outputs_original(*args, **kwargs)
            checkpoint("outputs_return")
            return returned

        Scheduler.run, app._write_outputs = observed_scheduler, observed_outputs
        checkpoint("input_complete")
        returned = app.run_backtest(
            config,
            events,
            run_name="resource-study",
            output_root=output / "engine",
            generate_plots=True,
            input_validation=certificate,
        )
        checkpoint("application_complete")
        # The headline counters are now durable. No artifact hashing or decoding
        # happens here: all such work waits until all four worker exits.
        result["scheduler_tracemalloc_peak_bytes"] = returned.diagnostics[
            "peak_memory_bytes"
        ]
        result["source_sha256_after"] = source_inventory(source)
        verify_source(source, plan["source_sha256"])
        result["imports_after"] = imports(source, plan["source_sha256"])
        result["status"] = "complete"
        returncode = 0
    except BaseException as exc:
        result.update(
            status="failed", error=repr(exc), traceback=traceback.format_exc()
        )
        returncode = 2
    finally:
        if scheduler is not None and scheduler_original is not None:
            scheduler.run = scheduler_original
        if app is not None and outputs_original is not None:
            app._write_outputs = outputs_original
        stream.close()
        result["boundaries"] = boundaries
        write(output / "result.json", result)
    return returncode


def process_status(pid: int) -> dict[str, Any]:
    try:
        raw = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return {
            "available": False,
            "vmrss_bytes": None,
            "reason": "process status no longer available",
        }
    return {"available": True, "status_raw": raw, **parse_proc_status(raw)}


def observed_children(pid: int) -> list[dict[str, Any]]:
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return []
    return [
        {"pid": int(child), "parent_pid": pid, **process_status(int(child))}
        for child in children
    ]


def terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def monitor(command: list[str], output: Path, stage_started: int) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["MPLCONFIGDIR"] = str(output / "cache")
    started = time.monotonic_ns()
    process_record: dict[str, Any] = {
        "command": command,
        "started_ns": started,
        "sample_period_ns": SAMPLE_NS,
        "disk_poll_period_ns": DISK_POLL_NS,
        "children_scope": "Direct children observed through /proc/PID/task/PID/children; transients may be missed; RSS is never summed",
    }
    with (
        (output / "worker.log").open("x") as log,
        (output / "samples.jsonl").open("x") as samples,
        (output / "disk-polls.jsonl").open("x") as disks,
    ):
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
        process_record["pid"] = process.pid
        next_sample, next_disk, logical = started, started, 0
        try:
            while True:
                status = process_status(process.pid)
                moment = time.monotonic_ns()
                append(
                    samples,
                    {
                        "monotonic_ns": moment,
                        "pid": process.pid,
                        **status,
                        "children": observed_children(process.pid),
                    },
                )
                if moment >= next_disk:
                    sizes = file_sizes(output)
                    logical = sum(group["logical_bytes"] for group in sizes.values())
                    append(
                        disks,
                        {
                            "monotonic_ns": moment,
                            "logical_bytes": logical,
                            "groups": sizes,
                        },
                    )
                    next_disk = moment + DISK_POLL_NS
                reason = check_caps(
                    status.get("vmrss_bytes"),
                    logical,
                    moment - started,
                    moment - stage_started,
                )
                if reason:
                    process_record.update(
                        status="stopped", stop_reason=reason, observed_at_ns=moment
                    )
                    terminate(process)
                    break
                if process.poll() is not None:
                    sizes = file_sizes(output)
                    logical = sum(group["logical_bytes"] for group in sizes.values())
                    append(
                        disks,
                        {
                            "monotonic_ns": time.monotonic_ns(),
                            "logical_bytes": logical,
                            "groups": sizes,
                            "final_exit_observation": True,
                        },
                    )
                    process_record["status"] = (
                        "complete" if process.returncode == 0 else "failed"
                    )
                    break
                next_sample = max(next_sample + SAMPLE_NS, time.monotonic_ns())
                time.sleep(max(0, next_sample - time.monotonic_ns()) / 1e9)
        except BaseException as exc:
            if "stop_reason" not in process_record:
                process_record.update(status="failed", stop_reason=repr(exc))
            process_record.update(
                monitor_exception=repr(exc), traceback=traceback.format_exc()
            )
            terminate(process)
        finally:
            finished = time.monotonic_ns()
            process_record.update(
                returncode=process.wait(),
                finished_ns=finished,
                elapsed_ns=finished - started,
            )
            reason = check_caps(
                None, logical, finished - started, finished - stage_started
            )
            process_record["terminal_cap_reason"] = reason
            if reason and "stop_reason" not in process_record:
                process_record.update(status="stopped", stop_reason=reason)
            write(output / "process.json", process_record)
    return process_record


def json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def input_fingerprint(table: Any) -> str:
    codes = {"ADD": 1, "CANCEL": 2, "TRADE": 3, "RESET": 4, "SNAPSHOT": 5}
    columns = [
        "timestamp_ns",
        "sequence_number",
        "event_type",
        "side",
        "price_ticks",
        "quantity",
    ]
    require(table.column_names == columns, "input schema column order differs")
    book: dict[int, dict[int, int]] = {1: {}, -1: {}}
    hasher = hashlib.sha256()
    previous = (-1, -1)
    for row in table.to_pylist():
        timestamp, sequence, kind, side, price, quantity = [
            row[name] for name in columns
        ]
        require(
            all(type(value) is int for value in (timestamp, sequence, price, quantity)),
            "input integers changed type",
        )
        require(
            timestamp >= previous[0] and sequence > previous[1] and kind in codes,
            "input order or enum differs",
        )
        if kind == "RESET":
            require(side is None and price == quantity == 0, "invalid RESET")
            book = {1: {}, -1: {}}
        else:
            require(
                type(side) is int and side in (1, -1) and price > 0 and quantity > 0,
                "invalid book event",
            )
            before = book[side].get(price, 0)
            after = (
                before + quantity if kind in ("ADD", "SNAPSHOT") else before - quantity
            )
            require(after >= 0, "input book overdraw")
            if after:
                book[side][price] = after
            else:
                book[side].pop(price, None)
            require(
                not book[1] or not book[-1] or max(book[1]) < min(book[-1]),
                "input crossed book",
            )
        hasher.update(
            struct.pack(
                ">qqbbqq",
                timestamp,
                sequence,
                codes[kind],
                0 if side is None else side,
                price,
                quantity,
            )
        )
        previous = timestamp, sequence
    return hasher.hexdigest()


def audit_attempt(
    directory: Path, selected: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    # This function is called only by the controller after all four workers exit,
    # or by the explicit saved-evidence verifier. It imports no application code.
    import pyarrow.parquet as pq
    import yaml

    result, process = read(directory / "result.json"), read(directory / "process.json")
    require(
        result["status"] == process["status"] == "complete"
        and process["returncode"] == 0,
        "worker incomplete",
    )
    require(
        process["pid"] == result["pid"]
        and process["command"][1:3] == ["-I", "-B"]
        and process["elapsed_ns"] == process["finished_ns"] - process["started_ns"]
        and 0 < process["elapsed_ns"] <= WORKER_CAP_NS,
        "worker identity, isolated launch or time cap differs",
    )
    require(
        result["event_count"] == selected["event_count"]
        and result["plan_sha256"] == digest(directory.parent.parent / "plan.json"),
        "worker input provenance differs",
    )
    boundaries = json_lines(directory / "boundaries.jsonl")
    require(
        boundaries == result["boundaries"], "boundary prefix differs from final record"
    )
    assign_stage(boundaries[-1]["monotonic_ns"], boundaries)
    require(
        process["started_ns"]
        <= boundaries[0]["monotonic_ns"]
        < boundaries[-1]["monotonic_ns"]
        <= process["finished_ns"],
        "boundary lifetime differs",
    )
    for field in ("vmhwm_bytes", "ru_maxrss_bytes"):
        values = [row[field] for row in boundaries]
        require(
            all(type(value) is int and value >= 0 for value in values)
            and all(a <= b for a, b in itertools.pairwise(values)),
            "HWM counter is not cumulative",
        )
    for row in boundaries:
        parsed = parse_proc_status(row["status_raw"])
        require(
            all(row[key] == value for key, value in parsed.items())
            and row["ru_maxrss_bytes"] == row["ru_maxrss_raw"] * 1024,
            "raw boundary units differ",
        )
        require(
            row["vmrss_bytes"] is not None and row["ru_maxrss_unit"] == "KiB",
            "missing boundary counter",
        )
    require(
        not any(
            row["report_imported"] or row["matplotlib_imported"]
            for row in boundaries[:-1]
        )
        and boundaries[-1]["report_imported"]
        and boundaries[-1]["matplotlib_imported"],
        "lazy reporting boundary differs",
    )
    samples = json_lines(directory / "samples.jsonl")
    require(
        samples and any(row.get("vmrss_bytes") is not None for row in samples),
        "no usable RSS samples",
    )
    require(
        all(
            a["monotonic_ns"] < b["monotonic_ns"]
            for a, b in itertools.pairwise(samples)
        ),
        "sample times moved backward",
    )
    for row in samples:
        require(
            row["pid"] == process["pid"]
            and process["started_ns"] <= row["monotonic_ns"] <= process["finished_ns"],
            "sampler PID or lifetime differs",
        )
        if row["available"]:
            require(
                all(
                    row[key] == value
                    for key, value in parse_proc_status(row["status_raw"]).items()
                ),
                "sample raw counters differ",
            )
        else:
            require(row.get("vmrss_bytes") is None, "unavailable sample fabricated RSS")
        require((row.get("vmrss_bytes") or 0) <= RSS_CAP, "sampled RSS cap exceeded")
        for child in row["children"]:
            require(
                child["parent_pid"] == process["pid"]
                and child["pid"] != process["pid"],
                "invalid separately recorded child",
            )
            if child["available"]:
                require(
                    all(
                        child[key] == value
                        for key, value in parse_proc_status(child["status_raw"]).items()
                    ),
                    "child raw counters differ",
                )
    disks = json_lines(directory / "disk-polls.jsonl")
    require(
        bool(disks) and disks[-1].get("final_exit_observation") is True,
        "missing terminal disk observation",
    )
    require(
        process["sample_period_ns"] == SAMPLE_NS
        and process["disk_poll_period_ns"] == DISK_POLL_NS,
        "polling targets differ",
    )
    require(
        all(
            a["monotonic_ns"] < b["monotonic_ns"] for a, b in itertools.pairwise(disks)
        ),
        "disk polling times moved backward",
    )
    for row in disks:
        require(
            row["logical_bytes"]
            == sum(group["logical_bytes"] for group in row["groups"].values())
            and row["logical_bytes"] <= DISK_CAP
            and process["started_ns"] <= row["monotonic_ns"] <= process["finished_ns"],
            "disk arithmetic or cap differs",
        )
        require(
            set(row["groups"]) == set(CLASSES)
            and all(
                type(group[key]) is int and group[key] >= 0
                for group in row["groups"].values()
                for key in ("files", "logical_bytes", "allocated_bytes")
            ),
            "invalid recorded file-size groups",
        )
    intervals: dict[str, Any] = {}
    for label in ("startup", *STAGES, "after_application_complete"):
        subset = [
            row
            for row in samples
            if assign_stage(row["monotonic_ns"], boundaries) == label
        ]
        rss = [
            row["vmrss_bytes"] for row in subset if row.get("vmrss_bytes") is not None
        ]
        intervals[label] = {
            "periodic_samples": len(subset),
            "usable_rss_samples": len(rss),
            "peak_sampled_rss_bytes": max(rss, default=None),
        }
    peaks = [
        row["peak_sampled_rss_bytes"]
        for name, row in intervals.items()
        if name != "after_application_complete"
        and row["peak_sampled_rss_bytes"] is not None
    ]
    require(
        bool(peaks), "no usable lifecycle RSS samples before application completion"
    )
    maximum = max(peaks)
    for phase in ("imports_before", "imports_after"):
        require(bool(result[phase]), "missing import proof")
        for name, item in result[phase].items():
            require(name == "lobmm" or name.startswith("lobmm."), "unexpected module")
            require(
                item["sha256"] == plan["source_sha256"][item["relative_path"]]
                and Path(item["path"])
                == Path(result["source_root"]) / item["relative_path"],
                "import source leak",
            )
    verify_source(directory / "source", plan["source_sha256"])
    require(
        result["source_sha256_after"] == plan["source_sha256"],
        "source changed after application",
    )
    input_table = pq.read_table(directory / "input/events.parquet")
    require(
        input_table.num_rows == selected["event_count"]
        and input_fingerprint(input_table) == result["input"]["event_stream_sha256"],
        "physical input fingerprint differs",
    )
    engine = directory / "engine/resource-study"
    table_records = {}
    for name in TABLES:
        path = engine / (name + ".parquet")
        table = pq.read_table(path)
        table_records[name] = {
            "sha256": digest(path),
            "rows": table.num_rows,
            "schema": [[field.name, str(field.type)] for field in table.schema],
        }
    require(
        sorted(path.name for path in engine.glob("*.parquet"))
        == sorted(name + ".parquet" for name in TABLES),
        "unexpected engine table set",
    )
    require(
        sorted(path.name for path in (engine / "plots").glob("*.png"))
        == sorted(plan["plot_names"]),
        "plot set differs",
    )
    pngs = {name: verify_png(engine / "plots" / name) for name in plan["plot_names"]}
    require((engine / "report_summary.txt").stat().st_size > 0, "empty report summary")
    documents = {
        name: yaml.safe_load((engine / name).read_text())
        if name.endswith("yaml")
        else read(engine / name)
        for name in (
            "diagnostics.json",
            "metrics.json",
            "summary.json",
            "run_config.yaml",
        )
    }
    require(
        documents["run_config.yaml"] == result["effective_config"],
        "effective config differs from persisted config",
    )
    require(
        documents["diagnostics.json"]["event_stream_sha256"]
        == result["input"]["event_stream_sha256"],
        "engine/input fingerprint differs",
    )
    require(
        documents["diagnostics.json"]["peak_memory_bytes"]
        == result["scheduler_tracemalloc_peak_bytes"],
        "scheduler tracemalloc scalar differs",
    )
    return {
        "attempt_id": selected["id"],
        "event_count": selected["event_count"],
        "input_sha256": digest(directory / "input/events.parquet"),
        "event_stream_sha256": result["input"]["event_stream_sha256"],
        "tables": table_records,
        "pngs": pngs,
        "report_summary_sha256": digest(engine / "report_summary.txt"),
        "normalized_documents": {
            name: normalize_document(name, value) for name, value in documents.items()
        },
        "boundaries": boundaries,
        "periodic_intervals": intervals,
        "largest_observed_intervals": [
            name
            for name, value in intervals.items()
            if name != "after_application_complete"
            and value["peak_sampled_rss_bytes"] == maximum
        ],
        "headline_peak_sampled_rss_bytes": maximum,
        "sample_scope": "Headline includes explicitly labeled startup and lifecycle samples before application_complete; post-application source/provenance bookkeeping is retained separately and excluded; no child RSS sums",
        "primary_post_scheduler_hwm_increase": boundaries[-1]["ru_maxrss_bytes"]
        > boundaries[4]["ru_maxrss_bytes"],
        "secondary_vmhwm_increase": boundaries[-1]["vmhwm_bytes"]
        > boundaries[4]["vmhwm_bytes"],
        "scheduler_tracemalloc_peak_bytes": result["scheduler_tracemalloc_peak_bytes"],
    }


def original_stats(root: Path) -> dict[str, Any]:
    return {
        path.relative_to(root).as_posix(): {
            "sha256": digest(path),
            "logical_bytes": path.stat().st_size,
            "allocated_blocks": path.stat().st_blocks,
            "allocated_bytes": path.stat().st_blocks * 512,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path != root / "manifest.json"
        and "__pycache__" not in path.parts
    }


def saved_allocated_totals(stats: dict[str, Any]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    for relative, entry in stats.items():
        parts = Path(relative).parts
        if len(parts) < 3 or parts[0] != "attempts":
            continue
        require(
            entry["allocated_bytes"] == entry["allocated_blocks"] * 512,
            "original block units differ",
        )
        group = totals.setdefault(parts[1], {}).setdefault(
            classify_file(Path(*parts[2:]).as_posix()),
            {"files": 0, "logical_bytes": 0, "allocated_bytes": 0},
        )
        group["files"] += 1
        group["logical_bytes"] += entry["logical_bytes"]
        group["allocated_bytes"] += entry["allocated_bytes"]
    return totals


def audit_all(output: Path, plan: dict[str, Any]) -> dict[str, Any]:
    manifest = read(output / "manifest.json")
    require(
        manifest["worker_stage_elapsed_ns"]
        == manifest["worker_stage_finished_ns"] - manifest["worker_stage_started_ns"]
        and 0 < manifest["worker_stage_elapsed_ns"] <= TOTAL_CAP_NS,
        "stage time arithmetic or cap differs",
    )
    previous_end = manifest["worker_stage_started_ns"]
    for selected in plan["attempts"]:
        process = read(output / "attempts" / selected["id"] / "process.json")
        require(
            previous_end
            <= process["started_ns"]
            < process["finished_ns"]
            <= manifest["worker_stage_finished_ns"],
            "workers overlap or escape the recorded stage",
        )
        previous_end = process["finished_ns"]
    results = [
        audit_attempt(output / "attempts" / item["id"], item, plan)
        for item in plan["attempts"]
    ]
    comparisons = []
    for count in (2000, 20000):
        first, second = [row for row in results if row["event_count"] == count]
        checks = {
            name: first[name] == second[name]
            for name in (
                "input_sha256",
                "event_stream_sha256",
                "tables",
                "normalized_documents",
                "report_summary_sha256",
            )
        }
        require(
            all(checks.values()), f"replicate behavior/bytes differ for {count} events"
        )
        comparisons.append(
            {
                "event_count": count,
                "attempts": [first["attempt_id"], second["attempt_id"]],
                "checks": checks,
            }
        )
    return {
        "valid": True,
        "attempts": results,
        "replicate_comparisons": comparisons,
        "primary_any_post_scheduler_hwm_increase": any(
            row["primary_post_scheduler_hwm_increase"] for row in results
        ),
        "scope": "Descriptive four-run characterization through application completion; no optimization, scaling, capacity or economic claim",
    }


def run(plan_path: Path, output: Path) -> dict[str, Any]:
    plan = read(plan_path)
    validate_plan(plan)
    verify_source(ROOT, plan["source_sha256"])
    require(not output.exists(), "refusing to overwrite any previous attempt")
    output.mkdir(parents=True)
    shutil.copy2(plan_path, output / "plan.json")
    for relative in plan["pins"]:
        destination = output / "harness" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    manifest: dict[str, Any] = {
        "status": "started",
        "plan_sha256": digest(output / "plan.json"),
        "attempts": [],
        "controller_excluded_from_worker_rss": True,
    }
    write(output / "manifest.json", manifest)
    started = time.monotonic_ns()
    manifest["worker_stage_started_ns"] = started
    write(output / "manifest.json", manifest)
    try:
        for selected in plan["attempts"]:
            require(
                time.monotonic_ns() - started <= TOTAL_CAP_NS,
                "total stage cap before next worker",
            )
            directory = output / "attempts" / selected["id"]
            (directory / "cache").mkdir(parents=True)
            for relative in plan["source_sha256"]:
                target = directory / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            command = [
                sys.executable,
                "-I",
                "-B",
                str(output / "harness" / TOOL),
                "--worker",
                "--plan",
                str(output / "plan.json"),
                "--attempt-id",
                selected["id"],
                "--source-root",
                str(directory / "source"),
                "--output",
                str(directory),
            ]
            manifest["attempts"].append({"id": selected["id"], "status": "started"})
            write(output / "manifest.json", manifest)
            process = monitor(command, directory, started)
            manifest["attempts"][-1]["status"] = process["status"]
            write(output / "manifest.json", manifest)
            require(
                process["status"] == "complete",
                "worker stopped; no retry: " + selected["id"],
            )
        manifest["worker_stage_finished_ns"] = time.monotonic_ns()
        manifest["worker_stage_elapsed_ns"] = (
            manifest["worker_stage_finished_ns"] - started
        )
        require(manifest["worker_stage_elapsed_ns"] <= TOTAL_CAP_NS, "total stage cap")
        write(output / "manifest.json", manifest)
        # Every worker has exited before the first artifact readback/hash audit.
        report = audit_all(output, plan)
        write(output / "report.json", report)
        stats = original_stats(output)
        manifest.update(
            status="complete",
            original_file_stats=stats,
            original_file_totals=saved_allocated_totals(stats),
        )
        write(output / "manifest.json", manifest)
        return report
    except BaseException as exc:
        manifest.update(
            status="stopped",
            error=repr(exc),
            traceback=traceback.format_exc(),
            audit_policy="No automatic retry; no deep artifact audit on an incomplete worker prefix",
        )
        write(output / "manifest.json", manifest)
        raise


def verify_evidence(output: Path) -> dict[str, Any]:
    """Read-only: no /proc, resource counters, application calls or new workers."""
    manifest, plan = read(output / "manifest.json"), read(output / "plan.json")
    require(
        manifest["status"] == "complete"
        and manifest["plan_sha256"] == digest(output / "plan.json"),
        "incomplete or changed plan",
    )
    require(
        manifest["worker_stage_elapsed_ns"]
        == manifest["worker_stage_finished_ns"] - manifest["worker_stage_started_ns"]
        and 0 < manifest["worker_stage_elapsed_ns"] <= TOTAL_CAP_NS,
        "saved total-stage cap differs",
    )
    validate_plan(plan)
    validate_plan(plan, output / "harness")
    require(
        [row["id"] for row in manifest["attempts"]] == ["001", "002", "003", "004"]
        and all(row["status"] == "complete" for row in manifest["attempts"]),
        "attempt ledger differs",
    )
    stats = manifest["original_file_stats"]
    files = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
        and path != output / "manifest.json"
        and "__pycache__" not in path.parts
    }
    require(files == set(stats), "saved artifact inventory differs")
    for relative, saved in stats.items():
        path = output / relative
        require(
            path.stat().st_size == saved["logical_bytes"]
            and digest(path) == saved["sha256"],
            "saved artifact bytes differ: " + relative,
        )
    require(
        saved_allocated_totals(stats) == manifest["original_file_totals"],
        "original allocation arithmetic differs",
    )
    report = audit_all(output, plan)
    require(
        report == read(output / "report.json"), "saved observation/audit result differs"
    )
    return {
        "verified": True,
        "no_new_resource_measurements_or_workers": True,
        "plan_sha256": manifest["plan_sha256"],
        "source_sha256": plan["source_sha256"],
        "original_file_totals": manifest["original_file_totals"],
        "report": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-plan", type=Path)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--verify-evidence", type=Path)
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--attempt-id", choices=("001", "002", "003", "004"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    try:
        if args.write_plan is not None:
            require(not args.write_plan.exists(), "refusing to overwrite a master plan")
            write(args.write_plan, make_plan())
            print(
                json.dumps(
                    {"plan": str(args.write_plan), "sha256": digest(args.write_plan)}
                )
            )
            return 0
        if args.worker:
            require(
                args.plan is not None
                and args.output is not None
                and args.source_root is not None
                and args.attempt_id is not None,
                "missing worker arguments",
            )
            return worker(
                args.plan.resolve(),
                args.attempt_id,
                args.source_root.resolve(),
                args.output.resolve(),
            )
        if args.verify_evidence is not None:
            result = verify_evidence(args.verify_evidence.resolve())
        else:
            require(
                args.plan is not None and args.output is not None,
                "--run needs a frozen plan and fresh output",
            )
            result = run(args.plan.resolve(), args.output.resolve())
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
