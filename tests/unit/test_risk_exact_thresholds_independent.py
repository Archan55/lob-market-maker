"""Preregistered exact continuous-risk oracle and transaction regressions.

Inputs and context cases were frozen before baseline execution. The oracle uses
Fraction on literal Decimals, never production risk or rendering helpers. These
are boundary and compatibility checks, not strategy or economic experiments.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_UP,
    Clamped,
    Context,
    Decimal,
    Inexact,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    getcontext,
    localcontext,
)
from fractions import Fraction
from typing import Any

import pytest

import lobmm.risk as risk_module
from lobmm.config import RiskConfig
from lobmm.enums import RiskReason, Side
from lobmm.risk import OpenOrderExposure, RiskError, RiskManager


@dataclass(frozen=True)
class Boundary:
    name: str
    high: str
    loss: str
    drawdown: str
    pnl: str


_CORE = (
    Boundary("loss-safe-lead", "0", "1.2345", "100", "-1.232"),
    Boundary("loss-boundary-0", "0", "1.2345", "100", "-1.2344"),
    Boundary("loss-boundary-1", "0", "1.2345", "100", "-1.2345"),
    Boundary("loss-boundary-2", "0", "1.2345", "100", "-1.2346"),
    Boundary("drawdown-boundary-0", "10", "100", "1.2345", "8.7656"),
    Boundary("drawdown-boundary-1", "10", "100", "1.2345", "8.7655"),
    Boundary("drawdown-boundary-2", "10", "100", "1.2345", "8.7654"),
    Boundary(
        "drawdown-default-below",
        "0",
        "100000",
        "1000",
        "-999.99999999999999999999999999",
    ),
    Boundary("drawdown-default-equal", "0", "100000", "1000", "-1000"),
    Boundary(
        "drawdown-default-above",
        "0",
        "100000",
        "1000",
        "-1000.00000000000000000000000001",
    ),
    Boundary(
        "loss-default-equal",
        "0",
        "999.99999999999999999999999999",
        "100000",
        "-999.99999999999999999999999999",
    ),
    Boundary("both-priority", "10", "1.2345", "1.2345", "-1.2345"),
    Boundary("large-finite-equal", "1e40", "1e50", "1e39", "9e39"),
    Boundary("tiny-finite-equal", "1.2345e-20", "1", "1e-24", "1.2344e-20"),
    Boundary("negative-zero", "0", "1", "1", "-0.000"),
)

_CONTEXTS = (
    "default28",
    "prec3-half-even",
    "prec3-up",
    "prec3-traps",
    "clamped-traps-and-flags",
)


def _context(name: str) -> Context:
    context = Context(prec=28, rounding=ROUND_HALF_EVEN)
    if name != "default28":
        context.prec = 3
    if name == "prec3-up":
        context.rounding = ROUND_UP
    if name in ("prec3-traps", "clamped-traps-and-flags"):
        context.traps[Inexact] = True
        context.traps[Rounded] = True
    if name == "clamped-traps-and-flags":
        context.Emin = -2
        context.Emax = 2
        context.clamp = 1
        for signal in (Underflow, Overflow, Subnormal, Clamped):
            context.traps[signal] = True
        for signal in (Inexact, Rounded, Subnormal, Clamped):
            context.flags[signal] = True
    return context


@contextmanager
def _preserved_environment(name: str) -> Iterator[None]:
    guard = sys.get_int_max_str_digits()
    outer = repr(getcontext())
    with localcontext(_context(name)) as active:
        before = repr(active)
        try:
            yield
        finally:
            assert repr(active) == before, "risk operation changed caller context/flags"
            assert sys.get_int_max_str_digits() == guard
    assert repr(getcontext()) == outer


def _freeze(value: Any) -> Any:
    if isinstance(value, Decimal):
        return (type(value), value.as_tuple())
    if isinstance(value, dict):
        return (type(value), tuple((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple, deque)):
        return (type(value), tuple(_freeze(item) for item in value))
    if isinstance(value, set):
        return (type(value), frozenset(value))
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value),
            tuple(
                (field.name, _freeze(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    if isinstance(value, RiskConfig):
        return (type(value), _freeze(value.model_dump(mode="python")))
    return (type(value), value)


def _state(manager: RiskManager) -> Any:
    # Deepcopy prevents an appended event or mutated deque from changing the
    # saved prestate in place, which a shallow vars().copy() would miss.
    return _freeze(deepcopy(vars(manager)))


def _manager(case: Boundary) -> tuple[RiskManager, Decimal]:
    manager = RiskManager(
        RiskConfig(max_loss=Decimal(case.loss), max_drawdown=Decimal(case.drawdown))
    )
    manager.high_watermark = Decimal(case.high)
    return manager, Decimal(case.pnl)


def _oracle(
    high: Decimal, pnl: Decimal, loss: Decimal, drawdown: Decimal
) -> tuple[RiskReason | None, Decimal, Fraction]:
    high_r, pnl_r = Fraction(high), Fraction(pnl)
    new_high = pnl if pnl_r > high_r else high
    distance = max(high_r, pnl_r) - pnl_r
    if pnl_r <= -Fraction(loss):
        reason = RiskReason.MAX_LOSS
    elif distance >= Fraction(drawdown):
        reason = RiskReason.MAX_DRAWDOWN
    else:
        reason = None
    return reason, new_high, distance


def _assert_first_observation(
    manager: RiskManager, pnl: Decimal, *, timestamp: int = 71
) -> None:
    reason, new_high, distance = _oracle(
        manager.high_watermark,
        pnl,
        manager.config.max_loss,
        manager.config.max_drawdown,
    )
    before = deepcopy(vars(manager))
    decision = manager.observe_pnl(pnl, timestamp_ns=timestamp)
    assert decision.approved is (reason is None)
    assert decision.reason is reason
    assert decision.cancel_open_orders is (reason is not None)
    assert not decision.risk_reducing
    assert manager.high_watermark.as_tuple() == new_high.as_tuple()
    assert manager.kill_switch_active is (reason is not None)
    assert manager.kill_reason is reason
    assert vars(manager).keys() == before.keys()
    for name, value in before.items():
        if name not in {
            "high_watermark",
            "kill_switch_active",
            "kill_reason",
            "events",
        }:
            assert _freeze(vars(manager)[name]) == _freeze(value)
    if reason is None:
        assert manager.events == []
        assert decision.detail == "continuous P&L checks passed"
        return
    assert len(manager.events) == 1
    event = manager.events[0]
    assert event.timestamp_ns == timestamp
    assert event.reason is reason
    assert event.order_id is None
    assert type(event.observed_value) is Decimal
    assert type(event.limit_value) is Decimal
    if reason is RiskReason.MAX_LOSS:
        assert Fraction(event.observed_value) == Fraction(pnl)
        assert event.observed_value.as_tuple() == pnl.as_tuple()
        assert Fraction(event.limit_value) == -Fraction(manager.config.max_loss)
        assert event.detail == decision.detail == "net P&L breached maximum loss"
    else:
        assert Fraction(event.observed_value) == distance
        assert Fraction(event.limit_value) == Fraction(manager.config.max_drawdown)
        assert event.limit_value.as_tuple() == manager.config.max_drawdown.as_tuple()
        assert event.detail == decision.detail == "net P&L breached maximum drawdown"


@pytest.mark.parametrize("case", _CORE, ids=lambda case: case.name)
@pytest.mark.parametrize("context_name", _CONTEXTS)
def test_frozen_boundaries_match_exact_rational_oracle(
    case: Boundary, context_name: str
) -> None:
    manager, pnl = _manager(case)
    with _preserved_environment(context_name):
        _assert_first_observation(manager, pnl)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-1.23450"), Decimal("-1.23450")),
        ("-1.23450", Decimal("-1.23450")),
        (-2, Decimal("-2")),
        (-1.2345, Decimal("-1.2345")),
    ],
    ids=["decimal", "string", "integer", "float-via-string"],
)
def test_public_pnl_conversion_policy_preserves_exact_observation(
    value: Decimal | str | int | float, expected: Decimal
) -> None:
    manager = RiskManager(
        RiskConfig(max_loss=Decimal("1.2345"), max_drawdown=Decimal("100"))
    )
    with _preserved_environment("prec3-traps"):
        decision = manager.observe_pnl(value, timestamp_ns=1)
        assert decision.reason is RiskReason.MAX_LOSS
        assert manager.events[0].observed_value.as_tuple() == expected.as_tuple()
        assert manager.events[0].limit_value.as_tuple() == Decimal("-1.2345").as_tuple()


@pytest.mark.parametrize("kind", ["loss", "drawdown"])
def test_ordinary_event_decimal_quantum_is_preserved(kind: str) -> None:
    if kind == "loss":
        manager, pnl = _manager(Boundary("format", "0", "1.2300", "100", "-1.23000"))
        observed, limit = Decimal("-1.23000"), Decimal("-1.2300")
    else:
        manager, pnl = _manager(
            Boundary("format", "10.000", "100", "1.2300", "8.77000")
        )
        # Exact subtraction retains the finer input exponent min(-3, -5).
        observed, limit = Decimal("1.23000"), Decimal("1.2300")
    with _preserved_environment("default28"):
        _assert_first_observation(manager, pnl)
        event = manager.events[0]
        assert event.observed_value.as_tuple() == observed.as_tuple()
        assert event.limit_value.as_tuple() == limit.as_tuple()


def test_first_kill_reason_and_event_are_sticky_across_peak_loss_and_recovery() -> None:
    manager = RiskManager(
        RiskConfig(max_loss=Decimal("1.2345"), max_drawdown=Decimal("1.2345"))
    )
    values = [
        Decimal(value) for value in ("10.000", "8.7655", "-1.2345", "11.0000", "9.7655")
    ]
    with _preserved_environment("prec3-traps"):
        assert manager.observe_pnl(values[0], timestamp_ns=1).approved
        first = manager.observe_pnl(values[1], timestamp_ns=2)
        assert first.reason is RiskReason.MAX_DRAWDOWN
        first_events = deepcopy(manager.events)
        assert len(first_events) == 1
        assert first_events[0].timestamp_ns == 2
        assert Fraction(first_events[0].observed_value) == Fraction(Decimal("1.2345"))
        loss = manager.observe_pnl(values[2], timestamp_ns=3)
        assert not loss.approved and loss.cancel_open_orders
        assert loss.reason is RiskReason.MAX_DRAWDOWN
        assert loss.detail == "net P&L breached maximum loss"
        recovered = manager.observe_pnl(values[3], timestamp_ns=4)
        # Preserve the existing public return behavior after recovery even
        # though the manager's sticky kill flag remains active.
        assert recovered.approved and not recovered.cancel_open_orders
        assert recovered.reason is None
        assert manager.high_watermark.as_tuple() == values[3].as_tuple()
        again = manager.observe_pnl(values[4], timestamp_ns=5)
        assert again.reason is RiskReason.MAX_DRAWDOWN
        assert not again.approved and again.cancel_open_orders
        assert manager.kill_switch_active
        assert manager.kill_reason is RiskReason.MAX_DRAWDOWN
        assert _freeze(manager.events) == _freeze(first_events)


@pytest.mark.parametrize(
    "case",
    [_CORE[1], _CORE[2], _CORE[4], _CORE[5]],
    ids=["safe-loss", "equal-loss", "safe-drawdown", "equal-drawdown"],
)
def test_continuous_cancellation_and_pretrade_paths_share_exact_boundaries(
    case: Boundary,
) -> None:
    manager, pnl = _manager(case)
    reason, new_high, _ = _oracle(
        manager.high_watermark,
        pnl,
        manager.config.max_loss,
        manager.config.max_drawdown,
    )
    orders = (
        OpenOrderExposure(
            order_id="existing",
            side=Side.BID,
            remaining_quantity=1,
            accepted_timestamp_ns=0,
        ),
    )
    with _preserved_environment("prec3-traps"):
        result = manager.continuous_check(
            timestamp_ns=71, net_pnl=pnl, inventory=2, open_orders=orders
        )
        assert result.kill_switch_active is (reason is not None)
        assert result.cancel_order_ids == (() if reason is None else ("existing",))
        assert manager.high_watermark.as_tuple() == new_high.as_tuple()
        new_order = manager.check_order(
            side=Side.BID,
            quantity=1,
            timestamp_ns=72,
            inventory=2,
            open_orders=(),
            is_quote=False,
        )
        assert new_order.approved is (reason is None)
        assert new_order.reason is (None if reason is None else RiskReason.KILL_SWITCH)
        reducing = manager.check_order(
            side=Side.ASK,
            quantity=1,
            timestamp_ns=73,
            inventory=2,
            open_orders=(),
            reduce_only=True,
            is_quote=False,
        )
        assert reducing.approved and reducing.risk_reducing


def _assert_rejected_atomically(
    manager: RiskManager, pnl: Decimal | str, *, timestamp: int = 99
) -> None:
    before = _state(manager)
    with _preserved_environment("prec3-traps"):
        with pytest.raises(RiskError):
            manager.observe_pnl(pnl, timestamp_ns=timestamp)
        assert _state(manager) == before


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", "invalid"])
def test_nonfinite_or_invalid_pnl_is_rejected_before_owned_state_changes(
    value: str,
) -> None:
    manager, _ = _manager(Boundary("invalid", "10.000", "100", "100", "0"))
    pnl = value if value == "invalid" else Decimal(value)
    _assert_rejected_atomically(manager, pnl)


def test_negative_timestamp_is_rejected_before_owned_state_changes() -> None:
    manager, pnl = _manager(Boundary("invalid-time", "10.000", "100", "100", "11.000"))
    _assert_rejected_atomically(manager, pnl, timestamp=-1)


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_explicit_nonfinite_high_watermark_corruption_is_rejected_atomically(
    value: str,
) -> None:
    manager, pnl = _manager(Boundary("invalid-high", "0", "100", "100", "11.000"))
    manager.high_watermark = Decimal(value)
    _assert_rejected_atomically(manager, pnl)


@pytest.mark.parametrize("field", ["max_loss", "max_drawdown"])
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "sNaN", "Infinity", "-Infinity"])
def test_explicit_invalid_limit_corruption_is_rejected_atomically(
    field: str, value: str
) -> None:
    manager, pnl = _manager(Boundary("invalid-limit", "0", "100", "100", "11.000"))
    # Deliberately bypass Pydantic validation to exercise observe_pnl's stated
    # invalid-state boundary, rather than pretending this is normal config.
    manager.config = manager.config.model_copy(update={field: Decimal(value)})
    _assert_rejected_atomically(manager, pnl)


class ConstructionReviewError(RuntimeError):
    """Injected ordinary event or decision construction failed."""


@pytest.mark.parametrize(
    ("constructor", "scenario"),
    [
        ("RiskEvent", "loss"),
        ("RiskEvent", "drawdown"),
        ("RiskDecision", "approved-peak"),
        ("RiskDecision", "loss"),
        ("RiskDecision", "drawdown"),
    ],
)
def test_ordinary_constructor_failure_does_not_partially_commit_observation(
    constructor: str, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = {
        "loss": Boundary("fault-loss", "0", "1.2345", "100", "-1.2345"),
        "drawdown": Boundary("fault-drawdown", "10", "100", "1.2345", "8.7655"),
        "approved-peak": Boundary("fault-peak", "0", "100", "100", "10.123"),
    }
    manager, pnl = _manager(cases[scenario])
    before = _state(manager)
    calls: list[object] = []

    def fail(*args: object, **kwargs: object) -> Any:
        calls.append((args, kwargs))
        raise ConstructionReviewError("ordinary risk construction failed")

    monkeypatch.setattr(risk_module, constructor, fail)
    with _preserved_environment("default28"):
        with pytest.raises(
            ConstructionReviewError, match="ordinary risk construction failed"
        ):
            manager.observe_pnl(pnl, timestamp_ns=99)
        assert len(calls) == 1
        assert _state(manager) == before
