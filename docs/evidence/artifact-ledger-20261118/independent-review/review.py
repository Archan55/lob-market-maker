"""Distinct signed-acquisition-pool review of fixed exposed artifact cohorts.

Authoring/importing this file does not read artifacts or run arithmetic cases.
Execute only after the owner's result-review GO. No application or primary
auditor helper is imported. Root is explicit through --root (default: cwd).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import time
import traceback
from collections import Counter
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

RESOURCE = Path("docs/evidence/end-to-end-resources-20261117/run")
SOURCE = "0866bf13e449cd66e3862454eaaf424a5172c33a"
COHORTS = ("001", "002")
FIELDS = (
    "timestamp_ns",
    "currency",
    "tick_value",
    "mark_ticks",
    "inventory",
    "average_cost_ticks",
    "trade_cash_ticks",
    "trade_cash",
    "cash",
    "realized_pnl_ticks",
    "unrealized_pnl_ticks",
    "gross_pnl_ticks",
    "realized_pnl",
    "unrealized_pnl",
    "gross_pnl",
    "fees",
    "rebates",
    "net_pnl",
    "turnover_ticks",
    "turnover",
    "buy_volume",
    "sell_volume",
    "fill_count",
    "current_gross_exposure",
    "peak_gross_exposure",
)
INTS = {
    "timestamp_ns",
    "inventory",
    "trade_cash_ticks",
    "turnover_ticks",
    "buy_volume",
    "sell_volume",
    "fill_count",
}
INV_FIELDS = (
    "timestamp_ns",
    "inventory",
    "current_gross_exposure",
    "peak_gross_exposure",
)
STARTED = None


def demand(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded(value: Fraction) -> Fraction:
    demand(
        max(value.numerator.bit_length(), value.denominator.bit_length()) <= 65536,
        "independent rational cap exceeded",
    )
    return value


def factors(value: Fraction) -> tuple[int, int, int]:
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    return denominator, twos, fives


def project(value: Fraction) -> float:
    """Decimal reference, independently of the primary integer-rounding oracle.

    Terminating values construct an exact coefficient/exponent Decimal. Integer
    Decimal construction avoids Python's integer-to-string safety guard. Only
    repeating values use the declared local 28-digit HALF_EVEN division.
    """
    bounded(value)
    if not value:
        return 0.0
    remainder, twos, fives = factors(value)
    if remainder == 1:
        scale = max(twos, fives)
        coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
        digits = Decimal(coefficient).as_tuple().digits
        exact = Decimal((int(value < 0), digits, -scale))
        actual = float(exact)
    else:
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            rounded = Decimal(value.numerator) / Decimal(value.denominator)
            actual = float(rounded)
    demand(math.isfinite(actual), "independent projection is not finite")
    return actual


def token(value: Any) -> Any:
    if isinstance(value, Fraction):
        return {"float64": struct.pack(">d", project(value)).hex()}
    if type(value) is float:
        demand(math.isfinite(value), "nonfinite observed numeric field")
        return {"float64": struct.pack(">d", value).hex()}
    return value


def exact_token(value: Fraction) -> dict[str, str]:
    return {
        "numerator_hex": hex(value.numerator),
        "denominator_hex": hex(value.denominator),
    }


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def literal_config(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    import yaml

    tree = yaml.compose(path.read_text())

    def scalar(*keys: str) -> str:
        node = tree
        for key in keys:
            demand(isinstance(node, yaml.MappingNode), "YAML path is not a mapping")
            matches = [value for name, value in node.value if name.value == key]
            demand(len(matches) == 1, "missing or duplicate literal YAML key")
            node = matches[0]
        demand(
            isinstance(node, yaml.ScalarNode), "economic configuration is not scalar"
        )
        return node.value

    literals = {
        "tick": scalar("instrument", "tick_size"),
        "currency": scalar("instrument", "currency"),
        "maker_fee": scalar("fees", "maker_fee_per_unit"),
        "maker_rebate": scalar("fees", "maker_rebate_per_unit"),
        "taker_fee": scalar("fees", "taker_fee_per_unit"),
        "proportional": scalar("fees", "proportional_fee_rate"),
    }
    demand(
        scalar("backtest", "mark_price") == "midpoint",
        "recorded mark policy is not midpoint",
    )
    config = {
        key: value if key == "currency" else bounded(Fraction(value))
        for key, value in literals.items()
    }
    demand(bool(config["currency"]) and config["tick"] > 0, "invalid currency/tick")
    demand(
        all(
            config[key] >= 0
            for key in ("maker_fee", "maker_rebate", "taker_fee", "proportional")
        ),
        "negative economic configuration",
    )
    return config, literals


class AcquisitionPool:
    """Signed remaining acquisition pool; closed P&L is derived from cash.

    Opposing fills retire a proportional part of the signed acquisition pool;
    any residual fill then creates a new pool. No acquisition average or running
    sum of closed-position P&L is stored.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.position = self.trade_cash = self.turnover = self.buys = self.sells = (
            self.count
        ) = 0
        self.pool = self.fees = self.rebates = self.peak = Fraction(0)
        self.coverage = Counter()

    def apply(self, fill: dict[str, Any]) -> dict[str, Fraction]:
        side, units, price = (fill[key] for key in ("side", "quantity", "price_ticks"))
        change = side * units
        previous = self.position
        if previous and previous * change < 0:
            retired = min(abs(previous), units)
            self.pool -= self.pool * Fraction(retired, abs(previous))
            residual = units - retired
            self.pool += side * residual * price
            transition = (
                "partial_close"
                if retired < abs(previous)
                else "flat_close"
                if not residual
                else "reversal"
            )
        else:
            self.pool += change * price
            transition = "same_side_accumulation" if previous else "opening"
        self.position += change
        self.trade_cash -= change * price
        self.turnover += units * price
        self.buys += units if side == 1 else 0
        self.sells += units if side == -1 else 0
        self.count += 1
        role = fill["liquidity_role"]
        costs = {
            "fee": units * self.config[role + "_fee"]
            + units * price * self.config["tick"] * self.config["proportional"],
            "rebate": units * self.config["maker_rebate"]
            if role == "maker"
            else Fraction(0),
        }
        self.fees += costs["fee"]
        self.rebates += costs["rebate"]
        self.coverage["role_" + role] += 1
        self.coverage[transition] += 1
        self.coverage[
            "position_after_"
            + (
                "long"
                if self.position > 0
                else "short"
                if self.position < 0
                else "flat"
            )
        ] += 1
        for item in (self.pool, self.fees, self.rebates, *costs.values()):
            bounded(item)
        demand(
            bool(self.position) or self.pool == 0,
            "flat signed acquisition pool is not zero",
        )
        return costs

    def expected(self, mark: Fraction, timestamp: int) -> dict[str, Any]:
        realized = Fraction(self.trade_cash) + self.pool
        unrealized = self.position * mark - self.pool
        gross = Fraction(self.trade_cash) + self.position * mark
        demand(realized + unrealized == gross, "signed-pool exact identity failed")
        tick = self.config["tick"]
        exposure = abs(self.position) * mark * tick
        self.peak = max(self.peak, exposure)
        values = {
            "timestamp_ns": timestamp,
            "currency": self.config["currency"],
            "tick_value": tick,
            "mark_ticks": mark,
            "inventory": self.position,
            "average_cost_ticks": self.pool / self.position if self.position else None,
            "trade_cash_ticks": self.trade_cash,
            "trade_cash": self.trade_cash * tick,
            "cash": self.trade_cash * tick - self.fees + self.rebates,
            "realized_pnl_ticks": realized,
            "unrealized_pnl_ticks": unrealized,
            "gross_pnl_ticks": gross,
            "realized_pnl": realized * tick,
            "unrealized_pnl": unrealized * tick,
            "gross_pnl": gross * tick,
            "fees": self.fees,
            "rebates": self.rebates,
            "net_pnl": gross * tick - self.fees + self.rebates,
            "turnover_ticks": self.turnover,
            "turnover": self.turnover * tick,
            "buy_volume": self.buys,
            "sell_volume": self.sells,
            "fill_count": self.count,
            "current_gross_exposure": exposure,
            "peak_gross_exposure": self.peak,
        }
        for item in values.values():
            if isinstance(item, Fraction):
                bounded(item)
        return values


def source_proof(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run = root / RESOURCE
    manifest = json.loads((run / "manifest.json").read_text())
    plan = json.loads((run / "plan.json").read_text())
    demand(
        digest(run / "plan.json") == manifest["plan_sha256"],
        "original plan hash changed",
    )
    demand(
        plan["source_revision"] == SOURCE and len(plan["source_sha256"]) == 38,
        "frozen source identity differs",
    )
    actual = {
        path.relative_to(root).as_posix(): digest(path)
        for path in (root / "src/lobmm").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    demand(actual == plan["source_sha256"], "current production source differs")
    return manifest, plan


def schema_check(table: Any, names: tuple[str, ...]) -> None:
    import pyarrow as pa

    demand(tuple(table.schema_arrow.names) == names, "mandatory ordered schema differs")
    for field in table.schema_arrow:
        expected = (
            pa.types.is_int64(field.type)
            if field.name in INTS
            else (
                pa.types.is_string(field.type) or pa.types.is_large_string(field.type)
            )
            if field.name == "currency"
            else pa.types.is_float64(field.type)
        )
        demand(expected, "mandatory Arrow field dtype differs: " + field.name)


def review_cohort(
    root: Path, ident: str, manifest: dict[str, Any], progress: dict[str, Any]
) -> dict[str, Any]:
    import pyarrow.parquet as pq

    began = time.monotonic()
    progress.update(
        cohort=ident,
        status="started",
        completed_pnl_rows=0,
        consumed_fills=0,
        mismatch_count=0,
    )
    prefix = Path("attempts") / ident
    engine = prefix / "engine/resource-study"
    relative = {
        "fills": engine / "fills.parquet",
        "pnl": engine / "pnl.parquet",
        "inventory": engine / "inventory.parquet",
        "config": engine / "run_config.yaml",
        "input": prefix / "input/events.parquet",
        "resource_result": prefix / "result.json",
    }
    paths = {key: root / RESOURCE / path for key, path in relative.items()}
    hashes = {}
    for key, path in paths.items():
        hashes[key] = digest(path)
        demand(
            hashes[key]
            == manifest["original_file_stats"][relative[key].as_posix()]["sha256"],
            "original cohort bytes changed: " + key,
        )
    config, literals = literal_config(paths["config"])
    resource = json.loads(paths["resource_result"].read_text())
    original = pq.read_table(paths["input"])
    event_hash = hashlib.sha256()
    event_codes = {"ADD": 1, "CANCEL": 2, "TRADE": 3, "RESET": 4, "SNAPSHOT": 5}
    for event in original.to_pylist():
        event_hash.update(
            struct.pack(
                ">qqbbqq",
                event["timestamp_ns"],
                event["sequence_number"],
                event_codes[event["event_type"]],
                event["side"] or 0,
                event["price_ticks"],
                event["quantity"],
            )
        )
    demand(
        original.num_rows == (2000 if ident == "001" else 20000)
        and event_hash.hexdigest() == resource["input"]["event_stream_sha256"],
        "fixed original input count/fingerprint differs",
    )
    pnl_file, inv_file = (
        pq.ParquetFile(paths["pnl"]),
        pq.ParquetFile(paths["inventory"]),
    )
    schema_check(pnl_file, FIELDS)
    schema_check(inv_file, INV_FIELDS)
    demand(
        0 < pnl_file.metadata.num_rows <= 100000
        and pnl_file.metadata.num_rows == inv_file.metadata.num_rows,
        "row count/admission cap",
    )
    fills = pq.read_table(paths["fills"]).to_pylist()
    demand(len(fills) <= 20000, "fill count cap")
    seen, previous_time = set(), -1
    for fill in fills:
        demand(
            type(fill["fill_id"]) is str
            and fill["fill_id"]
            and fill["fill_id"] not in seen,
            "fill ID admission failed",
        )
        seen.add(fill["fill_id"])
        demand(
            type(fill["side"]) is int and fill["side"] in (-1, 1), "fill side admission"
        )
        demand(
            all(
                type(fill[key]) is int and fill[key] > 0
                for key in ("price_ticks", "quantity")
            ),
            "fill integer admission",
        )
        demand(fill["liquidity_role"] in ("maker", "taker"), "fill role admission")
        timestamp = fill["exchange_fill_timestamp_ns"]
        demand(
            type(timestamp) is int and timestamp >= max(0, previous_time),
            "fill order admission",
        )
        previous_time = timestamp
        clock = [
            fill[key]
            for key in (
                "decision_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
                "exchange_fill_timestamp_ns",
                "strategy_notification_timestamp_ns",
            )
            if key in fill and fill[key] is not None
        ]
        demand(
            all(type(item) is int and item >= 0 for item in clock)
            and all(a <= b for a, b in pairwise(clock)),
            "fill clock admission",
        )
        demand(
            all(
                type(fill[key]) is float and math.isfinite(fill[key])
                for key in ("fee", "rebate")
            ),
            "fill cost storage admission",
        )

    def rows(path: Path) -> Any:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=2048):
            yield from batch.to_pylist()

    ledger = AcquisitionPool(config)
    row_expected, row_observed, mismatch_digest = (hashlib.sha256() for _ in range(3))
    previous_prefix, previous_timestamp = None, -1
    repeating, snapshots = Counter(), Counter()
    mismatch_count, row_count, fill_cost_checks = 0, 0, 0
    examples = []

    def mismatch(kind: str, index: int, field: str, expected: Any, actual: Any) -> None:
        nonlocal mismatch_count
        detail = {
            "kind": kind,
            "index": index,
            "field": field,
            "expected": token(expected),
            "actual": token(actual),
        }
        mismatch_count += 1
        progress["mismatch_count"] = mismatch_count
        mismatch_digest.update(json_bytes(detail))
        if len(examples) < 256:
            examples.append(detail)

    for index, (row, inventory) in enumerate(
        zip(rows(paths["pnl"]), rows(paths["inventory"]), strict=True)
    ):
        demand(
            time.monotonic() - began <= 180 and time.monotonic() - STARTED <= 600,
            "independent review wall cap",
        )
        timestamp, count = row["timestamp_ns"], row["fill_count"]
        demand(
            type(timestamp) is int and timestamp >= max(0, previous_timestamp),
            "snapshot time admission",
        )
        demand(
            type(count) is int and 0 <= count <= len(fills),
            "prefix type/range admission",
        )
        demand(
            count == 0
            if previous_prefix is None
            else count - previous_prefix in (0, 1),
            "prefix initial/order admission",
        )
        if count > ledger.count:
            demand(
                timestamp == fills[count - 1]["exchange_fill_timestamp_ns"],
                "first prefix row is not the corresponding fill time",
            )
            costs = ledger.apply(fills[count - 1])
            for field, value in costs.items():
                fill_cost_checks += 1
                if token(value) != token(fills[count - 1][field]):
                    mismatch("fill_cost", count, field, value, fills[count - 1][field])
        mark = row["mark_ticks"]
        demand(
            type(mark) is float and math.isfinite(mark) and math.copysign(1, mark) > 0,
            "mark finite/sign admission",
        )
        exact_mark = Fraction.from_float(mark)
        demand(
            abs(exact_mark) < 2**52
            and exact_mark.denominator in (1, 2)
            and (mark > 0 or ledger.position == 0),
            "mark bounded half-grid admission",
        )
        expect = ledger.expected(exact_mark, timestamp)
        demand(
            (row["average_cost_ticks"] is None) == (ledger.position == 0),
            "flat average-cost null policy",
        )
        wanted, observed = [], []
        for field in FIELDS:
            actual = row[field]
            if field in INTS:
                demand(type(actual) is int, "original Int64 row type changed: " + field)
            elif field == "currency":
                demand(type(actual) is str, "original currency type changed")
            elif actual is not None:
                demand(
                    type(actual) is float and math.isfinite(actual),
                    "original Float64 row type/finite policy: " + field,
                )
            else:
                demand(field == "average_cost_ticks", "unexpected null snapshot field")
            if isinstance(expect[field], Fraction) and factors(expect[field])[0] != 1:
                repeating[field] += 1
            wanted.append([field, token(expect[field])])
            observed.append([field, token(actual)])
            if wanted[-1] != observed[-1]:
                mismatch("snapshot", index, field, expect[field], actual)
        for field in INV_FIELDS:
            actual = inventory[field]
            demand(
                type(actual) is int
                if field in INTS
                else type(actual) is float and math.isfinite(actual),
                "inventory storage admission",
            )
            wanted.append(["inventory." + field, token(expect[field])])
            observed.append(["inventory." + field, token(actual)])
            if wanted[-1] != observed[-1]:
                mismatch("inventory", index, field, expect[field], actual)
        row_expected.update(json_bytes(wanted))
        row_observed.update(json_bytes(observed))
        snapshots[
            "flat"
            if ledger.position == 0
            else "long"
            if ledger.position > 0
            else "short"
        ] += 1
        row_count += 1
        progress.update(completed_pnl_rows=row_count, consumed_fills=ledger.count)
        previous_prefix, previous_timestamp = count, timestamp
    demand(
        previous_prefix == len(fills) == ledger.count,
        "not every retained fill prefix was observed",
    )
    demand(time.monotonic() - began <= 180, "independent review cohort cap")
    return {
        "cohort": ident,
        "status": "complete",
        "input_sha256": hashes,
        "input_fingerprint": event_hash.hexdigest(),
        "literal_economics": literals,
        "pnl_rows": row_count,
        "fills": len(fills),
        "snapshot_fields_compared": row_count * 25,
        "inventory_fields_compared": row_count * 4,
        "fill_cost_fields_compared": fill_cost_checks,
        "mismatch_count": mismatch_count,
        "mismatches_sha256": mismatch_digest.hexdigest(),
        "mismatch_examples_first_256": examples,
        "mismatch_examples_truncated": mismatch_count > len(examples),
        "expected_rows_chain_sha256": row_expected.hexdigest(),
        "observed_rows_chain_sha256": row_observed.hexdigest(),
        "fill_coverage": dict(ledger.coverage),
        "snapshot_coverage": dict(snapshots),
        "repeating_field_rows": dict(repeating),
        "final_exact_realized_ticks": exact_token(
            Fraction(ledger.trade_cash) + ledger.pool
        ),
        "final_exact_acquisition_pool_ticks": exact_token(ledger.pool),
    }


def main() -> int:
    global STARTED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    STARTED = time.monotonic()
    guard = sys.get_int_max_str_digits()
    result: dict[str, Any] = {
        "status": "started",
        "independent_source_sha256": digest(Path(__file__).resolve()),
        "root": str(args.root.resolve()),
        "cohorts": [],
    }
    try:
        demand(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application import detected",
        )
        manifest, plan = source_proof(args.root.resolve())
        result["original_source_revision"] = plan["source_revision"]
        result["source_file_count"] = len(plan["source_sha256"])
        for ident in COHORTS:
            progress: dict[str, Any] = {}
            result["cohorts"].append(progress)
            progress.update(
                review_cohort(args.root.resolve(), ident, manifest, progress)
            )
        demand(
            not any(
                name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
            ),
            "application import detected after review",
        )
        demand(sys.get_int_max_str_digits() == guard, "integer-string guard changed")
        result.update(
            status="complete",
            all_fields_match=all(
                row["mismatch_count"] == 0 for row in result["cohorts"]
            ),
            integer_string_guard_unchanged=True,
            scope="Corroboration of the same two exposed immutable synthetic records; conditional on emitted fills and admitted recorded marks; no engine/resource run, hidden-state/context/causal-mark/fill-realism/economic proof",
        )
    except Exception as error:
        result.update(
            status="stopped",
            error={"type": type(error).__name__, "message": str(error)},
            traceback=traceback.format_exc(),
        )
    encoded = json_bytes(result)
    demand(len(encoded) <= 128 * 1024**2, "independent JSON evidence cap")
    sys.stdout.buffer.write(encoded)
    return 0 if result.get("all_fields_match") else 1


if __name__ == "__main__":
    raise SystemExit(main())
