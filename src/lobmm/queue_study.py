"""Independent artifact audit for the controlled cancellation-policy witness.

This checks the full replay against hand-derived queue and cash identities.
It does not call the matching engine to calculate expected results.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from lobmm.config import dump_config, load_config
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import load_events
from lobmm.data.manifest import sha256_file
from lobmm.enums import QueueAllocation, StrategyName
from lobmm.experiments import (
    ExperimentSpec,
    config_for_case,
    iter_cases,
    spec_as_dict,
)

FIXTURE_SHA256 = "3833652ba3e5781290010be29445b5c0c6762d79adbd212b81ba2e1662439071"
CONFIG_SHA256 = "16d06bd3442f9803cef7f85084b8d19d5761e9945a8ba63a682eaae577d6a04e"
QUEUE_AHEAD = {
    QueueAllocation.BACK_OF_QUEUE: 90,  # 100 - (60 - 50)
    QueueAllocation.PRO_RATA: 60,  # 100 - 60 * 100 / 150
    QueueAllocation.FRONT_OF_QUEUE: 40,  # 100 - 60
}


class QueueStudyError(ValueError):
    """A controlled replay artifact failed an independent audit."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QueueStudyError(message)


def _decimal(value: Any) -> Decimal:
    # Run tables serialize Decimal accounting as floats; recover the printed
    # decimal value and compare with the same 1e-12 accounting tolerance.
    return Decimal(str(value))


def audit_queue_study(
    experiment_directory: Path,
    config_path: Path,
) -> dict[str, Any]:
    """Verify all nine cases before producing a deterministic audit document.

    The exact versioned fixture is intentional: this oracle is only valid for
    its 100-ahead, own-10, add-50, cancel-60, trade-65 construction. Run configs
    must match the supplied base config with only the experiment overrides.
    Performance measurements and output paths are omitted from the audit.
    """

    _require(
        sha256_file(config_path) == CONFIG_SHA256, "unexpected controlled config hash"
    )
    config = load_config(config_path)
    fixture = config.data.input_path
    _require(fixture is not None, "the witness requires a canonical input file")
    assert fixture is not None
    fixture_hash = sha256_file(fixture)
    _require(fixture_hash == FIXTURE_SHA256, "unexpected controlled fixture hash")
    _require(
        config.data.provenance.checksum_sha256 == fixture_hash
        and config.data.provenance.synthetic_demonstration
        and config.strategy.name is StrategyName.FIXED_SPREAD
        and config.strategy.order_size == 10,
        "the witness requires hash-bound synthetic provenance and fixed-spread own-10",
    )
    stream_hash = event_stream_sha256(load_events(fixture))
    spec = ExperimentSpec(strategies=(StrategyName.FIXED_SPREAD,))
    manifest = json.loads((experiment_directory / "experiment.json").read_text())
    _require(
        manifest["spec"] == spec_as_dict(spec)
        and manifest["case_count"] == 9
        and manifest["event_count"] == 9
        and manifest["event_stream_sha256"] == stream_hash
        and manifest["synthetic_demonstration"] is True,
        "experiment manifest does not match the complete controlled grid",
    )
    sensitivity = pl.read_parquet(experiment_directory / "sensitivity.parquet")
    _require(
        sensitivity.height == 9
        and set(sensitivity["run_name"]) == {case.run_name for case in iter_cases(spec)}
        and set(sensitivity["event_stream_sha256"]) == {stream_hash},
        "sensitivity table does not match the controlled grid and stream hash",
    )
    rows: list[dict[str, Any]] = []
    for case in iter_cases(spec):
        run = experiment_directory / "runs" / case.run_name
        saved = load_config(run / "run_config.yaml")
        expected = config_for_case(
            config, case, runs_directory=saved.output.runs_directory
        )
        _require(
            dump_config(saved) == dump_config(expected), f"{case.run_name}: config"
        )
        latency = saved.latency
        _require(
            min(
                latency.market_data_ns,
                latency.order_entry_ns,
                latency.cancellation_ns,
                latency.fill_report_ns,
            )
            > 0,
            f"{case.run_name}: latency must be positive",
        )
        diagnostics = json.loads((run / "diagnostics.json").read_text())
        summary = json.loads((run / "summary.json").read_text())
        replay_hash = event_stream_sha256(
            load_events(run / "market.parquet", allow_extra_columns=True)
        )
        certificate = diagnostics["input_validation_certificate"]
        _require(
            diagnostics["event_stream_sha256"]
            == summary["event_stream_sha256"]
            == certificate["event_stream_sha256"]
            == replay_hash
            == stream_hash
            and certificate["mode"] == "strict"
            and diagnostics["input_validation_issue_count"] == 0
            and diagnostics["submitted_order_messages"] == 2
            and diagnostics["risk_blocked_quote_attempts"] == 0
            and diagnostics["exchange_rejected_quote_attempts"] == 0
            and diagnostics["true_book_diagnostics"] == {}
            and diagnostics["observed_book_diagnostics"] == {},
            f"{case.run_name}: input hashes, quote activity, or book diagnostics",
        )
        ahead = QUEUE_AHEAD[case.queue_allocation]
        quantity = min(10, max(0, 65 - ahead))
        fills = pl.read_parquet(run / "fills.parquet").to_dicts()
        _require(len(fills) == (2 if quantity else 0), f"{case.run_name}: fill count")
        if quantity:
            _require(
                {fill["side"] for fill in fills} == {-1, 1}, "both sides must fill"
            )
        total_fee = Decimal(0)
        total_rebate = Decimal(0)
        cash_ticks = 0
        for fill in fills:
            price = 99 if fill["side"] == 1 else 101
            _require(
                fill["quantity"] == quantity
                and fill["price_ticks"] == price
                and fill["liquidity_role"] == "maker"
                and fill["exchange_fill_timestamp_ns"] == 3_000_000
                and fill["decision_timestamp_ns"] == latency.market_data_ns
                and fill["send_timestamp_ns"] == latency.market_data_ns
                and fill["exchange_arrival_timestamp_ns"]
                == latency.market_data_ns + latency.order_entry_ns
                and fill["strategy_notification_timestamp_ns"]
                == 3_000_000 + latency.fill_report_ns,
                f"{case.run_name}: fill quantity, price, role, or causal timestamps",
            )
            notional = Decimal(quantity * price) * config.instrument.tick_size
            fee = (
                Decimal(quantity) * config.fees.maker_fee_per_unit
                + notional * config.fees.proportional_fee_rate
            )
            rebate = Decimal(quantity) * config.fees.maker_rebate_per_unit
            _require(
                abs(_decimal(fill["fee"]) - fee) <= Decimal("1e-12")
                and abs(_decimal(fill["rebate"]) - rebate) <= Decimal("1e-12"),
                f"{case.run_name}: fill costs",
            )
            total_fee += fee
            total_rebate += rebate
            cash_ticks -= fill["side"] * quantity * price
        orders = pl.read_parquet(run / "orders.parquet").to_dicts()
        live = [row for row in orders if row["new_status"] == "live"]
        _require(
            len(live) == 2
            and {row["side"] for row in live} == {-1, 1}
            and all(
                row["remaining_quantity"] == 10
                and row["exchange_arrival_timestamp_ns"]
                == latency.market_data_ns + latency.order_entry_ns
                < 1_000_000
                for row in live
            ),
            f"{case.run_name}: own quotes must join before the later add",
        )
        final_orders = {row["order_id"]: row for row in orders}
        _require(
            all(
                row["cumulative_filled_quantity"] == quantity
                and row["remaining_quantity"] == 10 - quantity
                and row["new_status"] == ("filled" if quantity == 10 else "cancelled")
                for row in final_orders.values()
            ),
            f"{case.run_name}: final order conservation/lifecycle",
        )
        final = pl.read_parquet(run / "pnl.parquet").tail(1).to_dicts()[0]
        gross = Decimal(cash_ticks) * config.instrument.tick_size
        net = gross - total_fee + total_rebate
        expected_accounting = {
            "trade_cash_ticks": cash_ticks,
            "gross_pnl": gross,
            "realized_pnl": gross,
            "unrealized_pnl": 0,
            "fees": total_fee,
            "rebates": total_rebate,
            "net_pnl": net,
            "cash": net,
            "inventory": 0,
            "buy_volume": quantity,
            "sell_volume": quantity,
            "turnover": Decimal(quantity * 200) * config.instrument.tick_size,
        }
        _require(
            all(
                abs(_decimal(final[key]) - _decimal(value)) <= Decimal("1e-12")
                for key, value in expected_accounting.items()
            )
            and abs(_decimal(summary["net_pnl"]) - net) <= Decimal("1e-12"),
            f"{case.run_name}: final cash, inventory, or P&L identity",
        )
        table_row = sensitivity.filter(pl.col("run_name") == case.run_name).row(
            0, named=True
        )
        _require(
            table_row["fill_events"] == len(fills)
            and table_row["end_inventory"] == 0
            and table_row["queue_allocation"] == case.queue_allocation.value
            and table_row["latency_multiplier"] == case.latency_multiplier
            and abs(_decimal(table_row["net_pnl"]) - net) <= Decimal("1e-12"),
            f"{case.run_name}: sensitivity metrics differ from audited fills",
        )
        rows.append(
            {
                "run_name": case.run_name,
                "latency_multiplier": case.latency_multiplier,
                "queue_allocation": case.queue_allocation.value,
                "external_ahead_after_cancel": ahead,
                "filled_per_side": quantity,
                "quote_arrival_ns": latency.market_data_ns + latency.order_entry_ns,
                "fill_notification_ns": 3_000_000 + latency.fill_report_ns
                if quantity
                else None,
                "event_stream_sha256": stream_hash,
                "gross_pnl": str(gross),
                "fees": str(total_fee),
                "rebates": str(total_rebate),
                "net_pnl": str(net),
                "end_inventory": 0,
            }
        )
    return {
        "schema_version": 1,
        "synthetic_demonstration": True,
        "fixture_sha256": fixture_hash,
        "config_sha256": sha256_file(config_path),
        "event_stream_sha256": stream_hash,
        "event_count": 9,
        "case_count": len(rows),
        "cases": rows,
    }


def write_queue_study(audit: dict[str, Any], output: Path) -> None:
    """Write a curated Markdown report and a deterministic JSON companion."""

    lines = [
        "# Controlled queue-cancellation replay",
        "",
        "**SYNTHETIC MECHANICS VALIDATION.** These results test execution and "
        "accounting; they do not establish real-market profitability.",
        "",
        "The ordinary experiment workflow replays one nine-event canonical CSV "
        "through fixed-spread quoting. Only cancellation allocation and latency "
        "change across the nine cases. The artifact audit checks the analytical "
        "oracle independently of the matching engine, both sides, maker costs, "
        "quote lifecycle, causal timestamps, and matching input hashes.",
        "",
        "## Tape and analytical oracle",
        "",
        "At 0 ms the external bid at 99 ticks and ask at 101 ticks each contain "
        "100 units. The delayed strategy submits one post-only 10-unit quote at "
        "each price. Quotes arrive at 0.125, 0.250, or 0.500 ms, before 50 units "
        "are added behind each quote at 1 ms. At 2 ms, 60 external units cancel "
        "on each side. At 3 ms, a 65-unit trade consumes each resting side. "
        "A harmless deeper bid add at 5 ms extends the session so execution "
        "reports and positive-latency quote cancellations can drain.",
        "",
        "- Back: remove the 50 behind, then 10 ahead; ahead = 90.",
        "- Pro rata: remove 40 of the 100 ahead and 20 of the 50 behind; ahead = 60.",
        "- Front: remove 60 ahead; ahead = 40.",
        "",
        "Own fill per side = `min(10, max(0, 65 - ahead))`: **0 / 5 / 10**. "
        "The proportional allocation is integral here, so rounding is not a "
        "confounder. Trades use resting-side semantics. Price-through fills "
        "are disabled; all fills are exact-price maker executions.",
        "",
        "## Verified cases",
        "",
        "| Latency x | Allocation | Ahead | Fill/side | Quote arrival ns | "
        "Gross USD | Fees USD | Rebates USD | Net USD | End inventory |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in audit["cases"]:
        lines.append(
            "| {latency_multiplier:g} | {queue_allocation} | "
            "{external_ahead_after_cancel} | {filled_per_side} | "
            "{quote_arrival_ns} | {gross_pnl} | {fees} | {rebates} | "
            "{net_pnl} | {end_inventory} |".format(**row)
        )
    lines.extend(
        [
            "",
            "Base latencies (ns): market data 100000, order entry 150000, "
            "cancellation 150000, fill report 100000. Every channel has positive "
            "delay in every case, with zero jitter. Fill notifications arrive "
            "at 3.05, 3.10, or 3.20 ms. Long quote lifetimes avoid refresh "
            "confounding; new quotes are suppressed from 3 ms onward.",
            "",
            "With tick size 0.01 and equal buy/sell quantity q, trade cash is "
            "`(101 - 99) * q` ticks and end inventory is zero. Gross USD = "
            "`0.02 * q`; fees = `2 * q * 0.002 + (99 + 101) * q * 0.01 * "
            "0.0001`; rebates = `2 * q * 0.0005`; net = gross - fees + rebates. "
            "The audit also checks each fill's cost and realized/unrealized "
            "P&L. Pro rata ends with five unfilled units per order cancelled; "
            "front ends fully filled; back cancels the unfilled ten units.",
            "",
            "## Reproduce and audit",
            "",
            "Run from the repository root after installing `.[dev]`:",
            "",
            "```bash",
            "python -m lobmm.cli experiment --config configs/queue_cancellation.yaml "
            "--name queue-cancellation --latency-multipliers 0.5,1,2",
            "python -m lobmm.cli audit-queue-study --experiment "
            "experiments/queue-cancellation --config configs/queue_cancellation.yaml "
            "--output docs/QUEUE_CANCELLATION.md",
            "```",
            "",
            f"- Canonical event-stream SHA-256: `{audit['event_stream_sha256']}`",
            f"- Fixture CSV SHA-256: `{audit['fixture_sha256']}`",
            f"- Base YAML SHA-256: `{audit['config_sha256']}`",
            "",
            f"[{output.with_suffix('.json').name}]({output.with_suffix('.json').name}) "
            "contains the complete deterministic audit, including the stream "
            "hash for each case. Each run's summary, diagnostics, validation "
            "certificate, and sensitivity row must match that hash. Raw replay "
            "tables stay under the ignored experiment directory. Repeated-run "
            "tests compare all eight Parquet tables byte for byte and the "
            "curated audit; wall-clock performance measurements are excluded.",
            "",
            "## Scope and limitations",
            "",
            "This witness closes the published demo's inability to separate "
            "cancellation policies. The original short synthetic path still "
            "produces identical policy metrics. Here latency multipliers are "
            "deliberately within the pre-add arrival window, so latency does "
            "not change fills. This is a cancellation-policy witness, not "
            "latency calibration or an empirical estimate of queue position.",
            "",
            "L2 cannot identify cancellation ownership. The external tape "
            "remains exogenous, while own fills displace historical external "
            "volume in the overlay; both trades happen before strategy "
            "feedback. Transition rows' queue estimates reflect final order "
            "state, so the ahead values above come from the analytical tape, "
            "not historical snapshots in those rows. The nine-event session "
            "does not support default 100 ms/1 s/5 s markouts or performance "
            "claims. Negative-control tests remove cancellations or move "
            "quote arrival after the add and require zero fills under every "
            "policy. Those controls are separate from this audited grid.",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    output.with_suffix(".json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
