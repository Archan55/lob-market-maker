"""Independent mathematical controls for the offline artifact auditor.

These fixed controls use no application classes, accounting helpers or retained
cohort outcomes. Importing the test file does not import or execute the auditor.
"""

from __future__ import annotations

import copy
import importlib.util
import struct
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module")
def ledger_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools/reconcile_artifact_ledger.py"
    spec = importlib.util.spec_from_file_location("artifact_ledger_unit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(tool: ModuleType, *, tick: Fraction = Fraction(1)) -> Any:
    return tool.EconomicConfig(
        tick=tick,
        currency="USD",
        maker_fee=Fraction(0),
        maker_rebate=Fraction(0),
        taker_fee=Fraction(0),
        proportional=Fraction(0),
    )


def _fill(
    identifier: str,
    side: int,
    quantity: int,
    price: int,
    timestamp: int,
    role: str = "maker",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "fill_id": identifier,
        "side": side,
        "quantity": quantity,
        "price_ticks": price,
        "liquidity_role": role,
        "exchange_fill_timestamp_ns": timestamp,
        **extra,
    }


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def test_long_weighted_average_partial_close_and_all_exact_snapshot_fields(
    ledger_tool: ModuleType,
) -> None:
    ledger = ledger_tool.WeightedAverageLedger(
        _config(ledger_tool, tick=Fraction(1, 4))
    )
    initial = ledger.snapshot(Fraction(0), 0)
    assert initial["average_cost_ticks"] is None
    assert initial["net_pnl"] == 0
    assert initial["peak_gross_exposure"] == 0
    ledger.apply_fill(_fill("L1", 1, 2, 100, 1))
    ledger.snapshot(Fraction(100), 1)
    ledger.apply_fill(_fill("L2", 1, 1, 103, 2))
    ledger.snapshot(Fraction(104), 2)
    ledger.apply_fill(_fill("L3", -1, 1, 106, 3))
    actual = ledger.snapshot(Fraction(104), 3)
    # Acquisition is (2 * 100 + 103) / 3 = 101. One sale at 106 realizes
    # 5 ticks; the two remaining units marked at 104 contribute 6 ticks.
    expected = {
        "timestamp_ns": 3,
        "currency": "USD",
        "tick_value": Fraction(1, 4),
        "mark_ticks": Fraction(104),
        "inventory": 2,
        "average_cost_ticks": Fraction(101),
        "trade_cash_ticks": -197,
        "trade_cash": Fraction(-197, 4),
        "cash": Fraction(-197, 4),
        "realized_pnl_ticks": Fraction(5),
        "unrealized_pnl_ticks": Fraction(6),
        "gross_pnl_ticks": Fraction(11),
        "realized_pnl": Fraction(5, 4),
        "unrealized_pnl": Fraction(3, 2),
        "gross_pnl": Fraction(11, 4),
        "fees": Fraction(0),
        "rebates": Fraction(0),
        "net_pnl": Fraction(11, 4),
        "turnover_ticks": 409,
        "turnover": Fraction(409, 4),
        "buy_volume": 3,
        "sell_volume": 1,
        "fill_count": 3,
        "current_gross_exposure": Fraction(52),
        "peak_gross_exposure": Fraction(78),
    }
    assert actual == expected
    assert {key: type(value) for key, value in actual.items()} == {
        key: type(value) for key, value in expected.items()
    }
    ledger.apply_fill(_fill("L4", -1, 2, 99, 4))
    flat = ledger.snapshot(Fraction(0), 4)
    assert flat["inventory"] == 0
    assert flat["average_cost_ticks"] is None
    assert flat["trade_cash_ticks"] == flat["realized_pnl_ticks"] == 1
    assert flat["unrealized_pnl_ticks"] == 0
    assert flat["net_pnl"] == Fraction(1, 4)
    assert flat["current_gross_exposure"] == 0
    assert flat["peak_gross_exposure"] == 78


def test_short_partial_close_then_reversal_uses_new_price_for_residual_position(
    ledger_tool: ModuleType,
) -> None:
    ledger = ledger_tool.WeightedAverageLedger(
        _config(ledger_tool, tick=Fraction(1, 10))
    )
    ledger.apply_fill(_fill("S1", -1, 3, 12, 0))
    first = ledger.snapshot(Fraction(12), 0)
    assert first["average_cost_ticks"] == 12
    assert first["inventory"] == -3
    ledger.apply_fill(_fill("S2", 1, 1, 9, 1))
    partial = ledger.snapshot(Fraction(9), 1)
    assert partial["inventory"] == -2
    assert partial["average_cost_ticks"] == 12
    assert partial["realized_pnl_ticks"] == 3
    ledger.apply_fill(_fill("S3", 1, 4, 15, 2))
    reversed_position = ledger.snapshot(Fraction(14), 2)
    # Close the old short 2 at 15 for -6 ticks, then hold long 2 acquired at 15.
    assert reversed_position["inventory"] == 2
    assert reversed_position["average_cost_ticks"] == 15
    assert reversed_position["trade_cash_ticks"] == -33
    assert reversed_position["realized_pnl_ticks"] == -3
    assert reversed_position["unrealized_pnl_ticks"] == -2
    assert reversed_position["gross_pnl_ticks"] == -5
    assert reversed_position["net_pnl"] == Fraction(-1, 2)
    assert reversed_position["turnover_ticks"] == 105
    assert reversed_position["buy_volume"] == 5
    assert reversed_position["sell_volume"] == 3
    assert reversed_position["fill_count"] == 3
    assert reversed_position["current_gross_exposure"] == Fraction(14, 5)
    assert reversed_position["peak_gross_exposure"] == Fraction(18, 5)


def test_repeating_average_partial_close_preserves_exact_rational_components(
    ledger_tool: ModuleType,
) -> None:
    ledger = ledger_tool.WeightedAverageLedger(_config(ledger_tool))
    ledger.apply_fill(_fill("R1", 1, 2, 100, 1))
    ledger.apply_fill(_fill("R2", 1, 1, 101, 2))
    ledger.apply_fill(_fill("R3", -1, 1, 102, 3))
    snapshot = ledger.snapshot(Fraction(101), 3)
    # Acquisition is 301 / 3. The sale realizes (306 - 301) / 3;
    # marking the remaining two units realizes no further cash flow.
    assert snapshot["average_cost_ticks"] == Fraction(301, 3)
    assert snapshot["realized_pnl_ticks"] == Fraction(5, 3)
    assert snapshot["unrealized_pnl_ticks"] == Fraction(4, 3)
    assert snapshot["gross_pnl_ticks"] == snapshot["net_pnl"] == 3
    assert snapshot["trade_cash_ticks"] == -199
    assert snapshot["inventory"] == 2
    assert snapshot["realized_pnl_ticks"] + snapshot["unrealized_pnl_ticks"] == (
        snapshot["trade_cash_ticks"] + snapshot["inventory"] * Fraction(101)
    )


def test_literal_costs_override_saved_float_projections_and_net_is_not_a_sum_of_views(
    ledger_tool: ModuleType,
) -> None:
    config = ledger_tool.EconomicConfig(
        tick=Fraction(1, 4),
        currency="USD",
        maker_fee=Fraction(1, 100),
        maker_rebate=Fraction(1, 200),
        taker_fee=Fraction(3, 100),
        proportional=Fraction(1, 1000),
    )
    ledger = ledger_tool.WeightedAverageLedger(config)
    first = ledger.apply_fill(_fill("F1", 1, 2, 100, 1, fee=999.0, rebate=999.0))
    second = ledger.apply_fill(_fill("F2", -1, 1, 104, 2, "taker", fee=999.0))
    assert first == {"fee": Fraction(7, 100), "rebate": Fraction(1, 100)}
    assert second == {"fee": Fraction(7, 125), "rebate": Fraction(0)}
    assert all(type(value) is Fraction for value in (*first.values(), *second.values()))
    snapshot = ledger.snapshot(Fraction(103), 3)
    assert snapshot["realized_pnl"] == 1
    assert snapshot["unrealized_pnl"] == Fraction(3, 4)
    assert snapshot["gross_pnl"] == Fraction(7, 4)
    assert snapshot["fees"] == Fraction(63, 500)
    assert snapshot["rebates"] == Fraction(1, 100)
    assert snapshot["cash"] == Fraction(-6029, 250)
    assert snapshot["net_pnl"] == Fraction(817, 500)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("1.2345", "1.234"),
        ("1.2355", "1.236"),
        ("-1.2345", "-1.234"),
        ("-1.2355", "-1.236"),
        ("9.9995", "10"),
        ("12345", "12340"),
        ("0.12345", "0.1234"),
    ),
)
def test_significant_rounding_is_integer_ties_even_across_sign_and_scale(
    ledger_tool: ModuleType, value: str, expected: str
) -> None:
    assert ledger_tool.round_significant(Fraction(value), precision=4) == Fraction(
        expected
    )


def test_repeating_view_rounds_to_28_digits_before_binary64_conversion(
    ledger_tool: ModuleType,
) -> None:
    assert ledger_tool.round_significant(Fraction(1, 3)) == Fraction(
        3333333333333333333333333333, 10**28
    )
    # The binary64 midpoint above 1 is 1 + 2**-53. The repeating value is
    # slightly below it, but its 28-digit decimal view lies above it.
    value = Fraction(1) + Fraction(1, 2**53) - Fraction(1, 3 * 10**40)
    assert _bits(float(value)) == "3ff0000000000000"
    assert _bits(ledger_tool.render_fraction(value)) == "3ff0000000000001"
    assert _bits(float(-value)) == "bff0000000000000"
    assert _bits(ledger_tool.render_fraction(-value)) == "bff0000000000001"


def test_terminating_projection_preserves_exact_conversion_and_zero_policy(
    ledger_tool: ModuleType,
) -> None:
    assert type(ledger_tool.render_fraction(Fraction(1, 8))) is float
    assert _bits(ledger_tool.render_fraction(Fraction(1, 8))) == "3fc0000000000000"
    assert _bits(ledger_tool.render_fraction(Fraction(-1, 8))) == "bfc0000000000000"
    assert _bits(ledger_tool.render_fraction(Fraction(0))) == "0000000000000000"
    assert ledger_tool.render_fraction(None) is None
    assert (
        _bits(ledger_tool.render_fraction(Fraction(1, 10**400))) == "0000000000000000"
    )
    assert (
        _bits(ledger_tool.render_fraction(Fraction(-1, 10**400))) == "8000000000000000"
    )


def test_rational_bit_cap_checks_numerator_and_denominator_without_decimal_strings(
    ledger_tool: ModuleType,
) -> None:
    assert ledger_tool.RATIONAL_BIT_LIMIT == 65536
    ledger_tool.enforce_rational_cap(Fraction(1 << 65535))
    ledger_tool.enforce_rational_cap(Fraction(1, 1 << 65535))
    with pytest.raises(ValueError):
        ledger_tool.enforce_rational_cap(Fraction(1 << 65536))
    with pytest.raises(ValueError):
        ledger_tool.enforce_rational_cap(Fraction(1, 1 << 65536))


def test_same_timestamp_fill_order_and_repeated_prefix_rows_are_admissible(
    ledger_tool: ModuleType,
) -> None:
    fills = [_fill("A", -1, 3, 105, 10), _fill("B", 1, 2, 100, 10)]
    original = copy.deepcopy(fills)
    assert ledger_tool.validate_fills(fills) is None
    previous = None
    for count, timestamp in ((0, 0), (0, 10), (1, 10), (1, 10), (2, 10)):
        assert (
            ledger_tool.validate_prefix_step(previous, count, 2, timestamp, fills)
            is None
        )
        previous = count
    assert fills == original
    ledger = ledger_tool.WeightedAverageLedger(_config(ledger_tool))
    ledger.apply_fill(fills[0])
    first = ledger.snapshot(Fraction(105), 10)
    assert first["inventory"] == -3
    assert first["average_cost_ticks"] == 105
    ledger.apply_fill(fills[1])
    second = ledger.snapshot(Fraction(105), 10)
    assert second["inventory"] == -1
    assert second["average_cost_ticks"] == 105
    assert second["realized_pnl_ticks"] == 10


@pytest.mark.parametrize(
    ("previous", "current", "timestamp"),
    (
        (None, 1, 10),
        (0, 2, 10),
        (2, 1, 10),
        (2, 3, 10),
        (0, 1, 11),
        (0, True, 10),
        (0, 0, -1),
    ),
)
def test_prefix_admission_rejects_missing_skipped_reversed_or_misaligned_prefixes(
    ledger_tool: ModuleType, previous: int | None, current: int, timestamp: int
) -> None:
    fills = [_fill("A", 1, 1, 100, 10), _fill("B", -1, 1, 101, 10)]
    with pytest.raises(ValueError):
        ledger_tool.validate_prefix_step(previous, current, 2, timestamp, fills)


@pytest.mark.parametrize(
    "problem", ("duplicate", "bool_quantity", "role", "timestamp", "causality")
)
def test_fill_admission_rejects_ambiguous_or_incoherent_original_rows(
    ledger_tool: ModuleType, problem: str
) -> None:
    fills = [_fill("A", 1, 1, 100, 10), _fill("B", -1, 1, 101, 20)]
    if problem == "duplicate":
        fills[1]["fill_id"] = "A"
    elif problem == "bool_quantity":
        fills[0]["quantity"] = True
    elif problem == "role":
        fills[0]["liquidity_role"] = "unknown"
    elif problem == "timestamp":
        fills[1]["exchange_fill_timestamp_ns"] = 9
    else:
        fills[0].update(send_timestamp_ns=5, exchange_arrival_timestamp_ns=11)
    with pytest.raises(ValueError):
        ledger_tool.validate_fills(fills)


def test_midpoint_admission_retains_exact_half_grid_and_zero_only_when_flat(
    ledger_tool: ModuleType,
) -> None:
    assert type(ledger_tool.admit_mark(1.5, 2)) is Fraction
    assert ledger_tool.admit_mark(1.5, 2) == Fraction(3, 2)
    assert ledger_tool.admit_mark(4503599627370495.5, -1) == Fraction(
        9007199254740991, 2
    )
    assert ledger_tool.admit_mark(0.0, 0) == 0


@pytest.mark.parametrize(
    ("mark", "inventory"),
    (
        (0.0, 1),
        (-0.0, 0),
        (-0.0, -1),
        (-1.0, 0),
        (1.25, 0),
        (4503599627370496.0, 0),
        (float("inf"), 0),
        (float("nan"), 0),
    ),
)
def test_midpoint_admission_rejects_nonidentifiable_or_invalid_marks(
    ledger_tool: ModuleType, mark: float, inventory: int
) -> None:
    with pytest.raises(ValueError):
        ledger_tool.admit_mark(mark, inventory)
