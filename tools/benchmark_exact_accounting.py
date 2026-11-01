"""Bounded paired synthetic accounting benchmark; never a trading throughput SLO.

Run explicitly after installing .[dev]. Timings are machine dependent. This fixed
comparison is deliberately not a default CI timing gate or a parameter search.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import resource
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
ORDER = [
    ("baseline", "candidate"),
    ("candidate", "baseline"),
    ("baseline", "candidate"),
    ("candidate", "baseline"),
    ("baseline", "candidate"),
]
CONTRACT = {
    "scope": "Previously inspected original synthetic engineering workload; no market evidence.",
    "config": "configs/benchmark.yaml",
    "overrides": {
        "synthetic.event_count": 20000,
        "backtest.track_memory": False,
        "output.write_plots": False,
    },
    "random_seed": 7,
    "pair_order": ORDER,
    "worker": "Fresh subprocess, one untimed full warmup then one full timed run.",
    "timer": "run_backtest diagnostics.wall_clock_seconds: scheduler.run only.",
    "excluded_from_timer": "Generation, validation, metrics, serialization and plots.",
    "profiling": False,
    "tracing": False,
    "persist_artifacts": False,
    "per_process_timeout_seconds": 90,
    "total_trial_seconds": 600,
    "checks": "Identical config/event hashes, events/scheduler/fills/volumes/inventory; P&L absolute difference <=1e-9.",
    "investigation_trigger": "Median paired candidate/baseline >=1.5 and every pair >=1.2; engineering investigation threshold only.",
    "stopping": "Stop on mismatch, error or timeout and retain every completed sample and subprocess output; no replacement trials.",
}


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checks(result: Any) -> dict[str, Any]:
    diagnostics = result.diagnostics
    activity = result.metrics["trading_activity"]
    return {
        "event_count": diagnostics["events_processed"],
        "scheduler_count": diagnostics["scheduler_events_processed"],
        "fill_events": activity["fill_events"],
        "buy_volume": activity["buy_volume"],
        "sell_volume": activity["sell_volume"],
        "end_inventory": diagnostics["end_inventory"],
        "pnl": {
            key: result.metrics["pnl"][key]
            for key in ("net_pnl", "gross_pnl", "fees", "rebates")
        },
    }


def worker(root: Path, revision: str, output: Path) -> None:
    # Import the archived revision, never the editable package from this checkout.
    sys.path.insert(0, str(root / "src"))
    import lobmm.portfolio as portfolio_module
    from lobmm.backtest import run_backtest
    from lobmm.config import load_config
    from lobmm.data import event_stream_sha256
    from lobmm.synthetic import generate_synthetic_events
    from lobmm.validation import validate_event_stream

    source = Path(portfolio_module.__file__).resolve()
    if source != (root / "src/lobmm/portfolio.py").resolve():
        raise RuntimeError(f"wrong imported Portfolio source: {source}")
    config = load_config(root / CONTRACT["config"])
    if config.random_seed != CONTRACT["random_seed"]:
        raise RuntimeError("benchmark config changed the frozen random seed")
    config = config.model_copy(
        update={
            "synthetic": config.synthetic.model_copy(update={"event_count": 20000}),
            "backtest": config.backtest.model_copy(update={"track_memory": False}),
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )
    config_text = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    events = generate_synthetic_events(config)
    validation = validate_event_stream(events, mode=config.data.validation_mode)
    with tempfile.TemporaryDirectory(prefix="lob-exact-benchmark-runs-") as temporary:
        options = {
            "output_root": temporary,
            "generate_plots": False,
            "input_validation": validation,
            "persist_artifacts": False,
        }
        warmup = run_backtest(config, events, run_name="warmup", **options)
        expected = checks(warmup)
        del warmup
        gc.collect()
        result = run_backtest(config, events, run_name="timed", **options)
        observed = checks(result)
        if observed != expected:
            raise RuntimeError("warmup and timed run behavior differed")
    dump(
        output,
        {
            "resolved_revision": revision,
            "portfolio_source": str(source),
            "source_sha256": {
                name: digest(root / "src/lobmm" / name)
                for name in ("portfolio.py", "exchange.py")
            },
            "config_file_sha256": digest(root / "configs/benchmark.yaml"),
            "effective_config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
            "event_stream_sha256": event_stream_sha256(events),
            "harness_sha256": digest(Path(__file__).resolve()),
            "elapsed_seconds": result.diagnostics["wall_clock_seconds"],
            "mode": "unprofiled",
            "checks": observed,
            "python": sys.version,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "timing_scope": CONTRACT["timer"],
            "effective_config": json.loads(config_text),
        },
    )


def resolve_archive(reference: str, destination: Path) -> dict[str, Any]:
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", reference + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    archive = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(destination, filter="data")
    return {
        "requested_reference": reference,
        "resolved_revision": revision,
        "git_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "source_sha256": {
            name: digest(destination / "src/lobmm" / name)
            for name in ("portfolio.py", "exchange.py")
        },
    }


def validate_sample(sample: dict[str, Any], reference: dict[str, Any]) -> None:
    elapsed = sample["elapsed_seconds"]
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeError("invalid elapsed time")
    for key in ("effective_config_sha256", "event_stream_sha256"):
        if sample[key] != reference[key]:
            raise RuntimeError(f"input mismatch: {key}")
    for key in (
        "event_count",
        "scheduler_count",
        "fill_events",
        "buy_volume",
        "sell_volume",
        "end_inventory",
    ):
        if sample["checks"][key] != reference["checks"][key]:
            raise RuntimeError(f"behavior mismatch: {key}")
    for key in ("net_pnl", "gross_pnl", "fees", "rebates"):
        # NaN comparisons must fail, rather than slipping through a > check.
        if (
            not abs(sample["checks"]["pnl"][key] - reference["checks"]["pnl"][key])
            <= 1e-9
        ):
            raise RuntimeError(f"P&L mismatch: {key}")


def capture_timeout(exc: subprocess.TimeoutExpired) -> str:
    return "".join(
        value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        for value in (exc.stdout, exc.stderr)
    )


def compare(baseline: str, candidate: str, output: Path) -> int:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"refusing to overwrite retained evidence: {output}")
    output.mkdir(parents=True, exist_ok=True)
    dump(output / "contract.json", CONTRACT)
    shutil.copy2(__file__, output / "benchmark-script.py")
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
        "harness_sha256": digest(Path(__file__).resolve()),
        "contract_sha256": digest(output / "contract.json"),
        "observations": [],
        "attempts": [],
    }
    report_path = output / "report.json"
    dump(report_path, report)
    try:
        with tempfile.TemporaryDirectory(
            prefix="lob-exact-paired-source-"
        ) as temporary:
            paths = {
                label: Path(temporary) / label for label in ("baseline", "candidate")
            }
            sources = {
                label: resolve_archive(reference, paths[label])
                for label, reference in (
                    ("baseline", baseline),
                    ("candidate", candidate),
                )
            }
            report["sources"] = sources
            dump(report_path, report)
            started = time.monotonic()
            reference_sample = None
            for pair, labels in enumerate(ORDER):
                for label in labels:
                    remaining = 600 - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("600-second trial deadline reached")
                    result_path = output / f"{pair}-{label}.json"
                    attempt = {"pair": pair, "label": label, "status": "in_progress"}
                    report["attempts"].append(attempt)
                    dump(report_path, report)
                    try:
                        process = subprocess.run(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "--worker",
                                str(paths[label]),
                                sources[label]["resolved_revision"],
                                str(result_path),
                            ],
                            cwd=paths[label],
                            env=dict(os.environ, PYTHONPATH=str(paths[label] / "src")),
                            capture_output=True,
                            text=True,
                            timeout=min(90, remaining),
                        )
                    except subprocess.TimeoutExpired as exc:
                        result_path.with_suffix(".log").write_text(capture_timeout(exc))
                        attempt["status"] = "timeout"
                        raise
                    result_path.with_suffix(".log").write_text(
                        process.stdout + process.stderr
                    )
                    if process.returncode:
                        attempt["status"] = "worker_failure"
                        attempt["exit_code"] = process.returncode
                        raise RuntimeError(f"worker failed: pair {pair}, {label}")
                    sample = json.loads(result_path.read_text())
                    sample.update(pair=pair, label=label)
                    # Preserve a mismatching completed sample before stopping.
                    report["observations"].append(sample)
                    attempt["status"] = "completed"
                    if (
                        sample["resolved_revision"]
                        != sources[label]["resolved_revision"]
                    ):
                        raise RuntimeError("worker revision identity mismatch")
                    if sample["source_sha256"] != sources[label]["source_sha256"]:
                        raise RuntimeError("worker source hash mismatch")
                    if sample["harness_sha256"] != report["harness_sha256"]:
                        raise RuntimeError("worker harness hash mismatch")
                    if reference_sample is None:
                        reference_sample = sample
                    validate_sample(sample, reference_sample)
                    dump(report_path, report)
                    print(
                        json.dumps(
                            {
                                "pair": pair,
                                "label": label,
                                "seconds": sample["elapsed_seconds"],
                            }
                        ),
                        flush=True,
                    )
            ratios = []
            for pair in range(5):
                rows = {
                    row["label"]: row
                    for row in report["observations"]
                    if row["pair"] == pair
                }
                ratios.append(
                    rows["candidate"]["elapsed_seconds"]
                    / rows["baseline"]["elapsed_seconds"]
                )
            report.update(
                status="complete",
                paired_ratios=ratios,
                median_ratio=median(ratios),
                investigation_trigger=median(ratios) >= 1.5 and min(ratios) >= 1.2,
            )
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        dump(report_path, report)
        print(f"Stopped; retained evidence in {output}: {exc}", file=sys.stderr)
        return 1
    dump(report_path, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "paired_ratios",
                    "median_ratio",
                    "investigation_trigger",
                )
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "experiments/exact-accounting-performance"
    )
    parser.add_argument(
        "--worker",
        nargs=3,
        metavar=("SOURCE", "REVISION", "OUTPUT"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        source, revision, output = args.worker
        worker(Path(source).resolve(), revision, Path(output).resolve())
        return 0
    return compare(args.baseline, args.candidate, args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
