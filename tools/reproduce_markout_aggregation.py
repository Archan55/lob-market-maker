"""Frozen markout aggregation verification on fictional/exposed synthetic inputs.

Baseline and study each execute one fixture/regression worker and one CI300
worker. Study reuses checked baseline evidence rather than rerunning it. Verify
explicitly replicates both sources for CI. Retained reanalysis is local-only;
it never regenerates the original Nov5 cohorts or runs a20k engine.
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
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
EVIDENCE = "docs/evidence/markout-aggregation-20261108"
CONTRACT, FIXTURES = EVIDENCE + "/contract.json", EVIDENCE + "/fixtures.json"
CONTRACT_SHA = "f01dfc365c4784b363c856dc2895d55609cd8249d140759eb159cf3336bd7ddf"
FIXTURES_SHA = "06999812efaab9357948bf3dd1de463f3dc2166b6d4786115f9d578a9446c5a5"
TEST = "tests/unit/test_markout_aggregation_independent.py"
TEST_SHA = "f25de34f65497fef3df68306f81206737e520421b43804faa268a6a02b9eca29"
MODULES = ("metrics", "backtest", "report", "exchange", "orders", "scheduler", "config")
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
CLOCKS = {"wall_clock_seconds", "events_per_second", "peak_memory_bytes"}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, Fraction):
        return {"fraction": [value.numerator, value.denominator]}
    raise TypeError(f"unsupported evidence value: {type(value)}")


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, default=encode, indent=2, allow_nan=False) + "\n")


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            default=encode,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def frozen(root: Path) -> None:
    for name, expected in (
        (CONTRACT, CONTRACT_SHA),
        (FIXTURES, FIXTURES_SHA),
        (TEST, TEST_SHA),
    ):
        if len(expected) != 64 or digest(root / name) != expected:
            raise RuntimeError(f"unpinned or changed frozen input: {name}")


def archive(revision: str, destination: Path, retained: Path) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    if resolved != revision:
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
    for name in (CONTRACT, FIXTURES, TEST):
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
                row["nodeid"] for row in outcomes.reports if row["outcome"] == "failed"
            )
        ),
        "passed": sum(
            row["when"] == "call" and row["outcome"] == "passed"
            for row in outcomes.reports
        ),
        "failed": sum(
            row["when"] == "call" and row["outcome"] == "failed"
            for row in outcomes.reports
        ),
        "errors": sum(
            row["when"] != "call" and row["outcome"] == "failed"
            for row in outcomes.reports
        ),
        "skipped": sum(row["outcome"] == "skipped" for row in outcomes.reports),
        "junit_sha256": digest(output.with_suffix(".xml")),
    }


def validate_regression(result: dict[str, Any], role: str) -> None:
    if (
        result["pytest_exit_code"] not in (0, 1)
        or result["errors"]
        or result["skipped"]
        or not result["collected_nodeids"]
        or result["passed"] + result["failed"] != len(result["collected_nodeids"])
    ):
        raise RuntimeError(
            "frozen corpus did not execute completely without errors/skips"
        )
    if role == "candidate" and (result["pytest_exit_code"] != 0 or result["failed"]):
        raise RuntimeError(
            "candidate failed the unchanged independent regression corpus"
        )
    if role == "main" and not result["failed_nodeids"]:
        raise RuntimeError("main did not reproduce any frozen aggregation mismatch")


def stripped(
    metrics: dict[str, Any], *, candidate: bool, clocks: bool
) -> dict[str, Any]:
    value = deepcopy(metrics)
    if candidate:
        quality, attribution = value["execution_quality"], value["attribution"]
        if attribution["adverse_selection_markout"] is not None:
            raise RuntimeError(
                "unidentified adverse-selection scalar was not made null"
            )
        attribution["adverse_selection_markout"] = attribution.pop(
            "legacy_pooled_markout_proxy"
        )
        attribution.pop("adverse_selection_markout_note")
        quality.pop("markout_summary_by_horizon_ns")
        quality.pop("markout_aggregation_note")
    if clocks:
        for name in CLOCKS:
            value["engineering"].pop(name, None)
    return value


def metric_observation(
    fills: list[dict[str, Any]],
    markouts: list[dict[str, Any]],
    metrics: dict[str, Any],
    helpers: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    oracle = helpers["independent_quantity_summary"](fills, markouts)
    proxy = helpers["expected_legacy_proxy"](fills, markouts)
    means = helpers["expected_event_means"](markouts)
    proxy_field = (
        "legacy_pooled_markout_proxy"
        if role == "candidate"
        else "adverse_selection_markout"
    )
    if metrics["attribution"][proxy_field] != proxy or any(
        metrics["execution_quality"][field] != expected
        for field, expected in means.items()
    ):
        raise RuntimeError(
            "legacy means/proxy differ from the independent old-formula oracle"
        )
    if role == "candidate":
        helpers["assert_metrics_match"](fills, markouts, metrics)
    return {
        "metrics": metrics,
        "quantity_oracle": oracle,
        "legacy_proxy_oracle": proxy,
        "legacy_means_oracle": means,
        "raw_markouts": markouts,
        "raw_markouts_sha256": object_hash(markouts),
        "fills_sha256": object_hash(fills),
    }


def fixture_worker(
    root: Path,
    output: Path,
    modules: dict[str, Any],
    helpers: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    tests = regression(output)
    partial = json.loads(output.read_text())
    partial["regression"] = tests
    dump(output, partial)
    validate_regression(tests, role)
    ledger: dict[str, Any] = {}
    ledger_path = output.with_suffix(".fixtures.json")
    frozen_cases = helpers["load_cases"](root / FIXTURES)
    if set(frozen_cases) != set(json.loads((root / FIXTURES).read_text())["cases"]):
        raise RuntimeError("fixture provider changed the frozen case ledger")
    for name in frozen_cases:
        case = helpers["build_case"](name, root / FIXTURES)
        args = case["build_args"]
        before = object_hash(args)
        metrics = modules["metrics"].build_metrics(**args)
        if object_hash(args) != before:
            raise RuntimeError("fixture metrics mutated their input rows")
        ledger[name] = {
            "build_args_sha256": before,
            **metric_observation(
                case["fills"], case["markouts"], metrics, helpers, role
            ),
        }
        dump(ledger_path, ledger)
    return {
        "regression": tests,
        "fixture_ledger": ledger,
        "fixture_ledger_path": str(ledger_path),
        "fixture_ledger_sha256": digest(ledger_path),
    }


def read_run(directory: Path) -> tuple[dict[str, Any], dict[str, str]]:
    import polars as pl

    expected = {name + ".parquet" for name in TABLES}
    if {path.name for path in directory.glob("*.parquet")} != expected:
        raise RuntimeError("expected exactly the eight frozen audit tables")
    files = [directory / name for name in sorted(expected)] + [
        directory / name
        for name in (
            "run_config.yaml",
            "metrics.json",
            "diagnostics.json",
            "summary.json",
        )
    ]
    hashes = {path.name: digest(path) for path in files}
    value = {
        "tables": {
            name: pl.read_parquet(directory / (name + ".parquet")).to_dicts()
            for name in TABLES
        },
        **{
            name: json.loads((directory / (name + ".json")).read_text())
            for name in ("metrics", "diagnostics", "summary")
        },
    }
    return value, hashes


def build_from_tables(
    data: dict[str, Any], config: Any, modules: dict[str, Any]
) -> dict[str, Any]:
    tables = data["tables"]
    return modules["metrics"].build_metrics(
        orders=tables["orders"],
        fills=tables["fills"],
        inventory=tables["inventory"],
        pnl=tables["pnl"],
        market_states=tables["market"],
        markouts=tables["markouts"],
        risk_events=tables["risk_events"],
        quote_messages=tables["quotes"],
        diagnostics=data["diagnostics"],
        tick_size=config.instrument.tick_size,
        near_limit_fraction=config.metrics.near_limit_fraction,
    )


def ci_worker(
    root: Path,
    output: Path,
    modules: dict[str, Any],
    helpers: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    from lobmm.data import event_stream_sha256
    from lobmm.synthetic import generate_synthetic_events
    from lobmm.validation import validate_event_stream

    config = modules["config"].load_config(root / "configs/ci.yaml")
    if config.random_seed != 7 or config.synthetic.event_count != 300:
        raise RuntimeError("fixed CI300/seed7 configuration changed")
    config = config.model_copy(
        update={
            "backtest": config.backtest.model_copy(update={"track_memory": False}),
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )
    events = generate_synthetic_events(config)
    certificate = validate_event_stream(events, mode=config.data.validation_mode)
    destination = Path(
        tempfile.mkdtemp(prefix=output.stem + "-artifacts-", dir=output.parent)
    )
    partial = json.loads(output.read_text())
    partial["artifact_root"] = str(destination)
    dump(output, partial)
    result = modules["backtest"].run_backtest(
        config,
        events,
        run_name="markout-aggregation-ci",
        output_root=destination,
        generate_plots=False,
        input_validation=certificate,
        persist_artifacts=True,
    )
    data, hashes = read_run(result.run_directory)
    if result.diagnostics["events_processed"] != 300:
        raise RuntimeError("CI wiring did not process the frozen event count")
    return {
        "artifact_directory": str(result.run_directory),
        "input_sha256": hashes,
        "parquet_sha256": {
            name: value for name, value in hashes.items() if name.endswith(".parquet")
        },
        "config_file_sha256": digest(root / "configs/ci.yaml"),
        "effective_config_sha256": object_hash(config.model_dump(mode="json")),
        "event_stream_sha256": event_stream_sha256(events),
        "diagnostics": data["diagnostics"],
        "summary": data["summary"],
        **metric_observation(
            data["tables"]["fills"],
            data["tables"]["markouts"],
            data["metrics"],
            helpers,
            role,
        ),
    }


def retained_worker(
    root: Path,
    output: Path,
    cohort: str,
    modules: dict[str, Any],
    helpers: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    inputs = json.loads((root / CONTRACT).read_text())["retained_inputs"]
    directory, manifest_path = Path(inputs[cohort]), Path(inputs["manifest"])
    if digest(manifest_path) != inputs["manifest_sha256"]:
        raise RuntimeError("original retained manifest differs from frozen contract")
    manifest = json.loads(manifest_path.read_text())

    def checked(path: Path) -> str:
        expected = manifest["files"][str(path.relative_to(Path(manifest["root"])))]
        value = digest(path)
        if value != expected["sha256"] or path.stat().st_size != expected["size_bytes"]:
            raise RuntimeError(f"original retained file changed: {path}")
        return value

    provenance_path = Path(manifest["root"]) / "integration-final/report.json"
    provenance_sha = checked(provenance_path)
    provenance = json.loads(provenance_path.read_text())
    rows = [
        row
        for row in provenance["observations"]
        if row["label"] == "main" and row["workload"] == cohort
    ]
    if (
        provenance["status"] != "complete"
        or not provenance["criterion_passed"]
        or len(rows) != 1
    ):
        raise RuntimeError("original completed main provenance is absent or ambiguous")
    original = rows[0]
    if (
        original["resolved_revision"] != BASELINE
        or Path(original["artifact_root"]) != directory.parent
        or original["source_sha256"] != provenance["sources"]["main"]["source_sha256"]
    ):
        raise RuntimeError("retained cohort source/path identity differs")
    data, hashes = read_run(directory)
    for name, value in hashes.items():
        if checked(directory / name) != value:
            raise RuntimeError("retained bytes changed during input loading")
    if {
        name: value for name, value in hashes.items() if name.endswith(".parquet")
    } != original["parquet_sha256"]:
        raise RuntimeError(
            "retained audit tables differ from original worker fingerprints"
        )
    config = modules["config"].load_config(directory / "run_config.yaml")
    if (
        config.model_dump(mode="json") != original["effective_config"]
        or data["diagnostics"]["event_stream_sha256"] != original["event_stream_sha256"]
    ):
        raise RuntimeError("retained effective configuration/event identity differs")
    if any(
        data["metrics"][key] != value
        for key, value in original["selected_metrics"].items()
    ):
        raise RuntimeError("retained original metrics differ from prior provenance")
    before = object_hash(data["tables"])
    metrics = build_from_tables(data, config, modules)
    observation = metric_observation(
        data["tables"]["fills"], data["tables"]["markouts"], metrics, helpers, role
    )
    if object_hash(data["tables"]) != before or any(
        digest(directory / name) != value for name, value in hashes.items()
    ):
        raise RuntimeError("retained reanalysis changed original rows or bytes")
    return {
        "cohort": cohort,
        "input_path": str(directory),
        "input_sha256": hashes,
        "provenance_sha256": provenance_sha,
        "manifest_sha256": digest(manifest_path),
        "effective_config_sha256": object_hash(config.model_dump(mode="json")),
        "event_stream_sha256": original["event_stream_sha256"],
        "original_metrics": data["metrics"],
        "scope": "Already inspected Nov5 synthetic cohort, local-only read-only metrics reanalysis; no new engine.",
        **observation,
    }


def worker(
    stage: str,
    root: Path,
    role: str,
    revision: str,
    output: Path,
    kind: str,
    cohort: str,
) -> None:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required")
    sys.path.insert(0, str(root / "src"))
    frozen(root)
    modules = {name: importlib.import_module("lobmm." + name) for name in MODULES}
    paths = {name: Path(module.__file__).resolve() for name, module in modules.items()}
    if any(
        path != (root / "src/lobmm" / (name + ".py")).resolve()
        for name, path in paths.items()
    ):
        raise RuntimeError("worker imported an external editable production module")
    hashes = {name: digest(path) for name, path in paths.items()}
    result = {
        "status": "in_progress",
        "stage": stage,
        "role": role,
        "kind": kind,
        "cohort": cohort,
        "resolved_revision": revision,
        "source_sha256": hashes,
        "imported_sources": {name: str(path) for name, path in paths.items()},
        "harness_sha256": digest(Path(__file__).resolve()),
        "test_sha256": TEST_SHA,
        "contract_sha256": CONTRACT_SHA,
        "fixtures_sha256": FIXTURES_SHA,
        "python": sys.version,
        "integer_string_guard": sys.get_int_max_str_digits(),
    }
    dump(output, result)
    helpers = runpy.run_path(str(root / TEST))
    details = (
        fixture_worker(root, output, modules, helpers, role)
        if kind == "fixtures"
        else ci_worker(root, output, modules, helpers, role)
        if kind == "ci"
        else retained_worker(root, output, cohort, modules, helpers, role)
    )
    result.update(details)
    frozen(root)
    if hashes != {name: digest(path) for name, path in paths.items()} or any(
        Path(module.__file__).resolve() != paths[name]
        for name, module in modules.items()
    ):
        raise RuntimeError("worker source/import identities changed")
    result["status"] = "complete"
    dump(output, result)


def equal_metrics(
    main: dict[str, Any], candidate: dict[str, Any], *, clocks: bool
) -> None:
    if stripped(main, candidate=False, clocks=clocks) != stripped(
        candidate, candidate=True, clocks=clocks
    ):
        raise RuntimeError("legacy means/proxy or non-markout metric fields changed")


def compare(main: dict[str, Any], candidate: dict[str, Any]) -> None:
    if main["kind"] != candidate["kind"] or main["cohort"] != candidate["cohort"]:
        raise RuntimeError("source workload ledgers differ")
    if candidate["kind"] == "fixtures":
        if (
            main["regression"]["collected_nodeids"]
            != candidate["regression"]["collected_nodeids"]
        ):
            raise RuntimeError("sources collected different frozen regression nodes")
        old, new = main["fixture_ledger"], candidate["fixture_ledger"]
        if list(old) != list(new):
            raise RuntimeError("source fixture case ledgers differ")
        for name in old:
            for key in (
                "build_args_sha256",
                "raw_markouts",
                "raw_markouts_sha256",
                "fills_sha256",
                "quantity_oracle",
                "legacy_proxy_oracle",
                "legacy_means_oracle",
            ):
                if old[name][key] != new[name][key]:
                    raise RuntimeError(
                        f"fixture raw rows/input/oracle changed: {name}/{key}"
                    )
            equal_metrics(old[name]["metrics"], new[name]["metrics"], clocks=False)
        return
    for key in (
        "raw_markouts",
        "raw_markouts_sha256",
        "fills_sha256",
        "quantity_oracle",
        "legacy_proxy_oracle",
        "legacy_means_oracle",
        "effective_config_sha256",
        "event_stream_sha256",
    ):
        if main[key] != candidate[key]:
            raise RuntimeError(f"source input/raw markout/oracle mismatch: {key}")
    retained = candidate["kind"] == "retained"
    for key in (
        ("input_sha256", "provenance_sha256", "manifest_sha256")
        if retained
        else ("parquet_sha256", "config_file_sha256", "summary")
    ):
        if main[key] != candidate[key]:
            raise RuntimeError(f"source artifact mismatch: {key}")
    if retained and main["metrics"] != main["original_metrics"]:
        raise RuntimeError("main failed to reproduce the original retained metrics")
    if not retained and {
        key: value for key, value in main["diagnostics"].items() if key not in CLOCKS
    } != {
        key: value
        for key, value in candidate["diagnostics"].items()
        if key not in CLOCKS
    }:
        raise RuntimeError("nonclock replay diagnostics changed")
    equal_metrics(main["metrics"], candidate["metrics"], clocks=not retained)


def baseline_evidence(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = json.loads((directory / "report.json").read_text())
    if (
        report["stage"] != "baseline"
        or report["status"] != "complete"
        or report.get("criterion_passed") is not True
    ):
        raise RuntimeError(
            "baseline evidence did not complete the prescribed source run"
        )
    if report["harness_sha256"] != digest(Path(__file__).resolve()):
        raise RuntimeError("baseline evidence uses a different frozen runner")
    for key, expected in (
        ("contract_sha256", CONTRACT_SHA),
        ("fixtures_sha256", FIXTURES_SHA),
        ("test_sha256", TEST_SHA),
    ):
        if report[key] != expected:
            raise RuntimeError("baseline frozen inputs differ")
    source = report["sources"]["main"]
    expected_sources = json.loads((ROOT / CONTRACT).read_text())["source_sha256"]
    if (
        set(report["sources"]) != {"main"}
        or source["resolved_revision"] != BASELINE
        or source["source_sha256"] != expected_sources
    ):
        raise RuntimeError(
            "baseline evidence does not use the seven frozen main sources"
        )
    for name, expected in expected_sources.items():
        if digest(directory / "sources/main" / (name + ".py")) != expected:
            raise RuntimeError("baseline retained source copy changed")
    if [
        (row["role"], row["kind"], row["cohort"], row["status"])
        for row in report["attempts"]
    ] != [("main", "fixtures", "all", "complete"), ("main", "ci", "ci", "complete")]:
        raise RuntimeError(
            "baseline did not complete exactly the two prescribed workers"
        )
    if len(report["observations"]) != 2:
        raise RuntimeError("baseline contains an unexpected worker count")
    observations = {}
    for sample in report["observations"]:
        result_path = Path(sample["result_path"])
        if digest(result_path) != sample["result_sha256"]:
            raise RuntimeError("baseline worker JSON changed")
        if json.loads(result_path.read_text()) != {
            key: value
            for key, value in sample.items()
            if key not in {"result_path", "result_sha256"}
        }:
            raise RuntimeError("baseline embedded and raw worker records disagree")
        wanted = {
            "role": "main",
            "stage": "baseline",
            "status": "complete",
            "resolved_revision": BASELINE,
            "source_sha256": expected_sources,
            "harness_sha256": report["harness_sha256"],
            "test_sha256": TEST_SHA,
            "contract_sha256": CONTRACT_SHA,
            "fixtures_sha256": FIXTURES_SHA,
        }
        if any(sample[key] != expected for key, expected in wanted.items()):
            raise RuntimeError(
                "baseline worker source/harness/frozen-input identity differs"
            )
        if sample["kind"] == "fixtures":
            validate_regression(sample["regression"], "main")
            if (
                digest(Path(sample["fixture_ledger_path"]))
                != sample["fixture_ledger_sha256"]
            ):
                raise RuntimeError("baseline fixture ledger changed")
            if (
                digest(Path(sample["result_path"]).with_suffix(".xml"))
                != sample["regression"]["junit_sha256"]
            ):
                raise RuntimeError("baseline regression JUnit changed")
        if sample["kind"] == "ci":
            for name, expected in sample["input_sha256"].items():
                if digest(Path(sample["artifact_directory"]) / name) != expected:
                    raise RuntimeError("baseline CI audit/config/metrics bytes changed")
        observations[sample["kind"] + ":" + sample["cohort"]] = sample
    if set(observations) != {"fixtures:all", "ci:ci"}:
        raise RuntimeError(
            "baseline evidence does not contain exactly the two planned workers"
        )
    return observations, source


def reproduce(
    stage: str, candidate: str | None, output: Path, reuse: Path | None
) -> int:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError("refusing to overwrite retained evidence")
    frozen(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (Path(__file__), "reproducer.py"),
        (ROOT / CONTRACT, "contract.json"),
        (ROOT / FIXTURES, "fixtures.json"),
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
        "fixtures_sha256": FIXTURES_SHA,
        "process_cap_seconds": CAP_SECONDS,
        "attempts": [],
        "observations": [],
    }
    path = output / "report.json"
    dump(path, report)
    try:
        previous, previous_source = baseline_evidence(reuse) if reuse else ({}, None)
        revisions = {} if reuse else {"main": BASELINE}
        if stage != "baseline":
            revisions["candidate"] = candidate
        with tempfile.TemporaryDirectory(
            prefix="lob-markout-aggregation-"
        ) as temporary:
            roots = {role: Path(temporary) / role for role in revisions}
            sources = {
                role: archive(revision, roots[role], output / "sources" / role)
                for role, revision in revisions.items()
            }
            if previous_source:
                sources["main"] = previous_source
                report["reused_baseline_evidence"] = str(reuse)
            expected = json.loads((ROOT / CONTRACT).read_text())["source_sha256"]
            if sources["main"]["source_sha256"] != expected:
                raise RuntimeError("main archive differs from frozen source pins")
            if "candidate" in sources and any(
                sources["candidate"]["source_sha256"][name] != value
                for name, value in expected.items()
                if name != "metrics"
            ):
                raise RuntimeError("candidate changed a non-metrics production source")
            report["sources"] = sources
            dump(path, report)
            plan = (
                (("retained", "ci"), ("retained", "benchmark"))
                if stage == "retained"
                else (("fixtures", "all"), ("ci", "ci"))
            )
            for kind, cohort in plan:
                main = previous.get(kind + ":" + cohort)
                for role, revision in revisions.items():
                    result_path = output / f"{kind}-{cohort}-{role}.json"
                    attempt = {
                        "role": role,
                        "kind": kind,
                        "cohort": cohort,
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
                                str(result_path),
                                kind,
                                cohort,
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
                        raise RuntimeError(f"worker failed: {kind}/{cohort}/{role}")
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
                        "kind": kind,
                        "cohort": cohort,
                        "resolved_revision": revision,
                        "source_sha256": sources[role]["source_sha256"],
                        "harness_sha256": report["harness_sha256"],
                        "test_sha256": TEST_SHA,
                        "contract_sha256": CONTRACT_SHA,
                        "fixtures_sha256": FIXTURES_SHA,
                        "status": "complete",
                    }
                    if any(sample[key] != value for key, value in wanted.items()):
                        raise RuntimeError(
                            "worker source/import/frozen-input/harness identity mismatch"
                        )
                    if role == "main":
                        main = sample
                    else:
                        compare(main, sample)
                    progress = {
                        "kind": kind,
                        "cohort": cohort,
                        "role": role,
                        "source_sha256": sample["source_sha256"],
                        "status": "complete",
                    }
                    if kind == "fixtures":
                        progress.update(
                            case_ledger=list(sample["fixture_ledger"]),
                            regression_counts={
                                name: sample["regression"][name]
                                for name in ("passed", "failed", "errors", "skipped")
                            },
                            failed_nodeids=sample["regression"]["failed_nodeids"],
                        )
                    print(json.dumps(progress), flush=True)
            report.update(
                status="complete",
                criterion_passed=True,
                scope="Quantity-supported observed markout diagnostics on fictional/exposed synthetic inputs only; retained effects are descriptive, no economic or performance claim.",
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
        "--stage", choices=("baseline", "study", "verify", "retained"), required=True
    )
    parser.add_argument("--candidate", metavar="SHA")
    parser.add_argument("--baseline-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--worker",
        nargs=6,
        metavar=("SOURCE", "ROLE", "REVISION", "OUTPUT", "KIND", "COHORT"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        root, role, revision, output, kind, cohort = args.worker
        worker(
            args.stage,
            Path(root).resolve(),
            role,
            revision,
            Path(output).resolve(),
            kind,
            cohort,
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
    if (args.stage == "study") != (args.baseline_evidence is not None):
        parser.error(
            "study requires --baseline-evidence; other stages do not accept it"
        )
    return reproduce(
        args.stage,
        args.candidate,
        args.output.resolve(),
        args.baseline_evidence.resolve() if args.baseline_evidence else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
