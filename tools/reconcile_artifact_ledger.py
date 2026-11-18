"""Independent offline reconciliation of two frozen synthetic artifact cohorts.

This module never imports lobmm. Importing it performs no data reads or audit.
The ledger tracks acquisition averages and separately sums closed-position P&L.
The renderer uses exact integer decimal rounding, independent of Decimal context.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/reconcile_artifact_ledger.py"
TEST = "tests/unit/test_artifact_ledger.py"
DESIGN = "docs/ARTIFACT_LEDGER_DESIGN.md"
RESOURCE = "docs/evidence/end-to-end-resources-20261117/run"
SOURCE_REVISION = "0866bf13e449cd66e3862454eaaf424a5172c33a"
ARTIFACT_BASE = "ec7b6e0bce491d508c30a44b737950cda503d597"
ORIGINAL_METADATA = {
    RESOURCE
    + "/manifest.json": "a0022001dd042444b7f7500c55404bd14c744478e16829fa2ccab86d6fcdeda4",
    RESOURCE
    + "/plan.json": "ffcfa3bae32b4263c9e6d681b2291ffad69b0732f35e38ff62bf942792ef62b0",
}
COHORTS = ("001", "002")
RATIONAL_BIT_LIMIT = 65536
PNL_ROW_LIMIT = 100000
FILL_LIMIT = 20000
EVIDENCE_LIMIT = 128 * 1024**2
WORKER_SECONDS = 180
STAGE_SECONDS = 600
SNAPSHOT_FIELDS = (
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
INVENTORY_FIELDS = (
    "timestamp_ns",
    "inventory",
    "current_gross_exposure",
    "peak_gross_exposure",
)
INTEGER_FIELDS = {
    "timestamp_ns",
    "inventory",
    "trade_cash_ticks",
    "turnover_ticks",
    "buy_volume",
    "sell_volume",
    "fill_count",
}


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def enforce_rational_cap(value: Fraction) -> None:
    require(isinstance(value, Fraction), "oracle values must be rational")
    require(
        value.numerator.bit_length() <= RATIONAL_BIT_LIMIT
        and value.denominator.bit_length() <= RATIONAL_BIT_LIMIT,
        "rational numerator or denominator exceeded 65536 bits",
    )


def terminates(value: Fraction) -> bool:
    denominator = value.denominator
    denominator >>= (denominator & -denominator).bit_length() - 1
    while denominator % 5 == 0:
        denominator //= 5
    return denominator == 1


def round_significant(value: Fraction, precision: int = 28) -> Fraction:
    """Exact significant-digit HALF_EVEN using integer quotient/remainder."""
    enforce_rational_cap(value)
    require(type(precision) is int and precision > 0, "precision must be positive")
    if not value:
        return Fraction(0)
    numerator, denominator = abs(value.numerator), value.denominator
    # This integer estimate is corrected by exact comparisons; no float/log10
    # or conversion of huge integers to decimal strings is involved.
    exponent = ((numerator.bit_length() - denominator.bit_length()) * 30103) // 100000

    def at_least(power: int) -> bool:
        return (
            numerator >= denominator * 10**power
            if power >= 0
            else numerator * 10**-power >= denominator
        )

    while not at_least(exponent):
        exponent -= 1
    while at_least(exponent + 1):
        exponent += 1
    scale = precision - 1 - exponent
    dividend = numerator * 10**scale if scale >= 0 else numerator
    divisor = denominator if scale >= 0 else denominator * 10**-scale
    quotient, remainder = divmod(dividend, divisor)
    if remainder * 2 > divisor or (remainder * 2 == divisor and quotient % 2):
        quotient += 1
    rounded = (
        Fraction(quotient, 10**scale) if scale >= 0 else Fraction(quotient * 10**-scale)
    )
    if value < 0:
        rounded = -rounded
    enforce_rational_cap(rounded)
    return rounded


def render_fraction(value: Fraction | None) -> float | None:
    if value is None:
        return None
    enforce_rational_cap(value)
    if not value:
        return 0.0
    displayed = value if terminates(value) else round_significant(value)
    try:
        result = float(displayed)
    except OverflowError as exc:
        raise ValueError("oracle binary64 projection overflow") from exc
    require(math.isfinite(result), "nonfinite oracle projection")
    return result


@dataclass(frozen=True)
class EconomicConfig:
    tick: Fraction
    currency: str
    maker_fee: Fraction
    maker_rebate: Fraction
    taker_fee: Fraction
    proportional: Fraction

    def __post_init__(self) -> None:
        require(
            isinstance(self.currency, str) and bool(self.currency),
            "currency must be nonempty",
        )
        for value in (
            self.tick,
            self.maker_fee,
            self.maker_rebate,
            self.taker_fee,
            self.proportional,
        ):
            enforce_rational_cap(value)
            require(value >= 0, "negative economic configuration")
        require(self.tick > 0, "tick must be positive")


def load_economics(path: Path) -> tuple[EconomicConfig, dict[str, Any]]:
    import yaml

    raw = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    require(
        raw["backtest"]["mark_price"] == "midpoint",
        "only the frozen midpoint policy is admitted",
    )
    fees = raw["fees"]
    literals = {
        "tick": raw["instrument"]["tick_size"],
        "currency": raw["instrument"]["currency"],
        "maker_fee": fees["maker_fee_per_unit"],
        "maker_rebate": fees["maker_rebate_per_unit"],
        "taker_fee": fees["taker_fee_per_unit"],
        "proportional": fees["proportional_fee_rate"],
    }
    require(
        all(type(value) is str for value in literals.values()),
        "economic YAML scalars lost original text",
    )
    config = EconomicConfig(
        **{
            name: value if name == "currency" else Fraction(value)
            for name, value in literals.items()
        }
    )
    return config, literals


def validate_fills(rows: list[dict[str, Any]]) -> None:
    require(len(rows) <= FILL_LIMIT, "fill count cap")
    identifiers: set[str] = set()
    previous_time = -1
    for row in rows:
        identifier = row["fill_id"]
        require(
            type(identifier) is str
            and bool(identifier)
            and identifier not in identifiers,
            "fill IDs must be unique and nonempty",
        )
        identifiers.add(identifier)
        require(
            type(row["side"]) is int and row["side"] in (-1, 1), "invalid fill side"
        )
        for name in ("quantity", "price_ticks"):
            require(
                type(row[name]) is int and row[name] > 0,
                "fill price/quantity must be positive builtin integers",
            )
        require(row["liquidity_role"] in ("maker", "taker"), "invalid liquidity role")
        timestamp = row["exchange_fill_timestamp_ns"]
        require(
            type(timestamp) is int and timestamp >= previous_time and timestamp >= 0,
            "fill times moved backward",
        )
        previous_time = timestamp
        chain = [
            row[name]
            for name in (
                "decision_timestamp_ns",
                "send_timestamp_ns",
                "exchange_arrival_timestamp_ns",
                "exchange_fill_timestamp_ns",
                "strategy_notification_timestamp_ns",
            )
            if name in row and row[name] is not None
        ]
        require(
            all(type(value) is int and value >= 0 for value in chain)
            and all(a <= b for a, b in itertools.pairwise(chain)),
            "incoherent fill clock chain",
        )
        for name in ("fee", "rebate"):
            if name in row:
                require(
                    type(row[name]) is float and math.isfinite(row[name]),
                    "saved fill cost must be finite Float64",
                )


def validate_prefix_step(
    previous_count: int | None,
    current_count: int,
    fill_total: int,
    timestamp_ns: int,
    fill_rows: list[dict[str, Any]],
) -> None:
    require(
        type(timestamp_ns) is int and timestamp_ns >= 0, "invalid snapshot timestamp"
    )
    require(
        type(current_count) is int and 0 <= current_count <= fill_total,
        "invalid fill prefix",
    )
    if previous_count is None:
        require(current_count == 0, "the initial retained prefix must be zero")
    else:
        require(
            current_count - previous_count in (0, 1),
            "fill prefix must advance by zero or one",
        )
        if current_count == previous_count + 1:
            require(
                timestamp_ns
                == fill_rows[current_count - 1]["exchange_fill_timestamp_ns"],
                "first row of a prefix has the wrong fill timestamp",
            )


def admit_mark(value: float, inventory: int) -> Fraction:
    require(
        type(value) is float and math.isfinite(value), "mark must be finite Float64"
    )
    require(
        math.copysign(1.0, value) > 0,
        "negative marks and negative zero are inadmissible",
    )
    mark = Fraction.from_float(value)
    require(
        abs(mark) < 2**52 and mark.denominator in (1, 2),
        "mark does not identify an exact bounded half-tick",
    )
    require(
        mark >= 0 and (inventory == 0 or mark > 0),
        "mark must be nonnegative flat and positive open",
    )
    return mark


class WeightedAverageLedger:
    """Acquisition average plus accumulated P&L on separately closed units."""

    def __init__(self, config: EconomicConfig) -> None:
        self.config = config
        self.inventory = 0
        self.average: Fraction | None = None
        self.closed_pnl: list[Fraction] = []
        self.realized = Fraction(0)
        self.trade_cash_ticks = self.turnover_ticks = self.buy_volume = (
            self.sell_volume
        ) = self.fill_count = 0
        self.fees = self.rebates = self.peak_exposure = Fraction(0)
        self.coverage: Counter[str] = Counter()
        self.last_transition: dict[str, Any] = {}
        self.max_numerator_bits = self.max_denominator_bits = 0

    def observe_depth(self, value: Fraction) -> None:
        enforce_rational_cap(value)
        self.max_numerator_bits = max(
            self.max_numerator_bits, value.numerator.bit_length()
        )
        self.max_denominator_bits = max(
            self.max_denominator_bits, value.denominator.bit_length()
        )

    def apply_fill(self, fill: dict[str, Any]) -> dict[str, Fraction]:
        side, quantity, price = fill["side"], fill["quantity"], fill["price_ticks"]
        require(
            type(side) is int
            and side in (-1, 1)
            and type(quantity) is int
            and quantity > 0
            and type(price) is int
            and price > 0,
            "invalid oracle fill",
        )
        role = fill["liquidity_role"]
        require(role in ("maker", "taker"), "invalid oracle role")
        old_inventory, old_average = self.inventory, self.average
        closed, closed_pnl = 0, Fraction(0)
        if not old_inventory:
            self.average = Fraction(price)
            transition = "opening"
        elif (old_inventory > 0) == (side > 0):
            require(old_average is not None, "missing acquisition average")
            self.average = (old_average * abs(old_inventory) + price * quantity) / (
                abs(old_inventory) + quantity
            )
            transition = "same_side_accumulation"
        else:
            require(old_average is not None, "missing closing average")
            closed = min(abs(old_inventory), quantity)
            closed_pnl = (
                closed
                * (Fraction(price) - old_average)
                * (1 if old_inventory > 0 else -1)
            )
            self.closed_pnl.append(closed_pnl)
            self.realized += closed_pnl
            if quantity < abs(old_inventory):
                transition = "partial_close"
            elif quantity == abs(old_inventory):
                self.average = None
                transition = "flat_close"
            else:
                self.average = Fraction(price)
                transition = "reversal"
        self.inventory += side * quantity
        self.trade_cash_ticks -= side * quantity * price
        self.turnover_ticks += quantity * price
        self.buy_volume += quantity if side == 1 else 0
        self.sell_volume += quantity if side == -1 else 0
        self.fill_count += 1
        proportional = quantity * price * self.config.tick * self.config.proportional
        fee = (
            quantity
            * (self.config.maker_fee if role == "maker" else self.config.taker_fee)
            + proportional
        )
        rebate = quantity * self.config.maker_rebate if role == "maker" else Fraction(0)
        self.fees += fee
        self.rebates += rebate
        for value in (
            self.average,
            closed_pnl,
            self.realized,
            self.fees,
            self.rebates,
            fee,
            rebate,
        ):
            if value is not None:
                self.observe_depth(value)
        self.coverage["role_" + role] += 1
        self.coverage[transition] += 1
        self.last_transition = {
            "kind": transition,
            "old_inventory": old_inventory,
            "new_inventory": self.inventory,
            "old_average": old_average,
            "new_average": self.average,
            "closed_quantity": closed,
            "closed_pnl_ticks": closed_pnl,
        }
        return {"fee": fee, "rebate": rebate}

    def snapshot(self, mark: Fraction, timestamp_ns: int) -> dict[str, Any]:
        enforce_rational_cap(mark)
        unrealized = (
            self.inventory * (mark - self.average)
            if self.average is not None
            else Fraction(0)
        )
        gross = self.realized + unrealized
        require(
            gross == self.trade_cash_ticks + self.inventory * mark,
            "independent ledger exact identity failed",
        )
        exposure = abs(self.inventory) * mark * self.config.tick
        self.peak_exposure = max(self.peak_exposure, exposure)
        tick = self.config.tick
        result = {
            "timestamp_ns": timestamp_ns,
            "currency": self.config.currency,
            "tick_value": tick,
            "mark_ticks": mark,
            "inventory": self.inventory,
            "average_cost_ticks": self.average,
            "trade_cash_ticks": self.trade_cash_ticks,
            "trade_cash": self.trade_cash_ticks * tick,
            "cash": self.trade_cash_ticks * tick - self.fees + self.rebates,
            "realized_pnl_ticks": self.realized,
            "unrealized_pnl_ticks": unrealized,
            "gross_pnl_ticks": gross,
            "realized_pnl": self.realized * tick,
            "unrealized_pnl": unrealized * tick,
            "gross_pnl": gross * tick,
            "fees": self.fees,
            "rebates": self.rebates,
            "net_pnl": gross * tick - self.fees + self.rebates,
            "turnover_ticks": self.turnover_ticks,
            "turnover": self.turnover_ticks * tick,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "fill_count": self.fill_count,
            "current_gross_exposure": exposure,
            "peak_gross_exposure": self.peak_exposure,
        }
        for value in result.values():
            if isinstance(value, Fraction):
                self.observe_depth(value)
        return result


def exact_token(value: Any) -> Any:
    if isinstance(value, Fraction):
        return {
            "numerator_hex": hex(value.numerator),
            "denominator_hex": hex(value.denominator),
        }
    if type(value) is float:
        return {"float64_bits": struct.pack(">d", value).hex()}
    if isinstance(value, dict):
        return {name: exact_token(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [exact_token(item) for item in value]
    return value


def projected_token(value: Any) -> Any:
    return (
        exact_token(render_fraction(value))
        if isinstance(value, Fraction)
        else exact_token(value)
    )


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def source_inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted((root / "src/lobmm").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def make_plan(input_root: Path = ROOT) -> dict[str, Any]:
    manifest_path = RESOURCE + "/manifest.json"
    resource_plan_path = RESOURCE + "/plan.json"
    original, resource_plan = (
        read(input_root / manifest_path),
        read(input_root / resource_plan_path),
    )
    require(
        all(
            digest(input_root / path) == expected
            for path, expected in ORIGINAL_METADATA.items()
        ),
        "inherited PR14 metadata is not the pinned original",
    )
    require(
        original["status"] == "complete"
        and resource_plan["source_revision"] == SOURCE_REVISION,
        "original provenance is not the frozen resource study",
    )
    require(
        original["plan_sha256"] == digest(input_root / resource_plan_path),
        "original resource plan hash differs",
    )
    source = resource_plan["source_sha256"]
    require(
        len(source) == 38 and source_inventory(input_root) == source,
        "application bytes changed",
    )
    selected = []
    for cohort in COHORTS:
        engine = f"{RESOURCE}/attempts/{cohort}/engine/resource-study/"
        paths = {
            "fills": engine + "fills.parquet",
            "pnl": engine + "pnl.parquet",
            "inventory": engine + "inventory.parquet",
            "config": engine + "run_config.yaml",
            "input": f"{RESOURCE}/attempts/{cohort}/input/events.parquet",
            "resource_result": f"{RESOURCE}/attempts/{cohort}/result.json",
        }
        files = {}
        for name, path in paths.items():
            relative = str(Path(path).relative_to(RESOURCE))
            original_sha = original["original_file_stats"][relative]["sha256"]
            require(
                digest(input_root / path) == original_sha,
                "original cohort bytes changed",
            )
            files[name] = {"path": path, "sha256": original_sha}
        metadata = read(input_root / paths["resource_result"])
        selected.append(
            {
                "id": cohort,
                "files": files,
                "event_stream_sha256": metadata["input"]["event_stream_sha256"],
            }
        )
    return {
        "version": 1,
        "source_revision": SOURCE_REVISION,
        "artifact_base_commit": ARTIFACT_BASE,
        "source_sha256": source,
        "cohorts": selected,
        "original_metadata": {
            name: digest(input_root / name)
            for name in (manifest_path, resource_plan_path)
        },
        "pins": {name: digest(ROOT / name) for name in (TOOL, TEST, DESIGN)},
        "limits": {
            "pnl_rows": PNL_ROW_LIMIT,
            "fills": FILL_LIMIT,
            "rational_bits": RATIONAL_BIT_LIMIT,
            "worker_seconds": WORKER_SECONDS,
            "stage_seconds": STAGE_SECONDS,
            "evidence_bytes": EVIDENCE_LIMIT,
        },
        "presentation_assumption": "Fresh default Decimal precision 28, HALF_EVEN for repeating views; terminating views exact; conditional recorded half-tick marks",
    }


def validate_plan(
    plan: dict[str, Any], input_root: Path, harness_root: Path = ROOT
) -> None:
    require(
        plan["artifact_base_commit"] == ARTIFACT_BASE
        and plan["original_metadata"] == ORIGINAL_METADATA,
        "original PR14 metadata identity or keyset differs",
    )
    require(
        plan["source_revision"] == SOURCE_REVISION
        and [row["id"] for row in plan["cohorts"]] == list(COHORTS),
        "cohort/source selection drift",
    )
    require(
        plan["limits"]
        == {
            "pnl_rows": PNL_ROW_LIMIT,
            "fills": FILL_LIMIT,
            "rational_bits": RATIONAL_BIT_LIMIT,
            "worker_seconds": WORKER_SECONDS,
            "stage_seconds": STAGE_SECONDS,
            "evidence_bytes": EVIDENCE_LIMIT,
        },
        "audit cap drift",
    )
    require(set(plan["pins"]) == {TOOL, TEST, DESIGN}, "harness pin inventory drift")
    for relative, expected in plan["pins"].items():
        require(
            digest(harness_root / relative) == expected, "frozen auditor bytes changed"
        )
    require(
        len(plan["source_sha256"]) == 38
        and source_inventory(input_root) == plan["source_sha256"],
        "source inventory differs",
    )
    for relative, expected in plan["original_metadata"].items():
        require(
            digest(input_root / relative) == expected,
            "original provenance manifest changed",
        )
    original = read(input_root / RESOURCE / "manifest.json")
    resource_plan = read(input_root / RESOURCE / "plan.json")
    require(
        original["status"] == "complete"
        and original["plan_sha256"] == digest(input_root / RESOURCE / "plan.json")
        and resource_plan["source_revision"] == SOURCE_REVISION
        and resource_plan["source_sha256"] == plan["source_sha256"],
        "original source/plan chain differs",
    )
    for cohort in plan["cohorts"]:
        engine = f"{RESOURCE}/attempts/{cohort['id']}/engine/resource-study/"
        expected_paths = {
            "fills": engine + "fills.parquet",
            "pnl": engine + "pnl.parquet",
            "inventory": engine + "inventory.parquet",
            "config": engine + "run_config.yaml",
            "input": f"{RESOURCE}/attempts/{cohort['id']}/input/events.parquet",
            "resource_result": f"{RESOURCE}/attempts/{cohort['id']}/result.json",
        }
        require(
            {name: item["path"] for name, item in cohort["files"].items()}
            == expected_paths,
            "cohort input selection differs",
        )
        for saved in cohort["files"].values():
            relative = str(Path(saved["path"]).relative_to(RESOURCE))
            require(
                original["original_file_stats"][relative]["sha256"] == saved["sha256"]
                and digest(input_root / saved["path"]) == saved["sha256"],
                "selected immutable input changed",
            )
        require(
            cohort["event_stream_sha256"]
            == read(input_root / expected_paths["resource_result"])["input"][
                "event_stream_sha256"
            ],
            "input event-stream metadata differs",
        )


def evidence_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


class Evidence:
    def __init__(self, path: Path, study_root: Path) -> None:
        self.stream = path.open("x")
        self.root = study_root

    def append(self, record: Any) -> None:
        line = (
            json.dumps(
                exact_token(record),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        require(
            evidence_size(self.root) + len(line.encode()) <= EVIDENCE_LIMIT,
            "generated evidence exceeded 128 MiB",
        )
        self.stream.write(line)
        self.stream.flush()

    def close(self) -> None:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()


def rows(path: Path) -> Any:
    import pyarrow.parquet as pq

    for batch in pq.ParquetFile(path).iter_batches(batch_size=2048):
        yield from batch.to_pylist()


def audit_cohort(
    cohort: dict[str, Any],
    input_root: Path,
    output: Path,
    study_root: Path,
) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    started = time.monotonic_ns()
    paths = {name: input_root / item["path"] for name, item in cohort["files"].items()}
    config, literals = load_economics(paths["config"])
    pnl_file, fill_file, inventory_file = (
        pq.ParquetFile(paths[name]) for name in ("pnl", "fills", "inventory")
    )
    require(
        0 < pnl_file.metadata.num_rows <= PNL_ROW_LIMIT
        and fill_file.metadata.num_rows <= FILL_LIMIT,
        "row or fill cap",
    )
    require(
        pnl_file.schema_arrow.names == list(SNAPSHOT_FIELDS),
        "Snapshot field schema differs",
    )
    require(
        inventory_file.schema_arrow.names == list(INVENTORY_FIELDS)
        and inventory_file.metadata.num_rows == pnl_file.metadata.num_rows,
        "inventory alignment schema differs",
    )
    for name in SNAPSHOT_FIELDS:
        dtype = pnl_file.schema_arrow.field(name).type
        require(
            pa.types.is_int64(dtype)
            if name in INTEGER_FIELDS
            else pa.types.is_string(dtype) or pa.types.is_large_string(dtype)
            if name == "currency"
            else pa.types.is_float64(dtype),
            "mandatory Arrow Snapshot dtype differs: " + name,
        )
    for name in INVENTORY_FIELDS:
        dtype = inventory_file.schema_arrow.field(name).type
        require(
            pa.types.is_int64(dtype)
            if name in INTEGER_FIELDS
            else pa.types.is_float64(dtype),
            "mandatory inventory Arrow dtype differs: " + name,
        )
    if fill_file.metadata.num_rows:
        for name in (
            "side",
            "quantity",
            "price_ticks",
            "exchange_fill_timestamp_ns",
            "decision_timestamp_ns",
            "send_timestamp_ns",
            "exchange_arrival_timestamp_ns",
            "strategy_notification_timestamp_ns",
        ):
            require(
                pa.types.is_int64(fill_file.schema_arrow.field(name).type),
                "fill Arrow integer dtype differs",
            )
        for name in (
            "fill_id",
            "order_id",
            "client_order_id",
            "strategy_id",
            "liquidity_role",
        ):
            dtype = fill_file.schema_arrow.field(name).type
            require(
                pa.types.is_string(dtype) or pa.types.is_large_string(dtype),
                "fill Arrow string dtype differs",
            )
        for name in ("fee", "rebate"):
            require(
                pa.types.is_float64(fill_file.schema_arrow.field(name).type),
                "fill cost Arrow dtype differs",
            )
    fills = list(rows(paths["fills"]))
    validate_fills(fills)
    ledger = WeightedAverageLedger(config)
    coverage: Counter[str] = Counter()
    repeating: Counter[str] = Counter()
    projected_cells: Counter[str] = Counter()
    previous_count, previous_time = None, -1
    mismatch_count = audited_rows = 0
    sink = Evidence
    row_log, mismatch_log, fill_log = (
        sink(output / name, study_root)
        for name in ("rows.jsonl", "mismatches.jsonl", "fills.jsonl")
    )
    result = {
        "cohort": cohort["id"],
        "status": "started",
        "config_literals": literals,
        "source_fields": list(SNAPSHOT_FIELDS),
        "row_count": 0,
        "fill_count": len(fills),
        "mismatch_count": 0,
    }
    write(output / "result.json", result)
    try:
        for index, (pnl, inventory) in enumerate(
            zip(rows(paths["pnl"]), rows(paths["inventory"]), strict=True)
        ):
            require(
                time.monotonic_ns() - started <= WORKER_SECONDS * 10**9,
                "cohort wall-time cap",
            )
            timestamp, count = pnl["timestamp_ns"], pnl["fill_count"]
            validate_prefix_step(previous_count, count, len(fills), timestamp, fills)
            require(timestamp >= previous_time, "snapshot timestamps moved backward")
            if count > ledger.fill_count:
                fill = fills[count - 1]
                costs = ledger.apply_fill(fill)
                cost_mismatches = []
                for name, exact in costs.items():
                    projected_cells[
                        "fill_cost_finite_decimal"
                        if terminates(exact)
                        else "fill_cost_repeating"
                    ] += 1
                    expected = projected_token(exact)
                    actual = exact_token(fill[name])
                    if expected != actual:
                        cost_mismatches.append(
                            {
                                "field": name,
                                "expected_exact": exact_token(exact),
                                "expected": expected,
                                "actual": actual,
                            }
                        )
                fill_log.append(
                    {
                        "prefix": count,
                        "fill_id": fill["fill_id"],
                        "transition": ledger.last_transition,
                        "trade_cash_ticks": ledger.trade_cash_ticks,
                        "average": ledger.average,
                        "sum_closed_pnl_ticks": ledger.realized,
                        "fees": ledger.fees,
                        "rebates": ledger.rebates,
                        "costs": costs,
                        "cost_mismatches": cost_mismatches,
                    }
                )
                mismatch_count += len(cost_mismatches)
                if cost_mismatches:
                    mismatch_log.append(
                        {
                            "kind": "fill_cost_projection",
                            "prefix": count,
                            "mismatches": cost_mismatches,
                        }
                    )
            mark = admit_mark(pnl["mark_ticks"], ledger.inventory)
            expected = ledger.snapshot(mark, timestamp)
            expected_tokens, actual_tokens, mismatches = [], [], []
            for name in SNAPSHOT_FIELDS:
                actual = pnl[name]
                require(
                    actual is not None
                    or (name == "average_cost_ticks" and ledger.inventory == 0),
                    "unexpected null Snapshot field: " + name,
                )
                if name == "average_cost_ticks":
                    require(
                        (actual is None) == (ledger.inventory == 0),
                        "average cost nullability disagrees with flat position",
                    )
                if name in INTEGER_FIELDS:
                    require(
                        type(actual) is int,
                        "saved integer field lost its original type: " + name,
                    )
                elif name != "currency" and actual is not None:
                    require(
                        type(actual) is float and math.isfinite(actual),
                        "saved snapshot numeric value is nonfinite or not Float64: "
                        + name,
                    )
                want, got = projected_token(expected[name]), exact_token(actual)
                expected_tokens.append([name, want])
                actual_tokens.append([name, got])
                if isinstance(expected[name], Fraction) and not terminates(
                    expected[name]
                ):
                    repeating[name] += 1
                    projected_cells["snapshot_repeating"] += 1
                elif isinstance(expected[name], Fraction):
                    projected_cells["snapshot_finite_decimal"] += 1
                elif expected[name] is None:
                    projected_cells["snapshot_null_average"] += 1
                if want != got:
                    mismatches.append(
                        {
                            "field": name,
                            "expected_exact": exact_token(expected[name]),
                            "expected": want,
                            "actual": got,
                        }
                    )
            expected_inventory, actual_inventory = [], []
            for name in INVENTORY_FIELDS:
                actual = inventory[name]
                require(
                    type(actual) is int
                    if name in INTEGER_FIELDS
                    else type(actual) is float and math.isfinite(actual),
                    "invalid saved inventory projection type",
                )
                want, got = projected_token(expected[name]), exact_token(actual)
                expected_inventory.append([name, want])
                if isinstance(expected[name], Fraction):
                    projected_cells[
                        "inventory_finite_decimal"
                        if terminates(expected[name])
                        else "inventory_repeating"
                    ] += 1
                actual_inventory.append([name, got])
                if want != got:
                    mismatches.append(
                        {
                            "field": "inventory." + name,
                            "expected_exact": exact_token(expected[name]),
                            "expected": want,
                            "actual": got,
                        }
                    )
            row_log.append(
                {
                    "row": index,
                    "prefix": count,
                    "timestamp_ns": timestamp,
                    "expected_sha256": object_hash(
                        [expected_tokens, expected_inventory]
                    ),
                    "observed_sha256": object_hash([actual_tokens, actual_inventory]),
                    "mismatch_count": len(mismatches),
                }
            )
            if mismatches:
                mismatch_log.append(
                    {
                        "kind": "snapshot_projection",
                        "row": index,
                        "prefix": count,
                        "timestamp_ns": timestamp,
                        "mismatches": mismatches,
                    }
                )
            mismatch_count += len(mismatches)
            audited_rows += 1
            coverage[
                "flat_snapshot"
                if ledger.inventory == 0
                else "long_snapshot"
                if ledger.inventory > 0
                else "short_snapshot"
            ] += 1
            previous_count, previous_time = count, timestamp
            if audited_rows % 256 == 0:
                result.update(
                    row_count=audited_rows,
                    mismatch_count=mismatch_count,
                    completed_prefix=count,
                )
                write(output / "result.json", result)
        require(
            previous_count == len(fills),
            "retained snapshots do not cover every fill prefix",
        )
        result.update(
            status="complete",
            row_count=audited_rows,
            mismatch_count=mismatch_count,
            ledger_coverage=dict(ledger.coverage),
            snapshot_coverage=dict(coverage),
            repeating_field_rows=dict(repeating),
            projected_cell_coverage=dict(projected_cells),
            max_numerator_bits=ledger.max_numerator_bits,
            max_denominator_bits=ledger.max_denominator_bits,
            all_projections_match=mismatch_count == 0,
            assumption="Recorded midpoint marks and fresh default 28-digit HALF_EVEN presentation; no retrospective context or causal-mark proof",
        )
        return result
    except BaseException as exc:
        result.update(
            status="stopped",
            row_count=audited_rows,
            mismatch_count=mismatch_count,
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
        )
        raise
    finally:
        row_log.close()
        mismatch_log.close()
        fill_log.close()
        write(output / "result.json", result)


def preflight() -> dict[str, Any]:
    config = EconomicConfig(
        Fraction(1), "USD", Fraction(0), Fraction(0), Fraction(0), Fraction(0)
    )
    ledger = WeightedAverageLedger(config)
    for index, (side, quantity, price) in enumerate(
        ((1, 2, 100), (1, 1, 103), (-1, 1, 106))
    ):
        ledger.apply_fill(
            {
                "side": side,
                "quantity": quantity,
                "price_ticks": price,
                "liquidity_role": "maker",
                "fill_id": str(index),
                "exchange_fill_timestamp_ns": index,
            }
        )
    observed = ledger.snapshot(Fraction(104), 3)
    checks = {
        "long_partial_close": observed["realized_pnl_ticks"] == 5
        and observed["unrealized_pnl_ticks"] == 6,
        "ties_even": round_significant(Fraction(125, 100), 2) == Fraction(12, 10)
        and round_significant(Fraction(135, 100), 2) == Fraction(14, 10),
        "repeating": round_significant(Fraction(1, 3))
        == Fraction(3333333333333333333333333333, 10**28),
        "positive_zero": struct.pack(">d", render_fraction(Fraction(0))).hex()
        == "0000000000000000",
        "flat_none": WeightedAverageLedger(config).snapshot(Fraction(0), 0)[
            "average_cost_ticks"
        ]
        is None,
    }
    require(all(checks.values()), "fixed mathematical preflight failed")
    return checks


def worker(plan_path: Path, input_root: Path, cohort_id: str, output: Path) -> int:
    require(not output.exists(), "refusing an existing cohort output")
    output.mkdir(parents=True)
    plan = read(plan_path)
    record: dict[str, Any] = {
        "status": "started",
        "cohort": cohort_id,
        "plan_sha256": digest(plan_path),
        "no_application_imports": not any(
            name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
        ),
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "dependencies": {
                name: importlib.metadata.version(name) for name in ("pyarrow", "PyYAML")
            },
        },
    }
    write(output / "worker.json", record)
    try:
        validate_plan(plan, input_root)
        require(record["no_application_imports"], "unexpected application import")
        selected = next(row for row in plan["cohorts"] if row["id"] == cohort_id)
        result = audit_cohort(selected, input_root, output, output.parent.parent)
        validate_plan(plan, input_root)
        record.update(status="complete", result=result)
        returncode = 0
    except BaseException as exc:
        record.update(
            status="stopped", stop_reason=repr(exc), traceback=traceback.format_exc()
        )
        returncode = 2
    finally:
        record["no_application_imports_after"] = not any(
            name == "lobmm" or name.startswith("lobmm.") for name in sys.modules
        )
        write(output / "worker.json", record)
    return returncode


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path != root / "manifest.json"
        and "__pycache__" not in path.parts
    }


def validate_prior_link(
    previous: dict[str, Any], old_plan: dict[str, Any], new_plan: dict[str, Any]
) -> None:
    require(
        type(previous["repair_index"]) is int
        and previous["repair_index"] == 0
        and previous["prior_evidence"] is None
        and type(previous["prior_elapsed_ns"]) is int
        and previous["prior_elapsed_ns"] == 0
        and previous["cumulative_elapsed_ns"] == previous["elapsed_ns"],
        "a repair must link the original initial-only attempt",
    )
    require(
        {key: value for key, value in old_plan.items() if key != "pins"}
        == {key: value for key, value in new_plan.items() if key != "pins"},
        "repair changed frozen cohorts, source, inputs or criteria",
    )
    require(
        set(old_plan["pins"]) == set(new_plan["pins"]) == {TOOL, TEST, DESIGN}
        and old_plan["pins"][DESIGN] == new_plan["pins"][DESIGN],
        "only auditor tool/test bytes may differ in an approved repair",
    )


def run(
    plan_path: Path, input_root: Path, output: Path, prior_evidence: Path | None = None
) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        plan = read(plan_path)
        validate_plan(plan, input_root)
        require(not output.exists(), "refusing to overwrite a prior audit")
    except BaseException as exc:
        raise ValueError(
            "admission/preflight failure before output creation; no cohort worker was launched"
        ) from exc
    output.mkdir(parents=True)
    shutil.copy2(plan_path, output / "plan.json")
    for relative in plan["pins"]:
        target = output / "harness" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    prior_ns = 0
    prior = None
    if prior_evidence is not None:
        previous = read(prior_evidence / "manifest.json")
        old_plan = read(prior_evidence / "plan.json")
        validate_prior_link(previous, old_plan, plan)
        require(
            type(previous["elapsed_ns"]) is int
            and previous["elapsed_ns"] >= 0
            and type(previous["stage_started_ns"]) is int
            and type(previous["stage_finished_ns"]) is int
            and previous["elapsed_ns"]
            == previous["stage_finished_ns"] - previous["stage_started_ns"],
            "prior elapsed budget must be a coherent nonnegative builtin integer",
        )
        require(
            previous["files"] == inventory(prior_evidence),
            "original prior-attempt evidence changed",
        )
        require(
            previous.get("repair_index", 0) == 0
            and previous["status"] in ("stopped", "complete"),
            "only one independently authorized auditor repair may follow the initial pass",
        )
        if previous["status"] == "complete":
            require(
                not read(prior_evidence / "report.json")["all_projections_match"],
                "a passing audit does not authorize repair/reanalysis",
            )
        prior_ns = previous["elapsed_ns"]
        prior = {
            "manifest_sha256": digest(prior_evidence / "manifest.json"),
            "path": str(prior_evidence),
            "elapsed_ns": prior_ns,
        }
        shutil.copy2(prior_evidence / "manifest.json", output / "prior-manifest.json")
        shutil.copy2(prior_evidence / "plan.json", output / "prior-plan.json")
    require(prior_ns < STAGE_SECONDS * 10**9, "cumulative audit budget exhausted")
    manifest: dict[str, Any] = {
        "status": "started",
        "plan_sha256": digest(output / "plan.json"),
        "attempts": [],
        "prior_evidence": prior,
        "prior_elapsed_ns": prior_ns,
        "repair_index": int(prior_evidence is not None),
    }
    write(output / "manifest.json", manifest)
    manifest["stage_started_ns"] = started
    try:
        write(output / "preflight.json", preflight())
        for selected in plan["cohorts"]:
            remaining = STAGE_SECONDS - (prior_ns + time.monotonic_ns() - started) / 1e9
            require(remaining > 0, "total 600-second audit cap")
            directory = output / "cohorts" / selected["id"]
            directory.parent.mkdir(exist_ok=True)
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
                "--cohort",
                selected["id"],
                "--output",
                str(directory),
            ]
            process = {
                "cohort": selected["id"],
                "command": command,
                "started_ns": time.monotonic_ns(),
                "cap_seconds": min(WORKER_SECONDS, remaining),
            }
            manifest["attempts"].append(process)
            write(output / "manifest.json", manifest)
            try:
                with (output / f"{selected['id']}.log").open("x") as stream:
                    completed = subprocess.run(
                        command,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=min(WORKER_SECONDS, remaining),
                        check=False,
                    )
                process.update(
                    returncode=completed.returncode,
                    status="complete" if completed.returncode == 0 else "stopped",
                )
            except subprocess.TimeoutExpired:
                process.update(
                    returncode=None,
                    status="timeout",
                    stop_reason="offline cohort wall-time cap",
                    termination="subprocess.run killed and waited for the timed-out child",
                )
                raise
            finally:
                process["finished_ns"] = time.monotonic_ns()
                process["elapsed_ns"] = process["finished_ns"] - process["started_ns"]
                write(output / "manifest.json", manifest)
            require(completed.returncode == 0, "cohort stopped; no automatic retry")
            require(evidence_size(output) <= EVIDENCE_LIMIT, "generated evidence cap")
        manifest["cohort_pass_finished_ns"] = time.monotonic_ns()
        require(
            prior_ns + manifest["cohort_pass_finished_ns"] - started
            <= STAGE_SECONDS * 10**9,
            "cumulative total audit cap",
        )
        results = [read(output / "cohorts" / name / "result.json") for name in COHORTS]
        report = {
            "complete": True,
            "all_projections_match": all(
                row["all_projections_match"] for row in results
            ),
            "cohorts": results,
            "limits": plan["limits"],
            "scope": "Conditional retrospective accounting/projection correctness on exposed synthetic fills and recorded half-tick marks; no economic, context, causality or fill-realism claim",
        }
        write(output / "report.json", report)
        manifest.update(status="complete", files=inventory(output))
        write(output / "manifest.json", manifest)
        return report
    except BaseException as exc:
        manifest.update(
            status="stopped",
            stop_reason=repr(exc),
            traceback=traceback.format_exc(),
            files=inventory(output),
        )
        write(output / "manifest.json", manifest)
        raise
    finally:
        manifest["stage_finished_ns"] = time.monotonic_ns()
        manifest["elapsed_ns"] = manifest["stage_finished_ns"] - started
        manifest["cumulative_elapsed_ns"] = prior_ns + manifest["elapsed_ns"]
        over_budget = manifest["cumulative_elapsed_ns"] > STAGE_SECONDS * 10**9
        if over_budget:
            manifest["status"] = "stopped"
            manifest.setdefault(
                "stop_reason",
                "cumulative audit budget exhausted during evidence finalization",
            )
        write(output / "manifest.json", manifest)
        if over_budget and sys.exc_info()[0] is None:
            raise ValueError(manifest["stop_reason"])


def verify_evidence(output: Path, input_root: Path) -> dict[str, Any]:
    manifest, plan = read(output / "manifest.json"), read(output / "plan.json")
    require(
        manifest["status"] == "complete" and manifest["files"] == inventory(output),
        "audit evidence incomplete or changed",
    )
    require(
        manifest["plan_sha256"] == digest(output / "plan.json"), "plan bytes differ"
    )
    validate_plan(plan, input_root)
    validate_plan(plan, input_root, output / "harness")
    require(
        [row["cohort"] for row in manifest["attempts"]] == list(COHORTS),
        "attempt order differs",
    )
    require(
        manifest["stage_finished_ns"] - manifest["stage_started_ns"]
        <= STAGE_SECONDS * 10**9,
        "total cap differs",
    )
    require(
        manifest["cumulative_elapsed_ns"]
        == manifest["prior_elapsed_ns"] + manifest["elapsed_ns"]
        and manifest["cumulative_elapsed_ns"] <= STAGE_SECONDS * 10**9,
        "cumulative cap arithmetic differs",
    )
    require(
        manifest["elapsed_ns"]
        == manifest["stage_finished_ns"] - manifest["stage_started_ns"],
        "terminal stage timestamp differs",
    )
    if manifest["prior_evidence"] is not None:
        prior = read(output / "prior-manifest.json")
        validate_prior_link(prior, read(output / "prior-plan.json"), plan)
        require(
            digest(output / "prior-manifest.json")
            == manifest["prior_evidence"]["manifest_sha256"]
            and prior["elapsed_ns"] == manifest["prior_elapsed_ns"]
            and prior.get("repair_index", 0) == 0,
            "cumulative prior-attempt link differs",
        )
        require(
            digest(output / "prior-plan.json") == prior["files"]["plan.json"],
            "linked original plan hash differs",
        )
    require(
        evidence_size(output) <= EVIDENCE_LIMIT, "saved generated evidence exceeds cap"
    )
    results = []
    for cohort, attempt in zip(COHORTS, manifest["attempts"], strict=True):
        require(
            attempt["returncode"] == 0
            and 0
            < attempt["finished_ns"] - attempt["started_ns"]
            <= WORKER_SECONDS * 10**9,
            "worker cap or status differs",
        )
        record = read(output / "cohorts" / cohort / "worker.json")
        require(
            record["status"] == "complete"
            and record["no_application_imports"]
            and record["no_application_imports_after"],
            "worker/application isolation differs",
        )
        result = read(output / "cohorts" / cohort / "result.json")
        evidence = [
            json.loads(line)
            for line in (output / "cohorts" / cohort / "rows.jsonl")
            .read_text()
            .splitlines()
        ]
        require(
            len(evidence) == result["row_count"]
            and [row["row"] for row in evidence] == list(range(result["row_count"])),
            "row evidence prefix differs",
        )
        require(
            all(
                (row["expected_sha256"] == row["observed_sha256"])
                == (row["mismatch_count"] == 0)
                for row in evidence
            ),
            "row hash/mismatch ledger differs",
        )
        mismatch_records = [
            json.loads(line)
            for line in (output / "cohorts" / cohort / "mismatches.jsonl")
            .read_text()
            .splitlines()
        ]
        fill_records = [
            json.loads(line)
            for line in (output / "cohorts" / cohort / "fills.jsonl")
            .read_text()
            .splitlines()
        ]
        require(
            len(fill_records) == result["fill_count"]
            and [row["prefix"] for row in fill_records]
            == list(range(1, result["fill_count"] + 1)),
            "fill-prefix evidence differs",
        )
        require(
            sum(len(row["mismatches"]) for row in mismatch_records)
            == result["mismatch_count"],
            "saved mismatch total differs",
        )
        require(
            result == record["result"]
            and result["all_projections_match"] == (result["mismatch_count"] == 0),
            "saved worker result arithmetic differs",
        )
        results.append(result)
    report = read(output / "report.json")
    require(
        report["cohorts"] == results
        and report["all_projections_match"]
        == all(row["all_projections_match"] for row in results),
        "saved report counters differ",
    )
    controls = read(output / "preflight.json")
    require(
        set(controls)
        == {
            "long_partial_close",
            "ties_even",
            "repeating",
            "positive_zero",
            "flat_none",
        }
        and all(value is True for value in controls.values()),
        "saved control status differs",
    )

    return {
        "verified": True,
        "integrity_and_counter_verification_only": True,
        "report": report,
        "note": "No ledger arithmetic is rerun in this mode. It verifies saved record hashes/counters and immutable input identities; --run is the explicit offline arithmetic reproduction",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-plan", type=Path)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--verify-evidence", type=Path)
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--input-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cohort", choices=COHORTS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--prior-evidence",
        type=Path,
        help="original retained attempt for one independently authorized auditor-only repair; shares the 600-second budget",
    )
    args = parser.parse_args()
    try:
        if args.write_plan:
            require(not args.write_plan.exists(), "refusing to overwrite a ledger plan")
            write(args.write_plan, make_plan(args.input_root.resolve()))
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
                and args.cohort is not None,
                "missing worker arguments",
            )
            return worker(
                args.plan.resolve(),
                args.input_root.resolve(),
                args.cohort,
                args.output.resolve(),
            )
        if args.verify_evidence:
            result = verify_evidence(
                args.verify_evidence.resolve(), args.input_root.resolve()
            )
        else:
            require(
                args.plan is not None and args.output is not None,
                "--run needs frozen plan and fresh output",
            )
            result = run(
                args.plan.resolve(),
                args.input_root.resolve(),
                args.output.resolve(),
                None if args.prior_evidence is None else args.prior_evidence.resolve(),
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result.get("all_projections_match", True) else 1
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
