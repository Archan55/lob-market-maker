"""Frozen same-time reporting verification; no timing or market-performance claim.

The local baseline stage precedes implementation. A later study can reuse that
baseline through --baseline-evidence and execute only the fixed candidate cases.
CI can explicitly verify both sources. Retained Nov5 cohorts are local-only and
never regenerated; that stage invokes no engine.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
EVIDENCE = "docs/evidence/same-time-reporting-20261107"
CONTRACT = EVIDENCE + "/contract.json"
FIXTURES = EVIDENCE + "/fixtures.json"
CONTRACT_SHA = "26b51977ca815629bb386333e824cd956384c1d0f15e06025f61ce995dca8685"
FIXTURES_SHA = "01f3229ca7d5ddfeec646cbaaead2f5088360556d8c861e51a5107586acbdefc"
TEST = "tests/unit/test_same_time_report_order_independent.py"
PROVIDER = "tools/same_time_witnesses.py"
TEST_SHA = "adab87cdf49969106ca8d745615000feb7779c001211014238a92aee29606915"
PROVIDER_SHA = "3c65bbc24249de7ed6894f65617fc28d516e04f323ab774cf1d29dbe512d21fb"
MODULES = ("report", "metrics", "backtest", "exchange", "orders", "scheduler", "config")
CASES = ("maker_prefix", "maker_late_tail", "drawdown_order", "taker_phase_control")
PINS = {CONTRACT: CONTRACT_SHA, FIXTURES: FIXTURES_SHA}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def frozen(root: Path) -> None:
    for name, expected in {**PINS, TEST: TEST_SHA, PROVIDER: PROVIDER_SHA}.items():
        if len(expected) != 64 or digest(root / name) != expected:
            raise RuntimeError(f"frozen input hash mismatch or unpinned input: {name}")


def archive(revision: str, destination: Path, retained: Path) -> dict[str, Any]:
    actual = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if actual != revision:
        raise RuntimeError("explicit Git SHA did not resolve to itself")
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
    for name in (*PINS, TEST, PROVIDER):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
        shutil.copy2(ROOT / name, retained / Path(name).name)
    frozen(destination)
    return {
        "resolved_revision": actual,
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


def regression(output: Path) -> dict[str, Any]:
    import pytest

    outcomes = Outcomes()
    code = pytest.main(
        [TEST, "-q", "--tb=short", f"--junitxml={output.with_suffix('.xml')}"],
        plugins=[outcomes],
    )
    return {
        "pytest_exit_code": int(code),
        "collected_nodeids": outcomes.nodes,
        "reports": outcomes.reports,
        "failed_nodeids": list(
            dict.fromkeys(
                item["nodeid"]
                for item in outcomes.reports
                if item["outcome"] == "failed"
            )
        ),
        "passed": sum(
            item["when"] == "call" and item["outcome"] == "passed"
            for item in outcomes.reports
        ),
        "failed": sum(
            item["when"] == "call" and item["outcome"] == "failed"
            for item in outcomes.reports
        ),
        "errors": sum(
            item["when"] != "call" and item["outcome"] == "failed"
            for item in outcomes.reports
        ),
        "skipped": sum(item["outcome"] == "skipped" for item in outcomes.reports),
        "junit_sha256": digest(output.with_suffix(".xml")),
    }


def plots(
    artifacts: Any, output: Path, helpers: dict[str, Any], report: Any
) -> dict[str, Any]:
    output.mkdir(parents=True)
    pnl, market = artifacts.table("pnl"), artifacts.table("market")
    oracle = helpers["ordered_drawdown_oracle"](
        pnl.select("timestamp_ns", "net_pnl").rows()
    )
    drawdown = helpers["capture_drawdown_plot"](artifacts, output / "drawdown.png")
    expected_regime = helpers["regime_increment_oracle"](pnl, market)
    actual_regime = report._pnl_by_regime(pnl, market)
    regime = helpers["capture_regime_plot"](artifacts, output / "regimes.png")
    # Capture helpers retain every actual artist before delegating the original
    # saver. The oracle is scalar ordering/increments, never replacement plotting.
    lines = [line for axis in drawdown["axes"] for line in axis["lines"]]
    curve_matches = (
        len(drawdown["axes"]) == 1
        and len(lines) == 1
        and lines[0]["x"] == oracle["x"]
        and lines[0]["y"] == oracle["y"]
    )
    regime_axes = regime["axes"]
    bars_match = len(regime_axes) == len(expected_regime) and all(
        axis["x_tick_labels"] == list(values)
        and [bar["height"] for bar in axis["bars"]] == list(values.values())
        for axis, values in zip(regime_axes, expected_regime.values(), strict=True)
    )
    return {
        "drawdown": drawdown,
        "drawdown_oracle": oracle,
        "curve_matches": curve_matches,
        "regime": regime,
        "regime_actual": actual_regime,
        "regime_oracle": expected_regime,
        "regime_matches": actual_regime == expected_regime and bars_match,
        "png_sha256": {str(path): digest(path) for path in output.glob("*.png")},
    }


def study_case(
    root: Path,
    output: Path,
    case: str,
    modules: dict[str, Any],
    helpers: dict[str, Any],
) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "same_time_witnesses", root / PROVIDER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the frozen witness provider")
    provider = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = provider
    spec.loader.exec_module(provider)
    destination = output.with_suffix("")
    destination.mkdir()
    raw = provider.run_case(case, root, destination)
    dump(destination / "raw-study.json", raw)
    result = {
        "case": case,
        "witness": raw,
        "raw_study_path": str(destination / "raw-study.json"),
        "raw_study_sha256": digest(destination / "raw-study.json"),
    }
    if raw.get("artifact_directory") is not None:
        artifacts = modules["report"].load_run_artifacts(raw["artifact_directory"])
        result["plots"] = plots(
            artifacts, destination / "plots", helpers, modules["report"]
        )
    return result


def retained_case(
    root: Path,
    output: Path,
    cohort: str,
    modules: dict[str, Any],
    helpers: dict[str, Any],
) -> dict[str, Any]:
    contract = json.loads((root / CONTRACT).read_text())
    inputs = contract["retained_inputs"]
    manifest_path, directory = Path(inputs["manifest"]), Path(inputs[cohort])
    if digest(manifest_path) != inputs["manifest_sha256"]:
        raise RuntimeError("frozen original manifest differs")
    manifest = json.loads(manifest_path.read_text())

    def check(path: Path) -> str:
        expected = manifest["files"][str(path.relative_to(Path(manifest["root"])))]
        actual = digest(path)
        if (
            actual != expected["sha256"]
            or path.stat().st_size != expected["size_bytes"]
        ):
            raise RuntimeError(f"original retained file changed: {path}")
        return actual

    provenance_path = Path(manifest["root"]) / "integration-final/report.json"
    provenance_sha = check(provenance_path)
    provenance = json.loads(provenance_path.read_text())
    original = [
        row
        for row in provenance["observations"]
        if row["label"] == "main" and row["workload"] == cohort
    ]
    if (
        len(original) != 1
        or original[0]["resolved_revision"] != BASELINE
        or Path(original[0]["artifact_root"]) != directory.parent
    ):
        raise RuntimeError("retained cohort differs from original main provenance")
    original = original[0]
    names = (
        "orders",
        "fills",
        "inventory",
        "pnl",
        "market",
        "markouts",
        "risk_events",
        "quotes",
    )
    paths = [directory / (name + ".parquet") for name in names] + [
        directory / name
        for name in (
            "metrics.json",
            "diagnostics.json",
            "summary.json",
            "run_config.yaml",
        )
    ]
    hashes = {path.name: check(path) for path in paths}
    if {
        name: value for name, value in hashes.items() if name.endswith(".parquet")
    } != original["parquet_sha256"]:
        raise RuntimeError("retained tables differ from original worker provenance")
    artifacts = modules["report"].load_run_artifacts(directory)
    config = (
        modules["config"]
        .load_config(directory / "run_config.yaml")
        .model_dump(mode="json")
    )
    if (
        config != original["effective_config"]
        or artifacts.diagnostics["event_stream_sha256"]
        != original["event_stream_sha256"]
    ):
        raise RuntimeError("retained configuration/event identity differs")
    destination = output.with_suffix("")
    destination.mkdir()
    evidence = plots(artifacts, destination / "plots", helpers, modules["report"])
    if any(digest(path) != hashes[path.name] for path in paths):
        raise RuntimeError("retained analysis changed original bytes")
    return {
        "cohort": cohort,
        "input_path": str(directory),
        "input_sha256": hashes,
        "provenance_sha256": provenance_sha,
        "manifest_sha256": digest(manifest_path),
        "event_stream_sha256": original["event_stream_sha256"],
        "plots": evidence,
        "scope": "Previously inspected Nov5 synthetic cohort, read-only local reanalysis; no engine or fresh holdout.",
    }


def worker(
    stage: str, root: Path, role: str, revision: str, output: Path, case: str
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
        "case": case,
        "resolved_revision": revision,
        "source_sha256": sources,
        "imported_sources": {name: str(path) for name, path in paths.items()},
        "harness_sha256": digest(Path(__file__).resolve()),
        "test_sha256": TEST_SHA,
        "provider_sha256": PROVIDER_SHA,
        "contract_sha256": CONTRACT_SHA,
        "fixtures_sha256": FIXTURES_SHA,
        "python": sys.version,
        "integer_string_guard": sys.get_int_max_str_digits(),
    }
    dump(output, result)
    if stage == "regression":
        result.update(regression(output))
    else:
        helpers = runpy.run_path(str(root / TEST))
        result.update(
            retained_case(root, output, case, modules, helpers)
            if stage == "retained"
            else study_case(root, output, case, modules, helpers)
        )
    frozen(root)
    if sources != {name: digest(path) for name, path in paths.items()}:
        raise RuntimeError("archived source changed during worker")
    result["status"] = "complete"
    dump(output, result)


def validate(sample: dict[str, Any]) -> None:
    if sample["stage"] == "regression":
        if (
            sample["pytest_exit_code"] not in (0, 1)
            or sample["errors"]
            or sample["skipped"]
            or not sample["collected_nodeids"]
            or sample["passed"] + sample["failed"] != len(sample["collected_nodeids"])
        ):
            raise RuntimeError(
                "frozen corpus did not execute completely without setup errors/skips"
            )
        if sample["role"] == "candidate" and (
            sample["failed"] or sample["pytest_exit_code"] != 0
        ):
            raise RuntimeError("candidate failed frozen reporting regressions")
        return
    if sample["stage"] != "retained":
        raw = sample["witness"]
        if sample["case"] == "drawdown_order" and not all(
            raw["physical_checks"].values()
        ):
            raise RuntimeError(
                "primary held-inventory/queue/risk reachability oracle failed"
            )
    if (
        "plots" in sample
        and sample["role"] == "candidate"
        and (sample["stage"] == "retained" or sample["case"] == "drawdown_order")
        and (
            not sample["plots"]["curve_matches"]
            or not sample["plots"]["regime_matches"]
        )
    ):
        raise RuntimeError("candidate reporting differs from full-curve/regime oracle")


def compare(main: dict[str, Any], candidate: dict[str, Any]) -> None:
    if candidate["stage"] == "regression":
        if (
            candidate["collected_nodeids"] != main["collected_nodeids"]
            or not main["failed_nodeids"]
        ):
            raise RuntimeError(
                "baseline/candidate regression corpus or declared mismatch differs"
            )
    elif candidate["stage"] == "retained":
        for key in (
            "input_sha256",
            "provenance_sha256",
            "manifest_sha256",
            "event_stream_sha256",
        ):
            if main[key] != candidate[key]:
                raise RuntimeError(f"retained input changed across sources: {key}")
    else:
        if main["witness"]["parquet_sha256"] != candidate["witness"]["parquet_sha256"]:
            raise RuntimeError("same-case persisted Parquet bytes changed")
        if (
            main["witness"]["canonical_effect"]
            != candidate["witness"]["canonical_effect"]
        ):
            raise RuntimeError(
                "same-case execution/metrics/decision/book/scheduler effects changed"
            )


def load_baseline(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = json.loads((directory / "report.json").read_text())
    if report["status"] != "complete" or report["stage"] != "baseline":
        raise RuntimeError(
            "baseline evidence is incomplete or not the preimplementation study"
        )
    for key, expected in (
        ("contract_sha256", CONTRACT_SHA),
        ("fixtures_sha256", FIXTURES_SHA),
        ("test_sha256", TEST_SHA),
        ("provider_sha256", PROVIDER_SHA),
    ):
        if report[key] != expected:
            raise RuntimeError("baseline evidence uses different frozen inputs")
    observations = {}
    for sample in report["observations"]:
        source = Path(sample["result_path"])
        if digest(source) != sample["result_sha256"]:
            raise RuntimeError("retained baseline worker JSON changed")
        raw = Path(sample["raw_study_path"])
        if digest(raw) != sample["raw_study_sha256"]:
            raise RuntimeError("retained baseline raw study JSON changed")
        witness = sample["witness"]
        if json.loads(raw.read_text()) != witness:
            raise RuntimeError("baseline worker and raw witness records disagree")
        if witness["artifact_directory"] is not None:
            directory = Path(witness["artifact_directory"])
            files = {path.name: path for path in directory.glob("*.parquet")}
            if len(files) != 8 or set(files) != set(witness["parquet_sha256"]):
                raise RuntimeError("retained baseline does not have its eight tables")
            if any(
                digest(files[name]) != expected
                for name, expected in witness["parquet_sha256"].items()
            ):
                raise RuntimeError("retained baseline Parquet bytes changed")
        for png, expected in sample.get("plots", {}).get("png_sha256", {}).items():
            if digest(Path(png)) != expected:
                raise RuntimeError("retained baseline PNG changed")
        observations[sample["case"]] = sample
    if set(observations) != set(CASES):
        raise RuntimeError(
            "baseline evidence does not contain exactly the four frozen cases"
        )
    return observations, report["sources"]["main"]


def reproduce(
    stage: str, candidate: str | None, output: Path, baseline_evidence: Path | None
) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError("refusing to overwrite retained evidence")
    frozen(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (Path(__file__), "reproducer.py"),
        *((ROOT / name, Path(name).name) for name in (*PINS, TEST, PROVIDER)),
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
        "provider_sha256": PROVIDER_SHA,
        "contract_sha256": CONTRACT_SHA,
        "fixtures_sha256": FIXTURES_SHA,
        "process_cap_seconds": CAP_SECONDS,
        "attempts": [],
        "observations": [],
    }
    path = output / "report.json"
    dump(path, report)
    try:
        previous, previous_source = (
            load_baseline(baseline_evidence) if baseline_evidence else ({}, None)
        )
        revisions = {} if baseline_evidence else {"main": BASELINE}
        if stage != "baseline":
            revisions["candidate"] = candidate
        with tempfile.TemporaryDirectory(
            prefix="lob-same-time-reporting-"
        ) as temporary:
            roots = {role: Path(temporary) / role for role in revisions}
            sources = {
                role: archive(revision, roots[role], output / "sources" / role)
                for role, revision in revisions.items()
            }
            if previous_source:
                sources["main"] = previous_source
                report["reused_baseline_evidence"] = str(baseline_evidence)
            expected = json.loads((ROOT / CONTRACT).read_text())["source_sha256"]
            if sources["main"]["source_sha256"] != expected:
                raise RuntimeError("main source differs from frozen baseline")
            if "candidate" in sources and any(
                value != sources["candidate"]["source_sha256"][name]
                for name, value in expected.items()
                if name != "report"
            ):
                raise RuntimeError("candidate changed a non-report production source")
            report["sources"] = sources
            dump(path, report)
            cases = (
                ("tests",)
                if stage == "regression"
                else ("ci", "benchmark")
                if stage == "retained"
                else CASES
            )
            for case in cases:
                main = previous.get(case)
                for role, revision in revisions.items():
                    result_path = output / f"{case}-{role}.json"
                    attempt = {"role": role, "case": case, "status": "in_progress"}
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
                                str(result_path),
                                case,
                            ],
                            cwd=roots[role],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=CAP_SECONDS,
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
                        attempt["status"] = "timeout"
                        raise
                    result_path.with_suffix(".log").write_text(
                        process.stdout + process.stderr
                    )
                    if process.returncode:
                        attempt.update(
                            status="worker_failure", exit_code=process.returncode
                        )
                        raise RuntimeError(f"worker failed: {case}/{role}")
                    sample = json.loads(result_path.read_text())
                    sample.update(
                        result_path=str(result_path), result_sha256=digest(result_path)
                    )
                    report["observations"].append(sample)
                    attempt["status"] = "complete"
                    dump(path, report)
                    wanted = {
                        "stage": stage,
                        "role": role,
                        "case": case,
                        "resolved_revision": revision,
                        "source_sha256": sources[role]["source_sha256"],
                        "harness_sha256": report["harness_sha256"],
                        "test_sha256": TEST_SHA,
                        "provider_sha256": PROVIDER_SHA,
                        "contract_sha256": CONTRACT_SHA,
                        "fixtures_sha256": FIXTURES_SHA,
                        "status": "complete",
                    }
                    if any(sample[key] != value for key, value in wanted.items()):
                        raise RuntimeError(
                            "worker source/import/harness/frozen-input identity mismatch"
                        )
                    validate(sample)
                    if role == "main":
                        main = sample
                    else:
                        compare(main, sample)
                    print(
                        json.dumps({"case": case, "role": role, "status": "complete"}),
                        flush=True,
                    )
            report.update(
                status="complete",
                criterion_passed=True,
                scope="Primary report-ordering mechanics only. Secondary fill-reference conventions and retained effect sizes are descriptive, not publication gates or market evidence.",
            )
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
        "--stage",
        choices=("baseline", "regression", "study", "retained"),
        required=True,
    )
    parser.add_argument("--candidate", metavar="SHA")
    parser.add_argument(
        "--baseline-evidence",
        type=Path,
        help="Completed local preimplementation baseline; study then runs candidate cases only.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--worker",
        nargs=5,
        metavar=("SOURCE", "ROLE", "REVISION", "OUTPUT", "CASE"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        root, role, revision, output, case = args.worker
        worker(
            args.stage,
            Path(root).resolve(),
            role,
            revision,
            Path(output).resolve(),
            case,
        )
        return 0
    if args.output is None:
        parser.error("--output is required")
    if args.stage == "baseline":
        if args.candidate is not None:
            parser.error("baseline uses only frozen main")
    elif (
        args.candidate is None
        or len(args.candidate) != 40
        or any(character not in "0123456789abcdef" for character in args.candidate)
    ):
        parser.error("--candidate must be an explicit full lowercase Git SHA")
    if args.baseline_evidence is not None and args.stage != "study":
        parser.error("--baseline-evidence is supported only by study")
    return reproduce(
        args.stage,
        args.candidate,
        args.output.resolve(),
        args.baseline_evidence.resolve() if args.baseline_evidence else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
