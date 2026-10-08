from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from lobmm.backtest import run_backtest
from lobmm.cli import app
from lobmm.config import load_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import load_events
from lobmm.enums import EventType, QueueAllocation, StrategyName
from lobmm.experiments import ExperimentSpec, run_experiment
from lobmm.queue_study import QueueStudyError, audit_queue_study, write_queue_study

pytestmark = pytest.mark.integration
CONFIG = Path("configs/queue_cancellation.yaml")
FIXTURE = Path("data/fixtures/queue_cancellation.csv")
STREAM_HASH = "abe7764fa0162b6f444c8223d752dbf4847bafb62536b8950f4f4211b4b36224"


@pytest.fixture(scope="module")
def grid_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("queue-grid")
    # Exercise the documented command, canonical loader, validation, experiment
    # case overrides, scheduler, delayed strategy, matching, and persistence.
    result = CliRunner().invoke(
        app,
        [
            "experiment",
            "--config",
            str(CONFIG),
            "--name",
            "controlled",
            "--latency-multipliers",
            "0.5,1,2",
            "--output-root",
            str(root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["case_count"] == 9
    return root / "controlled"


def test_full_loop_discriminates_policies_and_reconciles_accounting(
    grid_directory: Path,
) -> None:
    audit = audit_queue_study(grid_directory, CONFIG)
    assert event_stream_sha256(load_events(FIXTURE)) == STREAM_HASH
    assert audit["event_stream_sha256"] == STREAM_HASH
    assert audit["case_count"] == 9
    expected = {
        "back_of_queue": (0, Decimal("0")),
        "pro_rata": (5, Decimal("0.084")),
        "front_of_queue": (10, Decimal("0.168")),
    }
    for case in audit["cases"]:
        quantity, net = expected[case["queue_allocation"]]
        assert case["filled_per_side"] == quantity
        assert Decimal(case["net_pnl"]) == net
        assert case["end_inventory"] == 0
        assert case["event_stream_sha256"] == STREAM_HASH
        inventory = pl.read_parquet(
            grid_directory / "runs" / case["run_name"] / "inventory.parquet"
        )
        # Bid and ask trades share a timestamp but are separate causal events:
        # cash accounting must survive the intermediate long position.
        assert inventory["inventory"].max() == quantity
        assert inventory["inventory"].min() == 0


def test_repeated_grid_has_identical_research_artifacts(
    grid_directory: Path, tmp_path: Path
) -> None:
    config = load_config(CONFIG)
    repeat = run_experiment(
        config,
        load_events(FIXTURE),
        name="repeat",
        spec=ExperimentSpec(strategies=(StrategyName.FIXED_SPREAD,)),
        output_root=tmp_path,
    )
    second = repeat.experiment_directory
    audit = audit_queue_study(grid_directory, CONFIG)
    assert audit_queue_study(second, CONFIG) == audit
    for case in audit["cases"]:
        first_run = grid_directory / "runs" / case["run_name"]
        second_run = second / "runs" / case["run_name"]
        tables = sorted(first_run.glob("*.parquet"))
        assert len(tables) == 8
        for table in tables:
            assert table.read_bytes() == (second_run / table.name).read_bytes()
        assert (first_run / "summary.json").read_bytes() == (
            second_run / "summary.json"
        ).read_bytes()
        first_metrics = json.loads((first_run / "metrics.json").read_text())
        second_metrics = json.loads((second_run / "metrics.json").read_text())
        for metrics in (first_metrics, second_metrics):
            for measured in (
                "events_per_second",
                "wall_clock_seconds",
                "peak_memory_bytes",
            ):
                metrics["engineering"].pop(measured)
        assert first_metrics == second_metrics
    for directory in (grid_directory, second):
        write_queue_study(audit, directory / "QUEUE_AUDIT.md")
    for filename in ("QUEUE_AUDIT.md", "QUEUE_AUDIT.json"):
        assert (grid_directory / filename).read_bytes() == (
            second / filename
        ).read_bytes()


@pytest.mark.parametrize("allocation", list(QueueAllocation))
@pytest.mark.parametrize("control", ["no_cancellation", "arrival_after_add"])
def test_negative_controls_do_not_reach_own_quotes(
    allocation: QueueAllocation, control: str, tmp_path: Path
) -> None:
    config = load_config(CONFIG)
    config = config.model_copy(
        update={
            "queue_model": config.queue_model.model_copy(
                update={"cancellation_allocation": allocation}
            )
        }
    )
    events = load_events(FIXTURE)
    if control == "no_cancellation":
        events = [event for event in events if event.event_type is not EventType.CANCEL]
    else:
        # Join after the 50-unit add but before the cancellation. All 150
        # external units start ahead; 60 cancels leave 90 ahead for every policy.
        config = config.model_copy(
            update={
                "latency": config.latency.model_copy(
                    update={"order_entry_ns": 1_200_000}
                )
            }
        )
    result = run_backtest(config, events, run_name=control, output_root=tmp_path)
    assert pl.read_parquet(result.run_directory / "fills.parquet").is_empty()
    assert result.metrics["pnl"]["net_pnl"] == 0
    assert result.diagnostics["submitted_order_messages"] == 2
    assert result.diagnostics["input_validation_issue_count"] == 0
    orders = pl.read_parquet(result.run_directory / "orders.parquet")
    assert orders.filter(pl.col("new_status") == "live").height == 2


@pytest.mark.parametrize(
    ("artifact", "column", "value", "message"),
    [
        ("fills.parquet", "quantity", 4, "fill quantity"),
        ("fills.parquet", "side", 1, "both sides"),
        ("fills.parquet", "fee", 0.0, "fill costs"),
        ("fills.parquet", "strategy_notification_timestamp_ns", 3_000_000, "causal"),
        ("orders.parquet", "exchange_arrival_timestamp_ns", 1_000_001, "join before"),
        ("orders.parquet", "cumulative_filled_quantity", 4, "conservation"),
        ("pnl.parquet", "inventory", 1, "P&L identity"),
    ],
)
def test_audit_rejects_corrupted_execution_or_accounting_artifacts(
    grid_directory: Path,
    tmp_path: Path,
    artifact: str,
    column: str,
    value: int | float,
    message: str,
) -> None:
    damaged = tmp_path / "damaged"
    shutil.copytree(grid_directory, damaged)
    path = damaged / "runs" / "fixed_spread__full__lat1__pro_rata__fee1" / artifact
    frame = pl.read_parquet(path)
    frame.with_columns(pl.lit(value).alias(column)).write_parquet(path)
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize(
    ("artifact", "key", "value", "message"),
    [
        ("experiment.json", "event_stream_sha256", "a" * 64, "manifest"),
        ("summary.json", "event_stream_sha256", "a" * 64, "input hashes"),
        ("diagnostics.json", "risk_blocked_quote_attempts", 1, "quote activity"),
    ],
)
def test_audit_rejects_mismatched_manifest_or_run_identity(
    grid_directory: Path,
    tmp_path: Path,
    artifact: str,
    key: str,
    value: int | str,
    message: str,
) -> None:
    damaged = tmp_path / "damaged"
    shutil.copytree(grid_directory, damaged)
    root = (
        damaged if artifact == "experiment.json" else next((damaged / "runs").iterdir())
    )
    path = root / artifact
    document = json.loads(path.read_text())
    document[key] = value
    path.write_text(json.dumps(document))
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


def test_audit_rejects_incomplete_grid_and_modified_run_config(
    grid_directory: Path, tmp_path: Path
) -> None:
    damaged = tmp_path / "damaged"
    shutil.copytree(grid_directory, damaged)
    table_path = damaged / "sensitivity.parquet"
    original = table_path.read_bytes()
    pl.read_parquet(table_path).head(8).write_parquet(table_path)
    with pytest.raises(QueueStudyError, match="sensitivity table"):
        audit_queue_study(damaged, CONFIG)
    table_path.write_bytes(original)
    path = next((damaged / "runs").iterdir()) / "run_config.yaml"
    path.write_text(
        path.read_text().replace(
            "maker_fee_per_unit: '0.002'", "maker_fee_per_unit: '0'"
        )
    )
    with pytest.raises(QueueStudyError, match="config"):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize("artifact", ["market.parquet", "sensitivity.parquet"])
def test_audit_checks_saved_tape_and_sensitivity_values(
    grid_directory: Path, tmp_path: Path, artifact: str
) -> None:
    damaged = tmp_path / "damaged"
    shutil.copytree(grid_directory, damaged)
    if artifact == "market.parquet":
        path = next((damaged / "runs").iterdir()) / artifact
        frame = pl.read_parquet(path)
        frame.with_columns((pl.col("quantity") + 1).alias("quantity")).write_parquet(
            path
        )
        message = "input hashes"
    else:
        path = damaged / artifact
        frame = pl.read_parquet(path)
        frame.with_columns(pl.lit(999).alias("fill_events")).write_parquet(path)
        message = "sensitivity metrics"
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


def test_audit_command_publishes_only_after_successful_verification(
    grid_directory: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    arguments = [
        "audit-queue-study",
        "--experiment",
        str(grid_directory),
        "--config",
        str(CONFIG),
    ]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert "Verified 9 cases" in result.output
    output = tmp_path / "QUEUE_CANCELLATION.md"
    result = runner.invoke(app, [*arguments, "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.is_file()
    assert json.loads(output.with_suffix(".json").read_text())["case_count"] == 9
    changed = tmp_path / "changed.yaml"
    changed.write_text(CONFIG.read_text() + "\n# Changed config\n")
    blocked_output = tmp_path / "blocked.md"
    result = runner.invoke(
        app,
        [
            "audit-queue-study",
            "--experiment",
            str(grid_directory),
            "--config",
            str(changed),
            "--output",
            str(blocked_output),
        ],
    )
    assert result.exit_code == 1
    assert "controlled config hash" in result.output
    assert not blocked_output.exists()
    assert not blocked_output.with_suffix(".json").exists()


def test_audit_rejects_a_modified_canonical_fixture(
    grid_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    CONFIG.parent.mkdir(parents=True)
    FIXTURE.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    shutil.copyfile(source / CONFIG, CONFIG)
    FIXTURE.write_text((source / FIXTURE).read_text().replace(",65\n", ",64\n"))
    with pytest.raises(QueueStudyError, match="fixture hash"):
        audit_queue_study(grid_directory, CONFIG)


def _copy_artifact_for_corruption(
    grid_directory: Path, tmp_path: Path, artifact: str, allocation: str = "pro_rata"
) -> tuple[Path, Path, pl.DataFrame]:
    damaged = tmp_path / "damaged"
    shutil.copytree(grid_directory, damaged)
    path = damaged / "runs" / f"fixed_spread__full__lat1__{allocation}__fee1" / artifact
    return damaged, path, pl.read_parquet(path)


def _replace_cell(
    frame: pl.DataFrame, index: int, column: str, value: int | float | str | None
) -> pl.DataFrame:
    values = frame[column].to_list()
    values[index] = value
    return frame.with_columns(pl.Series(column, values, dtype=frame.schema[column]))


@pytest.mark.parametrize(
    ("artifact", "column", "value", "index", "message"),
    [
        ("fills.parquet", "order_id", "UNKNOWN", 0, "unknown order"),
        ("fills.parquet", "fill_id", "", 0, "empty fill identifiers"),
        ("fills.parquet", "fill_id", None, 0, "empty fill identifiers"),
        ("fills.parquet", "client_order_id", "UNKNOWN", 0, "identity join mismatch"),
        ("fills.parquet", "strategy_id", "other-strategy", 1, "identity join mismatch"),
        ("orders.parquet", "client_order_id", "CHANGED", 1, "immutable metadata"),
        ("orders.parquet", "strategy_id", "other-strategy", 0, "immutable metadata"),
        ("orders.parquet", "price_ticks", 100, 0, "immutable metadata"),
        ("orders.parquet", "original_quantity", 11, 0, "immutable metadata"),
        ("orders.parquet", "creation_timestamp_ns", 0, 0, "immutable metadata"),
        ("orders.parquet", "cumulative_filled_quantity", 4, 0, "per-order fill sums"),
        ("orders.parquet", "average_fill_price_ticks", 100.0, 0, "per-order fill sums"),
        ("orders.parquet", "timestamp_ns", 250_001, 1, "transition lifecycle"),
        ("orders.parquet", "previous_status", "live", 1, "transition lifecycle"),
        ("orders.parquet", "remaining_quantity", 9, 0, "quantity progression"),
        ("orders.parquet", "reason", "cancel_arrived", 0, "transition lifecycle"),
    ],
)
def test_audit_joins_fill_identity_and_checks_historical_order_rows(
    grid_directory: Path,
    tmp_path: Path,
    artifact: str,
    column: str,
    value: int | float | str | None,
    index: int,
    message: str,
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, artifact
    )
    _replace_cell(frame, index, column, value).write_parquet(path)
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_fill_id",
        "duplicate_transition_id",
        "empty_transition_id",
        "same_order_for_both_fills",
        "swapped_fill_orders",
        "duplicate_client_identity",
        "duplicate_order_identity",
        "reversed_fill_order",
        "missing_partial_transition",
        "reordered_creation_and_acceptance",
    ],
)
def test_audit_rejects_broken_identifier_relationships_and_lifecycle(
    grid_directory: Path, tmp_path: Path, corruption: str
) -> None:
    artifact = "fills.parquet" if "fill" in corruption else "orders.parquet"
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, artifact
    )
    if corruption == "duplicate_fill_id":
        changed = _replace_cell(frame, 1, "fill_id", frame["fill_id"][0])
        message = "duplicate or empty fill identifiers"
    elif corruption == "duplicate_transition_id":
        changed = _replace_cell(frame, 1, "transition_id", frame["transition_id"][0])
        message = "duplicate or empty transition identifiers"
    elif corruption == "empty_transition_id":
        changed = _replace_cell(frame, 0, "transition_id", "")
        message = "duplicate or empty transition identifiers"
    elif corruption == "same_order_for_both_fills":
        changed = _replace_cell(frame, 1, "order_id", frame["order_id"][0])
        message = "per-order fill sums"
    elif corruption == "swapped_fill_orders":
        changed = frame.with_columns(frame["order_id"].reverse())
        message = "identity join mismatch"
    elif corruption in {"duplicate_client_identity", "duplicate_order_identity"}:
        column = "client_order_id" if "client" in corruption else "order_id"
        changed = frame.with_columns(pl.lit(frame[column][0]).alias(column))
        message = "exactly two distinct own orders and client identifiers"
    elif corruption == "reversed_fill_order":
        changed = frame.reverse()
        message = "canonical bid then ask"
    elif corruption == "missing_partial_transition":
        changed = frame.filter(pl.col("new_status") != "partially_filled")
        message = "transition lifecycle"
    else:
        changed = pl.concat([frame.slice(1, 1), frame.head(1), frame.slice(2)])
        message = "transition lifecycle"
    changed.write_parquet(path)
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("timestamp_ns", 3_000_001),
        ("currency", "EUR"),
        ("tick_value", 0.02),
        ("mark_ticks", 101.0),
        ("inventory", 6),
        ("average_cost_ticks", 100.0),
        ("trade_cash_ticks", -494),
        ("trade_cash", -4.94),
        ("cash", -4.95),
        ("realized_pnl_ticks", 1.0),
        ("unrealized_pnl_ticks", 6.0),
        ("gross_pnl_ticks", 6.0),
        ("realized_pnl", 0.01),
        ("unrealized_pnl", 0.06),
        ("gross_pnl", 0.06),
        ("fees", 0.0),
        ("rebates", 0.0),
        ("net_pnl", 0.0),
        ("turnover_ticks", 496),
        ("turnover", 4.96),
        ("buy_volume", 6),
        ("sell_volume", 1),
        ("fill_count", 2),
        ("current_gross_exposure", 6.0),
        ("peak_gross_exposure", 6.0),
    ],
)
def test_audit_rejects_intermediate_accounting_corruption_with_valid_final_state(
    grid_directory: Path, tmp_path: Path, column: str, value: int | float | str
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, "pnl.parquet"
    )
    # Mutate only the first long snapshot. The final flat ledger remains valid.
    index = frame["fill_count"].to_list().index(1)
    changed = _replace_cell(frame, index, column, value)
    assert changed.tail(1).equals(frame.tail(1))
    changed.write_parquet(path)
    with pytest.raises(QueueStudyError, match="intermediate accounting P&L identity"):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize("corruption", ["deleted", "duplicated", "reordered"])
def test_audit_requires_the_complete_causal_accounting_path(
    grid_directory: Path, tmp_path: Path, corruption: str
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, "pnl.parquet"
    )
    index = frame["fill_count"].to_list().index(1)
    if corruption == "deleted":
        changed = frame.filter(pl.col("fill_count") != 1)
        message = "intermediate accounting snapshot count"
    elif corruption == "duplicated":
        changed = pl.concat(
            [frame.head(index), frame.slice(index, 1), frame.slice(index)]
        )
        message = "intermediate accounting snapshot count"
    else:
        # Both fills share 3 ms: swapping long and flat states preserves timestamps.
        changed = pl.concat(
            [
                frame.head(index),
                frame.slice(index + 2, 2),
                frame.slice(index, 2),
                frame.slice(index + 4),
            ]
        )
        message = "intermediate accounting P&L identity"
    assert changed.tail(1).equals(frame.tail(1))
    changed.write_parquet(path)
    with pytest.raises(QueueStudyError, match=message):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("timestamp_ns", 3_000_001),
        ("inventory", 6),
        ("current_gross_exposure", 6.0),
        ("peak_gross_exposure", 6.0),
    ],
)
def test_audit_reconciles_inventory_projection_with_intermediate_accounting(
    grid_directory: Path, tmp_path: Path, column: str, value: int | float
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, "inventory.parquet"
    )
    index = frame["inventory"].to_list().index(5)
    _replace_cell(frame, index, column, value).write_parquet(path)
    with pytest.raises(QueueStudyError, match="inventory projection"):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize("allocation", ["back_of_queue", "front_of_queue"])
def test_audit_checks_accounting_path_for_unfilled_and_fully_filled_quotes(
    grid_directory: Path, tmp_path: Path, allocation: str
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, "pnl.parquet", allocation
    )
    # A fee booked before either trade must fail even when final balances agree.
    _replace_cell(frame, 0, "fees", 0.01).write_parquet(path)
    with pytest.raises(QueueStudyError, match="intermediate accounting P&L identity"):
        audit_queue_study(damaged, CONFIG)


@pytest.mark.parametrize(
    ("artifact", "column", "value"),
    [
        ("pnl.parquet", "cash", float("nan")),
        ("pnl.parquet", "net_pnl", float("inf")),
        ("fills.parquet", "fee", float("nan")),
        ("fills.parquet", "rebate", float("inf")),
    ],
)
def test_audit_rejects_nonfinite_accounting_values(
    grid_directory: Path, tmp_path: Path, artifact: str, column: str, value: float
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, artifact
    )
    _replace_cell(frame, 0, column, value).write_parquet(path)
    with pytest.raises(QueueStudyError, match="non-finite accounting value"):
        audit_queue_study(damaged, CONFIG)


def test_audit_command_blocks_report_for_orphan_execution(
    grid_directory: Path, tmp_path: Path
) -> None:
    damaged, path, frame = _copy_artifact_for_corruption(
        grid_directory, tmp_path, "fills.parquet"
    )
    _replace_cell(frame, 0, "order_id", "UNKNOWN").write_parquet(path)
    output = tmp_path / "blocked.md"
    result = CliRunner().invoke(
        app,
        [
            "audit-queue-study",
            "--experiment",
            str(damaged),
            "--config",
            str(CONFIG),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 1
    assert "unknown order" in result.output
    assert not output.exists()
    assert not output.with_suffix(".json").exists()
