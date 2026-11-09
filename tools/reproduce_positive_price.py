"""Frozen positive-price construction probes and seven bounded workers per source.

Baseline uses main once; study rehashes/reuses it and runs only the candidate.
Verify explicitly replicates both sources for CI. All inputs are fictional or
previously exposed synthetic controls; no performance/economic comparison.
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
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f"
PREFIX = "docs/evidence/positive-price-20261109/"
CONTRACT, FIXTURES = PREFIX + "contract.json", PREFIX + "fixtures.json"
CONTRACT_SHA = "8862b5a2d346e65248e8a561c488dfe3c2bdf1b85d54de75a85dd85fe017c037"
FIXTURES_SHA = "96c30af69e182c3218c9f892ac5276f773b3a10393adf0782441869470cf727c"
TEST = "tests/unit/test_positive_price_quotes_independent.py"
PROVIDER = "tools/positive_price_witnesses.py"
TEST_SHA = "40cc751b121c0c23668f6cfd2c42dbf2962a8e210e21634e4734cb8f8614d4c1"
PROVIDER_SHA = "e8ab1ed2e1de58687233fa8b3cf1dc692e4b7d9a62645d9bda5392895af773a8"
POLICIES = ("fixed_spread", "inventory_aware", "microprice")
MAIN_PURE_ERRORS = {
    "fixed_spread": {"flat_low", "low_long_half", "low_short_half", "low_short_cap"},
    "inventory_aware": {"flat_low", "low_long_half", "nonpositive_ask"},
    "microprice": {"flat_low", "low_long_half", "nonpositive_ask"},
}
PLAN = (
    ("corpus", "all"),
    *(("low", policy) for policy in POLICIES),
    *(("normal", policy) for policy in POLICIES),
)
PINS = {
    CONTRACT: CONTRACT_SHA,
    FIXTURES: FIXTURES_SHA,
    TEST: TEST_SHA,
    PROVIDER: PROVIDER_SHA,
}
CLOCKS = {"wall_clock_seconds", "events_per_second", "peak_memory_bytes"}
CAP_SECONDS = 120


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def frozen(root: Path) -> dict[str, str]:
    for name, expected in PINS.items():
        if len(expected) != 64 or digest(root / name) != expected:
            raise RuntimeError("unpinned or changed frozen input: " + name)
    return json.loads((root / CONTRACT).read_text())["source_sha256"]


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
    hashes = {}
    for name in json.loads((ROOT / CONTRACT).read_text())["source_sha256"]:
        source = destination / "src/lobmm" / (name + ".py")
        target = retained / (name + ".py")
        target.parent.mkdir(parents=True, exist_ok=True)
        hashes[name] = digest(source)
        shutil.copy2(source, target)
    for name in PINS:
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
        or len(result["collected_nodeids"]) != 64
        or result["passed"] + result["failed"] != len(result["collected_nodeids"])
    ):
        raise RuntimeError(
            "frozen corpus did not execute completely without errors/skips"
        )
    if role == "main":
        pure_name = (
            TEST
            + "::test_frozen_policy_targets_equal_original_formulas_intersect_positive_domain"
        )
        managed_name = (
            TEST
            + "::test_omitted_bid_uses_existing_cancel_path_and_retains_accepted_ask"
        )
        expected_failures = {
            f"{pure_name}[{mode}-{policy}-{case}]"
            for policy, cases in MAIN_PURE_ERRORS.items()
            for case in cases
            for mode in (True, False)
        } | {
            f"{managed_name}[{mode}-{policy}]"
            for policy in POLICIES
            for mode in (True, False)
        }
        if (
            result["pytest_exit_code"] != 1
            or result["failed"] != 26
            or result["passed"] != 38
            or set(result["failed_nodeids"]) != expected_failures
        ):
            raise RuntimeError(
                "main did not reproduce the exact predicted 26-failure/38-pass matrix"
            )
    if role == "candidate" and (
        result["pytest_exit_code"] != 0 or result["failed"] or result["passed"] != 64
    ):
        raise RuntimeError("candidate failed the unchanged frozen corpus")


def validate_probes(
    probes: dict[str, Any], role: str, fixtures: dict[str, Any]
) -> None:
    expected = [
        (case, policy, mode)
        for case in fixtures["pure_cases"]
        for policy in POLICIES
        for mode in fixtures["post_only_modes"]
    ]
    if [
        (row["name"], row["policy"], row["post_only"]) for row in probes["pure"]
    ] != expected:
        raise RuntimeError("pure probe ledger differs from the frozen matrix")
    if (
        [(row["policy"], row["post_only"]) for row in probes["managed"]]
        != [
            (policy, mode)
            for policy in POLICIES
            for mode in fixtures["managed_case"]["post_only_modes"]
        ]
        or len(probes["validators"]) != 18
        or not all(row["check"] for row in probes["validators"])
    ):
        raise RuntimeError(
            "managed/strict-validator probe coverage or behavior changed"
        )
    for group, stage in (("pure", "desired_quotes"), ("managed", "low_decide")):
        for row in probes[group]:
            if role == "main":
                expected_error = (
                    group == "managed" or row["name"] in MAIN_PURE_ERRORS[row["policy"]]
                )
                if (row["error"] is not None) != expected_error:
                    raise RuntimeError(
                        "main probe error/nonerror matrix differed from the static prediction"
                    )
            if role == "main" and row["error"] is not None:
                if row["error"] != {
                    "type": "ValueError",
                    "message": "desired quote price and quantity must be positive",
                    "stage": stage,
                }:
                    raise RuntimeError("main probe raised an unrelated error")
            elif (
                row["error"] is not None
                or not row["checks"]
                or not all(row["checks"].values())
            ):
                raise RuntimeError(
                    "successful probe did not match every frozen oracle check"
                )
    if role == "candidate":
        for row in probes["pure"]:
            if (
                row["error"] is not None
                or row["actual"] != row["oracle"]
                or not row["checks"]
                or not all(row["checks"].values())
            ):
                raise RuntimeError(
                    "candidate pure targets differ from independent oracle"
                )
        for row in probes["managed"]:
            if (
                row["error"] is not None
                or not row["checks"]
                or not all(row["checks"].values())
            ):
                raise RuntimeError("candidate managed trace differs from frozen oracle")


def corpus(root: Path, output: Path, role: str) -> dict[str, Any]:
    tests = regression(output)
    partial = json.loads(output.read_text())
    partial["regression"] = tests
    dump(output, partial)
    validate_regression(tests, role)
    helpers = runpy.run_path(str(root / TEST))
    probes = helpers["run_policy_probes"](root / FIXTURES)
    probe_path = output.with_suffix(".probes.json")
    dump(probe_path, probes)
    validate_probes(probes, role, json.loads((root / FIXTURES).read_text()))
    return {
        "regression": tests,
        "probes": probes,
        "probe_path": str(probe_path),
        "probe_sha256": digest(probe_path),
    }


def read_run(directory: Path) -> dict[str, Any]:
    tables = {path.name: digest(path) for path in directory.glob("*.parquet")}
    expected = {
        name + ".parquet"
        for name in (
            "orders",
            "fills",
            "inventory",
            "pnl",
            "market",
            "markouts",
            "risk_events",
            "quotes",
        )
    }
    if set(tables) != expected:
        raise RuntimeError(
            "completed replay did not persist exactly eight audit tables"
        )
    files = {
        **tables,
        **{
            name: digest(directory / name)
            for name in (
                "metrics.json",
                "diagnostics.json",
                "summary.json",
                "run_config.yaml",
            )
        },
    }
    return {
        "artifact_directory": str(directory),
        "file_sha256": files,
        "parquet_sha256": tables,
        **{
            name: json.loads((directory / (name + ".json")).read_text())
            for name in ("metrics", "diagnostics", "summary")
        },
    }


def normal(
    root: Path, output: Path, policy: str, modules: dict[str, Any]
) -> dict[str, Any]:
    from lobmm.data import event_stream_sha256
    from lobmm.enums import StrategyName
    from lobmm.synthetic import generate_synthetic_events
    from lobmm.validation import validate_event_stream

    config = modules["config"].load_config(root / "configs/ci.yaml")
    if config.synthetic.event_count != 300 or config.random_seed != 7:
        raise RuntimeError("normal CI300/seed7 configuration changed")
    config = config.model_copy(
        update={
            "strategy": config.strategy.model_copy(
                update={"name": StrategyName(policy)}
            ),
            "backtest": config.backtest.model_copy(update={"track_memory": False}),
            "output": config.output.model_copy(update={"write_plots": False}),
        }
    )
    events = generate_synthetic_events(config)
    validation = validate_event_stream(events, mode=config.data.validation_mode)
    destination = output.with_suffix("")
    partial = json.loads(output.read_text())
    partial["artifact_root"] = str(destination)
    dump(output, partial)
    result = modules["backtest"].run_backtest(
        config,
        events,
        run_name="positive-price-normal-" + policy,
        output_root=destination,
        generate_plots=False,
        input_validation=validation,
        persist_artifacts=True,
    )
    data = read_run(result.run_directory)
    if data["diagnostics"]["events_processed"] != 300:
        raise RuntimeError("normal replay processed a different event count")
    return {
        **data,
        "config_file_sha256": digest(root / "configs/ci.yaml"),
        "config_sha256": object_hash(config.model_dump(mode="json")),
        "event_stream_sha256": event_stream_sha256(events),
    }


def low(root: Path, output: Path, policy: str) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "positive_price_witnesses", root / PROVIDER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the frozen low-tape witness provider")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    destination = output.with_suffix("")
    destination.mkdir()
    witness = module.run_low_case(
        policy, root, destination, fixtures_path=root / FIXTURES
    )
    raw_path = destination / "raw-witness.json"
    dump(raw_path, witness)
    completed = None
    if witness["status"] == "complete":
        if witness["artifact_directory"] is None:
            raise RuntimeError("completed low witness has no persisted audit directory")
        completed = read_run(Path(witness["artifact_directory"]))
        if completed["parquet_sha256"] != witness["parquet_sha256"]:
            raise RuntimeError(
                "low witness hashes differ from actual persisted table bytes"
            )
    return {
        "witness": witness,
        "raw_witness_path": str(raw_path),
        "raw_witness_sha256": digest(raw_path),
        "low_file_sha256": {
            str(path.relative_to(destination)): digest(path)
            for path in destination.rglob("*")
            if path.is_file()
        },
        "low_output": str(destination),
        "completed_low_artifacts": completed,
    }


def validate_low(witness: dict[str, Any], role: str) -> None:
    checks = witness["main_checks"] if role == "main" else witness["candidate_checks"]
    if not checks or not all(checks.values()):
        raise RuntimeError(
            "low-tape actual effect or exact failure context differs from frozen oracle"
        )
    if role == "main" and witness["error"] != {
        "type": "ValueError",
        "message": "desired quote price and quantity must be positive",
    }:
        raise RuntimeError(
            "main low tape did not have the exact predicted constructor error"
        )
    if role == "main" and (
        witness["status"] != "construction_error"
        or witness["failure_timestamp_ns"] != 10
        or witness["attempted_quote"] != {"side": 1, "price_ticks": 0, "quantity": 1}
        or witness["observed_bbo"] != {"bid": 1, "ask": 2}
    ):
        raise RuntimeError("main error was not the exact frozen time/quote/BBO context")
    if role == "candidate" and (
        witness["error"] is not None or witness["status"] != "complete"
    ):
        raise RuntimeError("candidate low tape did not complete")


def worker(
    stage: str,
    root: Path,
    role: str,
    revision: str,
    output: Path,
    kind: str,
    policy: str,
) -> None:
    if not __debug__:
        raise RuntimeError("assertion-enabled Python is required")
    sys.path.insert(0, str(root / "src"))
    expected = frozen(root)
    modules = {
        name: importlib.import_module("lobmm." + name.replace("/", "."))
        for name in expected
    }
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
        "policy": policy,
        "resolved_revision": revision,
        "source_sha256": hashes,
        "imported_sources": {name: str(path) for name, path in paths.items()},
        "harness_sha256": digest(Path(__file__).resolve()),
        "test_sha256": TEST_SHA,
        "provider_sha256": PROVIDER_SHA,
        "contract_sha256": CONTRACT_SHA,
        "fixtures_sha256": FIXTURES_SHA,
        "python": sys.version,
    }
    dump(output, result)
    result.update(
        corpus(root, output, role)
        if kind == "corpus"
        else low(root, output, policy)
        if kind == "low"
        else normal(root, output, policy, modules)
    )
    if kind == "low":
        dump(output, result)
        validate_low(result["witness"], role)
    frozen(root)
    if hashes != {name: digest(path) for name, path in paths.items()} or any(
        Path(module.__file__).resolve() != paths[name]
        for name, module in modules.items()
    ):
        raise RuntimeError("worker production source/import identities changed")
    result["status"] = "complete"
    dump(output, result)


def compare(main: dict[str, Any], candidate: dict[str, Any]) -> None:
    if main["kind"] != candidate["kind"] or main["policy"] != candidate["policy"]:
        raise RuntimeError("source workload ledgers differ")
    if candidate["kind"] == "corpus":
        if (
            main["regression"]["collected_nodeids"]
            != candidate["regression"]["collected_nodeids"]
        ):
            raise RuntimeError("sources collected different frozen corpus nodes")
        for group, keys in (
            (
                "pure",
                (
                    "name",
                    "policy",
                    "post_only",
                    "config",
                    "view",
                    "inventory",
                    "max_abs_inventory",
                    "oracle",
                ),
            ),
            (
                "managed",
                (
                    "policy",
                    "post_only",
                    "config",
                    "initial_view",
                    "low_view",
                    "oracle_initial",
                    "oracle_low",
                    "initial_actions",
                    "accepted_quotes",
                ),
            ),
        ):
            old, new = main["probes"][group], candidate["probes"][group]
            if [{key: row[key] for key in keys} for row in old] != [
                {key: row[key] for key in keys} for row in new
            ]:
                raise RuntimeError(
                    "probe input/oracle/accepted context changed across sources"
                )
        if main["probes"]["validators"] != candidate["probes"]["validators"]:
            raise RuntimeError("strict-validator records changed across sources")
        return
    if candidate["kind"] == "low":
        old, new = main["witness"]["input"], candidate["witness"]["input"]
        for key in (
            "policy",
            "resolved_config",
            "effective_config_sha256",
            "tape",
            "event_stream_sha256",
            "fixture_sha256",
            "contract_sha256",
            "provider_sha256",
            "certificate",
        ):
            if old[key] != new[key]:
                raise RuntimeError(
                    "low input/clock/tape/frozen identity changed: " + key
                )
        return
    for key in (
        "parquet_sha256",
        "config_file_sha256",
        "config_sha256",
        "event_stream_sha256",
        "summary",
    ):
        if main[key] != candidate[key]:
            raise RuntimeError("normal within-policy replay changed: " + key)
    if (
        main["file_sha256"]["run_config.yaml"]
        != candidate["file_sha256"]["run_config.yaml"]
    ):
        raise RuntimeError("normal within-policy saved configuration bytes changed")
    for key in ("diagnostics", "metrics"):
        old, new = deepcopy(main[key]), deepcopy(candidate[key])
        if key == "metrics":
            old, new = old["engineering"], new["engineering"]
        for field in CLOCKS:
            old.pop(field, None)
            new.pop(field, None)
        if key == "metrics":
            old = {**main["metrics"], "engineering": old}
            new = {**candidate["metrics"], "engineering": new}
        if old != new:
            raise RuntimeError("normal within-policy nonclock " + key + " changed")


def baseline_evidence(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = json.loads((directory / "report.json").read_text())
    wanted = {
        "stage": "baseline",
        "status": "complete",
        "criterion_passed": True,
        "harness_sha256": digest(Path(__file__).resolve()),
        "test_sha256": TEST_SHA,
        "provider_sha256": PROVIDER_SHA,
        "contract_sha256": CONTRACT_SHA,
        "fixtures_sha256": FIXTURES_SHA,
    }
    if any(report[key] != value for key, value in wanted.items()):
        raise RuntimeError("baseline completion/runner/frozen-input identity differs")
    expected = frozen(ROOT)
    source = report["sources"]["main"]
    if (
        set(report["sources"]) != {"main"}
        or source["resolved_revision"] != BASELINE
        or source["source_sha256"] != expected
    ):
        raise RuntimeError("baseline does not use the sixteen frozen main source pins")
    for name, sha in expected.items():
        if digest(directory / "sources/main" / (name + ".py")) != sha:
            raise RuntimeError("baseline retained source copy changed")
    if [
        (row["role"], row["kind"], row["policy"], row["status"])
        for row in report["attempts"]
    ] != [("main", kind, policy, "complete") for kind, policy in PLAN] or len(
        report["observations"]
    ) != len(PLAN):
        raise RuntimeError(
            "baseline did not complete exactly the seven planned workers"
        )
    values = {}
    for sample in report["observations"]:
        actual_path = Path(sample["result_path"])
        if digest(actual_path) != sample["result_sha256"] or json.loads(
            actual_path.read_text()
        ) != {
            key: value
            for key, value in sample.items()
            if key not in {"result_path", "result_sha256"}
        }:
            raise RuntimeError("baseline raw and embedded worker records disagree")
        worker_pins = {
            "stage": "baseline",
            "role": "main",
            "status": "complete",
            "resolved_revision": BASELINE,
            "source_sha256": expected,
            **{
                key: wanted[key]
                for key in (
                    "harness_sha256",
                    "test_sha256",
                    "provider_sha256",
                    "contract_sha256",
                    "fixtures_sha256",
                )
            },
        }
        if any(sample[key] != value for key, value in worker_pins.items()):
            raise RuntimeError("baseline worker source/runner/input identity differs")
        if sample["kind"] == "corpus":
            validate_regression(sample["regression"], "main")
            validate_probes(
                sample["probes"], "main", json.loads((ROOT / FIXTURES).read_text())
            )
            if (
                digest(actual_path.with_suffix(".xml"))
                != sample["regression"]["junit_sha256"]
                or digest(Path(sample["probe_path"])) != sample["probe_sha256"]
                or json.loads(Path(sample["probe_path"]).read_text())
                != sample["probes"]
            ):
                raise RuntimeError("baseline corpus JUnit/probe ledger changed")
        elif sample["kind"] == "normal":
            for name, sha in sample["file_sha256"].items():
                if digest(Path(sample["artifact_directory"]) / name) != sha:
                    raise RuntimeError("baseline normal persisted bytes changed")
        else:
            validate_low(sample["witness"], "main")
            if (
                digest(Path(sample["raw_witness_path"])) != sample["raw_witness_sha256"]
                or json.loads(Path(sample["raw_witness_path"]).read_text())
                != sample["witness"]
            ):
                raise RuntimeError("baseline low raw witness changed")
            for name, sha in sample["low_file_sha256"].items():
                if digest(Path(sample["low_output"]) / name) != sha:
                    raise RuntimeError("baseline low partial/observer bytes changed")
        values[sample["kind"] + ":" + sample["policy"]] = sample
    if set(values) != {kind + ":" + policy for kind, policy in PLAN}:
        raise RuntimeError("baseline worker ledger is incomplete or duplicated")
    return values, source


def reproduce(
    stage: str, candidate: str | None, output: Path, reuse: Path | None
) -> int:
    if not __debug__ or (
        output.exists() and (not output.is_dir() or any(output.iterdir()))
    ):
        raise RuntimeError("require assertion-enabled Python and new/empty output")
    expected = frozen(ROOT)
    output.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (Path(__file__), "reproducer.py"),
        *((ROOT / name, Path(name).name) for name in PINS),
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
    target = output / "report.json"
    dump(target, report)
    try:
        previous, previous_source = baseline_evidence(reuse) if reuse else ({}, None)
        revisions = {} if reuse else {"main": BASELINE}
        if stage != "baseline":
            revisions["candidate"] = candidate
        with tempfile.TemporaryDirectory(prefix="lob-positive-price-") as temporary:
            roots = {role: Path(temporary) / role for role in revisions}
            sources = {
                role: archive(revision, roots[role], output / "sources" / role)
                for role, revision in revisions.items()
            }
            if previous_source:
                sources["main"] = previous_source
                report["reused_baseline_evidence"] = str(reuse)
            if sources["main"]["source_sha256"] != expected:
                raise RuntimeError("main source pins differ from frozen contract")
            if "candidate" in sources and any(
                sources["candidate"]["source_sha256"][name] != sha
                for name, sha in expected.items()
                if name not in {"strategies/" + policy for policy in POLICIES}
            ):
                raise RuntimeError(
                    "candidate changed a source outside the three strategies"
                )
            report["sources"] = sources
            dump(target, report)
            for kind, policy in PLAN:
                main = previous.get(kind + ":" + policy)
                for role, revision in revisions.items():
                    result_path = output / f"{kind}-{policy}-{role}.json"
                    attempt = {
                        "role": role,
                        "kind": kind,
                        "policy": policy,
                        "status": "in_progress",
                    }
                    report["attempts"].append(attempt)
                    dump(target, report)
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
                                policy,
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
                        raise RuntimeError(f"worker failed: {kind}/{policy}/{role}")
                    sample = json.loads(result_path.read_text())
                    sample.update(
                        result_path=str(result_path), result_sha256=digest(result_path)
                    )
                    report["observations"].append(sample)
                    attempt["status"] = "complete"
                    dump(target, report)
                    wanted = {
                        "stage": stage,
                        "role": role,
                        "kind": kind,
                        "policy": policy,
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
                            "worker source/import/runner/frozen-input identity mismatch"
                        )
                    if role == "main":
                        main = sample
                    else:
                        compare(main, sample)
                    progress = {
                        "role": role,
                        "kind": kind,
                        "policy": policy,
                        "source_sha256": sample["source_sha256"],
                        "status": "complete",
                    }
                    if kind == "corpus":
                        progress.update(
                            counts={
                                name: sample["regression"][name]
                                for name in ("passed", "failed", "errors", "skipped")
                            },
                            failed_nodeids=sample["regression"]["failed_nodeids"],
                            case_ledger={
                                name: len(rows)
                                for name, rows in sample["probes"].items()
                            },
                        )
                    elif kind == "low":
                        progress["low_checks"] = sample["witness"][
                            "main_checks" if role == "main" else "candidate_checks"
                        ]
                        progress["low_observations"] = {
                            key: sample["witness"][key]
                            for key in (
                                "status",
                                "error",
                                "failure_timestamp_ns",
                                "attempted_quote",
                                "observed_bbo",
                                "prior_accepted_orders",
                            )
                        }
                    print(json.dumps(progress), flush=True)
            report.update(
                status="complete",
                criterion_passed=True,
                scope="Positive-domain construction/management and bounded synthetic execution only; no economic or cancellation-liveness claim.",
            )
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
        dump(target, report)
        print(f"Stopped; retained evidence in {output}: {exc}", file=sys.stderr)
        return 1
    dump(target, report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("baseline", "study", "verify"), required=True
    )
    parser.add_argument("--candidate", metavar="SHA")
    parser.add_argument("--baseline-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--worker",
        nargs=6,
        metavar=("SOURCE", "ROLE", "REVISION", "OUTPUT", "KIND", "POLICY"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        root, role, revision, output, kind, policy = args.worker
        worker(
            args.stage,
            Path(root).resolve(),
            role,
            revision,
            Path(output).resolve(),
            kind,
            policy,
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
