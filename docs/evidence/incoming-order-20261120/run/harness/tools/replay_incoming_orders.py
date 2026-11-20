"""Fixed synthetic incoming-order replay over separate displayed books.

This research program imports no application code. Expected outputs are read
only by the evaluator after both independently allocated matching worlds return.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/replay_incoming_orders.py"
TEST = "tests/unit/test_incoming_replay.py"
DESIGN = "docs/INCOMING_REPLAY_DESIGN.md"
CASES = "docs/evidence/incoming-order-20261120/cases.json"
BUILDER = "docs/evidence/incoming-order-20261120/build_cases.py"
SOURCE_PLAN = "docs/evidence/end-to-end-resources-20261117/run/plan.json"
SOURCE_PLAN_SHA = "ffcfa3bae32b4263c9e6d681b2291ffad69b0732f35e38ff62bf942792ef62b0"
FIXTURE_SHA = "23b6f928ae94609decfceb86e1c435f8806bb05fb21fe2eb8baf8e9b1efb1b2e"
BASE_COMMIT = "2a96738c080b1a62a32e4306721de703b242477a"
SOURCE_REVISION = "0866bf13e449cd66e3862454eaaf424a5172c33a"
WORLDS = ("baseline", "counterfactual")
PINS = (TOOL, TEST, DESIGN, CASES, BUILDER)
LIMITS = {
    "cases": 64,
    "activated_units_per_case": 64,
    "activations_per_case": 8,
    "incoming_per_case": 8,
    "worker_seconds": 120,
    "controller_seconds": 180,
    "evidence_bytes": 16 * 1024**2,
}
STOP_RESERVE = 65536
ACTIVATION_FIELDS = {
    "event_id",
    "kind",
    "order_id",
    "owner",
    "participant_id",
    "price_ticks",
    "quantity",
    "timestamp_ns",
    "sequence",
}
INCOMING_FIELDS = {
    "event_id",
    "kind",
    "aggressor_side",
    "participant_id",
    "quantity",
    "limit_price_ticks",
    "timestamp_ns",
    "sequence",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def positive(value: Any) -> bool:
    return type(value) is int and value > 0


def nonempty(value: Any) -> bool:
    return type(value) is str and bool(value)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def validate_case(case: dict[str, Any]) -> None:
    """Admit only the complete declared one-sided, fully displayed contract."""
    require(
        type(case) is dict and nonempty(case.get("case_id")), "invalid case identity"
    )
    require(case.get("resting_side") in ("bid", "ask"), "invalid resting side")
    events = case.get("events")
    require(type(events) is list, "events must be an ordered list")
    event_ids: set[str] = set()
    maker_ids: set[str] = set()
    makers: set[str] = set()
    aggressors: set[str] = set()
    previous = (-1, -1)
    activations = incoming = units = 0
    for event in events:
        require(type(event) is dict, "event must be a mapping")
        kind = event.get("kind")
        require(kind in ("activate", "incoming"), "unsupported event kind")
        expected_fields = ACTIVATION_FIELDS if kind == "activate" else INCOMING_FIELDS
        require(set(event) == expected_fields, "unsupported or missing event fields")
        identifier = event["event_id"]
        require(
            nonempty(identifier) and identifier not in event_ids,
            "empty/duplicate event ID",
        )
        event_ids.add(identifier)
        timestamp, sequence = event["timestamp_ns"], event["sequence"]
        require(
            type(timestamp) is int
            and timestamp >= 0
            and type(sequence) is int
            and sequence >= 0,
            "event clocks must be nonnegative builtin integers",
        )
        key = timestamp, sequence
        require(
            key > previous,
            "event chronology must be strictly ordered; no duplicate keys",
        )
        previous = key
        require(nonempty(event["participant_id"]), "participant ID must be nonempty")
        require(
            positive(event["quantity"]), "quantity must be a positive builtin integer"
        )
        if kind == "activate":
            identifier = event["order_id"]
            require(
                nonempty(identifier) and identifier not in maker_ids,
                "empty/duplicate maker order ID",
            )
            maker_ids.add(identifier)
            require(event["owner"] in ("external", "own"), "unsupported maker owner")
            require(
                positive(event["price_ticks"]),
                "maker price must be a positive builtin integer",
            )
            makers.add(event["participant_id"])
            activations += 1
            units += event["quantity"]
        else:
            required_side = "sell" if case["resting_side"] == "bid" else "buy"
            require(
                event["aggressor_side"] == required_side,
                "aggressor consumes the wrong book side",
            )
            limit = event["limit_price_ticks"]
            require(
                limit is None or positive(limit),
                "hard limit must be null or a positive builtin integer",
            )
            aggressors.add(event["participant_id"])
            incoming += 1
    require(
        not (makers & aggressors),
        "same-participant matching is outside the excluded-STP contract",
    )
    require(
        activations <= LIMITS["activations_per_case"]
        and incoming <= LIMITS["incoming_per_case"]
        and units <= LIMITS["activated_units_per_case"],
        "case resource cap",
    )


def replay_world(case: dict[str, Any], world: str) -> dict[str, Any]:
    """Match aggregate resting quantities with one mutable incoming budget.

    This function never reads case.expected or expected_hybrid_errors. A new
    book is allocated for every invocation, including each paired world.
    """
    validate_case(case)
    require(world in WORLDS, "unknown matching world")
    bid = case["resting_side"] == "bid"
    book: dict[str, dict[str, Any]] = {}
    trace = []
    totals = dict.fromkeys(
        (
            "requested_quantity",
            "external_quantity",
            "own_quantity",
            "unfilled_quantity",
        ),
        0,
    )
    for event in case["events"]:
        fills: list[dict[str, Any]] = []
        external = own = 0
        unfilled: int | None = None
        if event["kind"] == "activate":
            if world == "counterfactual" or event["owner"] == "external":
                book[event["order_id"]] = {
                    "price_ticks": event["price_ticks"],
                    "owner": event["owner"],
                    "entered": event["quantity"],
                    "remaining": event["quantity"],
                    "filled": 0,
                    "activation": (event["timestamp_ns"], event["sequence"]),
                }
        else:
            budget = event["quantity"]
            limit = event["limit_price_ticks"]
            priority = sorted(
                book,
                key=lambda identifier: (
                    -book[identifier]["price_ticks"]
                    if bid
                    else book[identifier]["price_ticks"],
                    *book[identifier]["activation"],
                ),
            )
            for identifier in priority:
                if budget == 0:
                    break
                order = book[identifier]
                eligible = limit is None or (
                    order["price_ticks"] >= limit
                    if bid
                    else order["price_ticks"] <= limit
                )
                if not order["remaining"] or not eligible:
                    continue
                quantity = min(order["remaining"], budget)
                order["remaining"] -= quantity
                order["filled"] += quantity
                budget -= quantity
                fills.append(
                    {
                        "order_id": identifier,
                        "quantity": quantity,
                        "price_ticks": order["price_ticks"],
                        "owner": order["owner"],
                    }
                )
                if order["owner"] == "own":
                    own += quantity
                else:
                    external += quantity
            unfilled = budget  # IOC terminal residual is never added to this book.
            require(
                event["quantity"] == external + own + unfilled,
                "incoming quantity was not conserved",
            )
            totals["requested_quantity"] += event["quantity"]
            totals["external_quantity"] += external
            totals["own_quantity"] += own
            totals["unfilled_quantity"] += unfilled
        require(
            all(
                row["entered"] == row["filled"] + row["remaining"]
                and row["remaining"] >= 0
                for row in book.values()
            ),
            "resting-order quantity was not conserved",
        )
        trace.append(
            {
                "event_id": event["event_id"],
                "kind": event["kind"],
                "remaining_by_id": {
                    identifier: row["remaining"] for identifier, row in book.items()
                },
                "fills": fills,
                "unfilled": unfilled,
                "external_quantity": external,
                "own_quantity": own,
            }
        )
    require(
        world != "baseline" or totals["own_quantity"] == 0,
        "baseline contains own executions",
    )
    return {
        "trace": trace,
        "final_remaining_by_id": {key: row["remaining"] for key, row in book.items()},
        "totals": totals,
    }


def analyze_case(case: dict[str, Any]) -> dict[str, Any]:
    """Compute the paired worlds first; only then compare the manual oracle."""
    before = copy.deepcopy(case)
    worlds = {world: replay_world(case, world) for world in WORLDS}
    expected = case["expected"]
    require(set(expected) == set(WORLDS), "manual expected worlds differ")
    incoming = [event for event in case["events"] if event["kind"] == "incoming"]
    traces = {
        world: [row for row in worlds[world]["trace"] if row["kind"] == "incoming"]
        for world in WORLDS
    }
    paired = []
    for event, baseline, counterfactual in zip(
        incoming, traces["baseline"], traces["counterfactual"], strict=True
    ):
        require(
            event["event_id"] == baseline["event_id"] == counterfactual["event_id"],
            "paired incoming identity differs",
        )
        displacement = (
            baseline["external_quantity"] - counterfactual["external_quantity"]
        )
        reduction = baseline["unfilled"] - counterfactual["unfilled"]
        hybrid_error = (
            baseline["external_quantity"]
            + counterfactual["own_quantity"]
            + counterfactual["unfilled"]
            - event["quantity"]
        )
        paired.append(
            {
                "incoming_id": event["event_id"],
                "requested_quantity": event["quantity"],
                "baseline_external": baseline["external_quantity"],
                "counterfactual_external": counterfactual["external_quantity"],
                "counterfactual_own": counterfactual["own_quantity"],
                "baseline_unfilled": baseline["unfilled"],
                "counterfactual_unfilled": counterfactual["unfilled"],
                "external_displacement": displacement,
                "unfilled_reduction": reduction,
                "paired_identity": counterfactual["own_quantity"]
                == displacement + reduction,
                "hybrid_budget_error": hybrid_error,
                "hybrid_equals_displacement": hybrid_error == displacement,
            }
        )
    criteria = {
        "baseline_matches_manual": worlds["baseline"] == expected["baseline"],
        "counterfactual_matches_manual": worlds["counterfactual"]
        == expected["counterfactual"],
        "input_unchanged": case == before,
        "paired_incoming_identity": all(row["paired_identity"] for row in paired),
        "hybrid_error_identity": all(
            row["hybrid_equals_displacement"] for row in paired
        ),
        "hybrid_matches_manual": [row["hybrid_budget_error"] for row in paired]
        == case["expected_hybrid_errors"],
    }
    return {
        "case_id": case["case_id"],
        "recipe_id": case["recipe_id"],
        "mirrored": case["mirrored"],
        "prior_exposed_control": case["prior_exposed_control"],
        "worlds": worlds,
        "paired_incoming": paired,
        "criteria": criteria,
        "all_criteria_passed": all(criteria.values()),
        "expected_sha256": object_hash(expected),
        "actual_worlds_sha256": object_hash(worlds),
    }


def structural_witnesses(
    cases: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    inputs = {row["case_id"]: row for row in cases}
    actual = {row["case_id"]: row for row in results}
    witnesses = {}
    for side in ("sell", "buy"):

        def result(recipe: str, direction: str = side) -> dict[str, Any]:
            return actual[f"{recipe}_{direction}"]

        def own(recipe: str) -> int:
            return result(recipe)["worlds"]["counterfactual"]["totals"]["own_quantity"]

        def prints(recipe: str) -> list[Any]:
            return [
                row["fills"]
                for row in result(recipe)["worlds"]["baseline"]["trace"]
                if row["kind"] == "incoming"
            ]

        def front(recipe: str, direction: str = side) -> int:
            case = inputs[f"{recipe}_{direction}"]
            first = next(row for row in case["events"] if row["kind"] == "incoming")
            key = first["timestamp_ns"], first["sequence"]
            displayed = [
                row
                for row in case["events"]
                if row["kind"] == "activate"
                and row["owner"] == "external"
                and (row["timestamp_ns"], row["sequence"]) < key
            ]
            best = (max if direction == "sell" else min)(
                row["price_ticks"] for row in displayed
            )
            return sum(
                row["quantity"] for row in displayed if row["price_ticks"] == best
            )

        carry = result("displaced_carry")
        errors = [row["hybrid_budget_error"] for row in carry["paired_incoming"]]
        late = result("late_activation")["worlds"]["counterfactual"]
        second = [
            row
            for row in carry["worlds"]["counterfactual"]["trace"]
            if row["kind"] == "incoming"
        ][1]
        detail = {
            "shallow_identical_baseline_prints": prints("shallow6")
            == prints("shallow11"),
            "shallow_own_quantities": [own("shallow6"), own("shallow11")],
            "same_aggregate_front_quantities": [front("known8"), front("split_time")],
            "time_placement_own_quantities": [own("known8"), own("split_time")],
            "same_time_sequence_own_quantities": [own("tie_before"), own("tie_after")],
            "displaced_carry_hybrid_errors": errors,
            "displaced_carry_terminal_hybrid_error": sum(errors),
            "later_counterfactual_external_fills": [
                (row["order_id"], row["quantity"])
                for row in second["fills"]
                if row["owner"] == "external"
            ],
            "late_activation_final_own_remaining": late["final_remaining_by_id"]["O1"],
            "late_activation_own_filled": late["totals"]["own_quantity"],
            "late_activation_terminated_unfilled": late["totals"]["unfilled_quantity"],
        }
        detail["all_passed"] = (
            detail["shallow_identical_baseline_prints"]
            and detail["shallow_own_quantities"] == [0, 5]
            and detail["same_aggregate_front_quantities"] == [6, 6]
            and detail["time_placement_own_quantities"] == [2, 4]
            and detail["same_time_sequence_own_quantities"] == [2, 0]
            and errors == [5, -5]
            and detail["displaced_carry_terminal_hybrid_error"] == 0
            and detail["later_counterfactual_external_fills"] == [("E2", 2), ("E3", 3)]
            and detail["late_activation_final_own_remaining"] == 5
            and detail["late_activation_own_filled"] == 0
            and detail["late_activation_terminated_unfilled"] == 5
        )
        witnesses[side] = detail
    return witnesses


def source_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted((root / "src/lobmm").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def make_plan(input_root: Path) -> dict[str, Any]:
    require(
        digest(input_root / SOURCE_PLAN) == SOURCE_PLAN_SHA,
        "original source inventory changed",
    )
    origin = read(input_root / SOURCE_PLAN)
    require(
        origin["source_revision"] == SOURCE_REVISION
        and len(origin["source_sha256"]) == 38
        and source_inventory(input_root) == origin["source_sha256"],
        "production source differs from frozen C1",
    )
    require(
        digest(input_root / CASES) == FIXTURE_SHA,
        "fixed synthetic family bytes changed",
    )
    family = read(input_root / CASES)
    case_ids = [case["case_id"] for case in family["cases"]]
    require(
        len(case_ids) == len(set(case_ids)) == family["scenario_count"] == 36,
        "fixed case inventory differs",
    )
    return {
        "version": 1,
        "base_commit": BASE_COMMIT,
        "source_revision": SOURCE_REVISION,
        "source_sha256": origin["source_sha256"],
        "source_inventory_origin": {SOURCE_PLAN: SOURCE_PLAN_SHA},
        "pins": {name: digest(input_root / name) for name in PINS},
        "case_ids": case_ids,
        "limits": LIMITS,
        "scope": "Fully displayed synthetic fixed incoming streams; no production matcher, venue calibration or economics",
    }


def validate_plan(
    plan: dict[str, Any], input_root: Path, program_root: Path = ROOT
) -> None:
    require(
        plan["version"] == 1
        and plan["base_commit"] == BASE_COMMIT
        and plan["source_revision"] == SOURCE_REVISION,
        "frozen baseline identity differs",
    )
    require(
        plan["limits"] == LIMITS
        and set(plan["pins"]) == set(PINS)
        and plan["pins"][CASES] == FIXTURE_SHA,
        "frozen limits/pin inventory differs",
    )
    require(
        plan["source_inventory_origin"] == {SOURCE_PLAN: SOURCE_PLAN_SHA}
        and digest(input_root / SOURCE_PLAN) == SOURCE_PLAN_SHA,
        "source provenance chain differs",
    )
    original = read(input_root / SOURCE_PLAN)
    require(
        original["source_revision"] == SOURCE_REVISION
        and original["source_sha256"] == plan["source_sha256"]
        and len(plan["source_sha256"]) == 38
        and source_inventory(input_root) == plan["source_sha256"],
        "source bytes changed",
    )
    for name, expected in plan["pins"].items():
        require(
            digest(input_root / name) == expected
            and digest(program_root / name) == expected,
            "frozen program/input bytes changed: " + name,
        )
    family = read(program_root / CASES)
    require(
        [case["case_id"] for case in family["cases"]] == plan["case_ids"]
        and family["scenario_count"] == 36
        and family["world_path_count"] == 72
        and family["incoming_walk_count"] == 80,
        "frozen family counts/order differ",
    )


def evidence_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def file_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path != root / "manifest.json"
        and "__pycache__" not in path.parts
    }


def no_application_imports() -> bool:
    return not any(name == "lobmm" or name.startswith("lobmm.") for name in sys.modules)


def worker(plan_path: Path, input_root: Path, output: Path) -> int:
    require(not output.exists(), "refusing an existing worker output")
    output.mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "started",
        "stage": "metadata admission",
        "plan_sha256": digest(plan_path),
        "completed_case_ids": [],
        "failed_case_ids": [],
        "processed_incoming_rows": 0,
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "no_application_imports_before": no_application_imports(),
    }
    write(output / "result.json", result)
    current_case = None
    all_case_ids: list[str] = []
    try:
        plan = read(plan_path)
        validate_plan(plan, input_root)
        require(
            result["no_application_imports_before"], "unexpected application import"
        )
        family = read(ROOT / CASES)
        cases = family["cases"]
        all_case_ids = plan["case_ids"]
        require(len(cases) <= LIMITS["cases"], "family case cap")
        results = []
        paired_rows = []
        result["stage"] = "fixed family replay"
        with (output / "paired.jsonl").open("x") as paired_stream:
            for index, case in enumerate(cases):
                current_case = case["case_id"]
                result["current_case_id"] = current_case
                write(output / "result.json", result)
                row = analyze_case(case)
                target = output / "cases" / f"{index:03d}-{current_case}.json"
                write(target, row)
                for paired in row["paired_incoming"]:
                    record = {"case_id": current_case, **paired}
                    paired_stream.write(canonical(record).decode() + "\n")
                    paired_stream.flush()
                    os.fsync(paired_stream.fileno())
                    paired_rows.append(record)
                results.append(row)
                result["completed_case_ids"].append(current_case)
                result["processed_incoming_rows"] += len(row["paired_incoming"])
                if not row["all_criteria_passed"]:
                    result["failed_case_ids"].append(current_case)
                write(output / "result.json", result)
                require(
                    evidence_size(output.parent)
                    <= LIMITS["evidence_bytes"] - STOP_RESERVE,
                    "generated evidence cap",
                )
                require(
                    row["all_criteria_passed"],
                    "manual-oracle comparison failed; fixed pass stops",
                )
        witnesses = structural_witnesses(cases, results)
        require(
            all(row["all_passed"] for row in witnesses.values()),
            "structural witness failed",
        )
        require(
            len(results) == 36 and len(paired_rows) == 40,
            "fixed family coverage differs",
        )
        summary = {
            "scenario_count": len(results),
            "world_path_count": len(results) * 2,
            "incoming_walk_count": len(paired_rows) * 2,
            "paired_incoming_count": len(paired_rows),
            "prior_exposed_control_scenarios": sum(
                row["prior_exposed_control"] for row in results
            ),
            "all_criteria_passed": all(row["all_criteria_passed"] for row in results),
            "hybrid_error_counts": {
                "positive": sum(row["hybrid_budget_error"] > 0 for row in paired_rows),
                "zero": sum(row["hybrid_budget_error"] == 0 for row in paired_rows),
                "negative": sum(row["hybrid_budget_error"] < 0 for row in paired_rows),
            },
            "structural_witnesses": witnesses,
            "case_result_sha256": {row["case_id"]: object_hash(row) for row in results},
        }
        write(output / "summary.json", summary)
        validate_plan(plan, input_root)
        result.update(
            status="complete",
            stage="complete",
            current_case_id=None,
            summary=summary,
            untested_suffix=[],
        )
        returncode = 0
    except BaseException as exc:
        result.update(
            status="stopped",
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
            current_case_id=current_case,
            untested_suffix=[
                identifier
                for identifier in all_case_ids
                if identifier not in result["completed_case_ids"]
            ],
        )
        returncode = 2
    finally:
        result["no_application_imports_after"] = no_application_imports()
        if not result["no_application_imports_after"]:
            result.update(status="stopped", stop_reason="application module imported")
            returncode = 2
        write(output / "result.json", result)
    return returncode


def run(plan_path: Path, input_root: Path, output: Path) -> dict[str, Any]:
    started = time.monotonic_ns()
    require(not output.exists(), "refusing to overwrite prior evidence")
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "status": "started",
        "started_ns": started,
        "plan_sha256": None,
        "attempts": [],
    }
    write(output / "manifest.json", manifest)
    try:
        plan = read(plan_path)
        validate_plan(plan, input_root)
        shutil.copy2(plan_path, output / "plan.json")
        manifest["plan_sha256"] = digest(output / "plan.json")
        for name in PINS:
            target = output / "harness" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        remaining = LIMITS["controller_seconds"] - (time.monotonic_ns() - started) / 1e9
        require(remaining > 0, "controller cap exhausted before worker")
        command = [
            sys.executable,
            "-I",
            "-B",
            str(output / "harness" / TOOL),
            "--worker",
            "--plan",
            str(output / "plan.json"),
            "--input-root",
            str(input_root),
            "--output",
            str(output / "worker"),
        ]
        attempt = {
            "command": command,
            "started_ns": time.monotonic_ns(),
            "cap_seconds": min(LIMITS["worker_seconds"], remaining),
        }
        manifest["attempts"].append(attempt)
        write(output / "manifest.json", manifest)
        try:
            with (output / "worker.log").open("x") as log:
                completed = subprocess.run(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=attempt["cap_seconds"],
                    check=False,
                )
            attempt.update(
                status="complete" if completed.returncode == 0 else "stopped",
                returncode=completed.returncode,
            )
        except subprocess.TimeoutExpired:
            attempt.update(
                status="timeout",
                returncode=None,
                stop_reason="isolated worker wall-time cap",
                untested_suffix="Interrupted case and later cases are untested; only durable completed case files are evidence",
            )
            raise
        finally:
            attempt["finished_ns"] = time.monotonic_ns()
            write(output / "manifest.json", manifest)
        require(
            attempt["finished_ns"] - attempt["started_ns"]
            <= attempt["cap_seconds"] * 1e9,
            "worker startup/teardown cap",
        )
        require(completed.returncode == 0, "fixed family stopped; no retries or repair")
        record = read(output / "worker/result.json")
        require(
            record["status"] == "complete" and record["no_application_imports_after"],
            "worker completion proof differs",
        )
        report = {
            "complete": True,
            "summary": record["summary"],
            "scope": "Exact constructed fully displayed mechanics only; fixed future incoming flow is assumed, no calibrated fills or economic advantage",
        }
        write(output / "report.json", report)
        manifest.update(status="complete", files=file_inventory(output))
        return report
    except BaseException as exc:
        manifest.update(
            status="stopped",
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
            files=file_inventory(output),
            untested_suffix="Only durable completed-case traces were processed; interrupted/later cases remain untested",
        )
        raise
    finally:
        manifest["finished_ns"] = time.monotonic_ns()
        manifest["elapsed_ns"] = manifest["finished_ns"] - started
        over_time = manifest["elapsed_ns"] > LIMITS["controller_seconds"] * 10**9
        if over_time:
            manifest.update(status="stopped", stop_reason="controller wall-time cap")
        write(output / "manifest.json", manifest)
        over_size = evidence_size(output) > LIMITS["evidence_bytes"]
        if over_size:
            manifest.update(status="stopped", stop_reason="final evidence size cap")
            write(output / "manifest.json", manifest)
        if (over_time or over_size) and sys.exc_info()[0] is None:
            raise ValueError(manifest["stop_reason"])


def verify_evidence(output: Path, input_root: Path) -> dict[str, Any]:
    manifest, plan = read(output / "manifest.json"), read(output / "plan.json")
    require(
        manifest["status"] == "complete"
        and manifest["files"] == file_inventory(output)
        and manifest["plan_sha256"] == digest(output / "plan.json"),
        "saved evidence incomplete or changed",
    )
    validate_plan(plan, input_root)
    validate_plan(plan, input_root, output / "harness")
    require(
        manifest["elapsed_ns"] == manifest["finished_ns"] - manifest["started_ns"]
        and 0 < manifest["elapsed_ns"] <= LIMITS["controller_seconds"] * 10**9
        and len(manifest["attempts"]) == 1,
        "controller count/cap differs",
    )
    attempt = manifest["attempts"][0]
    require(
        attempt["status"] == "complete"
        and attempt["returncode"] == 0
        and 0
        < attempt["finished_ns"] - attempt["started_ns"]
        <= LIMITS["worker_seconds"] * 10**9,
        "single worker status/cap differs",
    )
    result = read(output / "worker/result.json")
    require(
        result["status"] == "complete"
        and result["plan_sha256"] == manifest["plan_sha256"]
        and result["completed_case_ids"] == plan["case_ids"]
        and not result["failed_case_ids"]
        and not result["untested_suffix"]
        and result["no_application_imports_before"]
        and result["no_application_imports_after"],
        "worker prefix/isolation differs",
    )
    rows = [read(path) for path in sorted((output / "worker/cases").glob("*.json"))]
    require(
        [row["case_id"] for row in rows] == plan["case_ids"]
        and all(
            row["all_criteria_passed"] and all(row["criteria"].values()) for row in rows
        ),
        "case records/criteria differ",
    )
    paired = [
        json.loads(line)
        for line in (output / "worker/paired.jsonl").read_text().splitlines()
    ]
    expected_paired = [
        {"case_id": row["case_id"], **item}
        for row in rows
        for item in row["paired_incoming"]
    ]
    require(
        paired == expected_paired
        and len(paired) == result["processed_incoming_rows"] == 40,
        "paired counter/record integrity differs",
    )
    summary = read(output / "worker/summary.json")
    require(
        summary == result["summary"] == read(output / "report.json")["summary"]
        and summary["scenario_count"] == len(rows) == 36
        and summary["world_path_count"] == 72
        and summary["incoming_walk_count"] == 80
        and summary["all_criteria_passed"]
        and summary["case_result_sha256"]
        == {row["case_id"]: object_hash(row) for row in rows}
        and evidence_size(output) <= LIMITS["evidence_bytes"],
        "summary/count/hash differs",
    )
    return {
        "verified": True,
        "integrity_only_no_matching_rerun": True,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-plan", type=Path)
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--verify-evidence", type=Path)
    modes.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        if args.write_plan is not None:
            require(not args.write_plan.exists(), "refusing an existing plan")
            write(args.write_plan, make_plan(args.input_root.resolve()))
            print(json.dumps({"plan_sha256": digest(args.write_plan)}))
            return 0
        if args.worker:
            require(
                args.plan is not None and args.output is not None,
                "worker arguments missing",
            )
            return worker(
                args.plan.resolve(), args.input_root.resolve(), args.output.resolve()
            )
        if args.verify_evidence is not None:
            result = verify_evidence(
                args.verify_evidence.resolve(), args.input_root.resolve()
            )
        else:
            require(
                args.plan is not None and args.output is not None,
                "--run needs plan and fresh output",
            )
            result = run(
                args.plan.resolve(), args.input_root.resolve(), args.output.resolve()
            )
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
