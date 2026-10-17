"""Independent v2 review: seven-unit scalar accounting, identity and fault probes.

Only the public v2 boundary and immutable evidence types are shared with the
implementation. No study fixture, venue oracle, or verdict helper is imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

from lobmm.durable_recovery import Receipt, RecoveryError
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import CancelRequest, ExecutionReport, Fill, NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse

ACCOUNT = "independent-account"
SESSION = "independent-recovery"
STRATEGY = "independent-review"


def _session(root: Path, *, reopen: bool = False) -> OutboundRecoverySessionV2:
    constructor = (
        OutboundRecoverySessionV2.open if reopen else OutboundRecoverySessionV2.create
    )
    return constructor(root, ACCOUNT, SESSION, STRATEGY)


def _new(side: Side = Side.BID, cid: str = "independent-seven") -> NewOrderRequest:
    return NewOrderRequest(cid, side, 113, 7, STRATEGY, 5, 6)


def _fill_report(
    request: NewOrderRequest,
    *,
    quantity: int = 2,
    remaining: int = 5,
    timestamp: int = 20,
    identity: str = "two",
) -> ExecutionReport:
    fill = Fill(
        "fill-" + identity,
        "venue-" + request.client_order_id,
        request.client_order_id,
        STRATEGY,
        request.side,
        quantity,
        request.price_ticks,
        LiquidityRole.MAKER,
        timestamp,
        Decimal("0.19") if quantity == 2 else Decimal("0.31"),
        Decimal("0.01") if quantity == 2 else Decimal("0.04"),
    )
    return ExecutionReport(
        "report-" + identity,
        ReportType.PARTIAL_FILL if remaining else ReportType.FILL,
        timestamp,
        fill.order_id,
        request.client_order_id,
        OrderStatus.PARTIALLY_FILLED if remaining else OrderStatus.FILLED,
        remaining,
        fill,
    )


def _accepted(request: NewOrderRequest, *, timestamp: int = 11) -> ExecutionReport:
    return ExecutionReport(
        "accepted-" + request.client_order_id,
        ReportType.ACCEPTED,
        timestamp,
        "venue-" + request.client_order_id,
        request.client_order_id,
        OrderStatus.LIVE,
        request.quantity,
    )


def _cancelled(request: NewOrderRequest, *, remaining: int = 5) -> ExecutionReport:
    return ExecutionReport(
        "cancelled-" + request.client_order_id,
        ReportType.CANCELLED,
        40,
        "venue-" + request.client_order_id,
        request.client_order_id,
        OrderStatus.CANCELLED,
        remaining,
    )


def _work(state: dict[str, Any], cid: str = "independent-seven") -> dict[str, Any]:
    return next(
        row for row in state["cancellation_work"] if row["client_order_id"] == cid
    )


def _unknown(request: NewOrderRequest, stamp: int = 12) -> ExecutionReport:
    return ExecutionReport(
        "unknown-" + str(stamp),
        ReportType.CANCEL_REJECTED,
        stamp,
        "",
        request.client_order_id,
        OrderStatus.REJECTED,
        0,
        reason="unknown_order",
    )


def _response(
    request: NewOrderRequest,
    *,
    status: OrderStatus | None = None,
    remaining: int | None = None,
    fills: tuple[Fill, ...] = (),
) -> ReconciliationResponse:
    return ReconciliationResponse(
        "independent-query",
        41,
        42,
        42,
        100,
        True,
        (
            OrderEvidence(
                request.client_order_id,
                40 if status is not None else None,
                status,
                remaining,
                fills,
                False,
            ),
        ),
    )


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_independent_seven_unit_reordering_restart_and_exact_scalar_ledger(
    tmp_path: Path, side: Side
) -> None:
    root = tmp_path / "journal"
    request = _new(side)
    with _session(root) as live:
        sent: list[Any] = []
        live.submit_new(request, timestamp_ns=10, sink=sent.append)
        assert sent == [request]
    first = _fill_report(request)
    last = _fill_report(request, quantity=5, remaining=0, timestamp=30, identity="five")
    with _session(root, reopen=True) as recovered:
        assert recovered.snapshot()["entry_enabled"] is False
        recovered.capture(Receipt("unknown", 13, _unknown(request)))
        state = recovered.snapshot()
        assert not _work(state)["terminal"]
        assert state["knowledge"]["inventory_min"] == (0 if side is Side.BID else -7)
        assert state["knowledge"]["inventory_max"] == (7 if side is Side.BID else 0)
        # A terminal report can arrive before older execution detail. Actual
        # execution is seven; only five units are bookable at this prefix.
        recovered.capture(Receipt("five", 35, last))
        state = recovered.snapshot()
        assert _work(state)["terminal"]
        assert state["ledger"]["known_inventory"] == int(side) * 5
        assert state["ledger"]["trade_cash_ticks"] == -int(side) * 565
        assert state["knowledge"]["inventory_min"] == int(side) * 7
        assert state["knowledge"]["inventory_max"] == int(side) * 7
        assert state["knowledge"]["orders"][0]["missing_fill_quantity_min"] == 2
        assert state["knowledge"]["complete"] is False
        assert (
            recovered.recover_cancel(
                request.client_order_id, timestamp_ns=36, sink=sent.append
            )
            is False
        )
        recovered.capture(Receipt("two", 37, first))
        recovered.capture(Receipt("two-again", 38, first))
        recovered.capture(Receipt("five-again", 39, last))
        # Redelivery can have a different local envelope but immutable report
        # and fill identities still book each execution exactly once locally.
        state = recovered.snapshot()
        assert state["ledger"] == {
            "known_inventory": int(side) * 7,
            "trade_cash_ticks": -int(side) * 791,
            "fees": "0.50",
            "rebates": "0.05",
        }
        assert state["knowledge"]["complete"] is True
        assert state["entry_enabled"] is False
        detached = recovered.snapshot()
        detached["ledger"]["known_inventory"] = 999
        assert recovered.snapshot() == state
    with _session(root, reopen=True) as again:
        assert again.snapshot() == state
        with pytest.raises(RecoveryError):
            again.submit_new(
                _new(cid="new-after-flat"), timestamp_ns=50, sink=sent.append
            )
        assert len(sent) == 1


def test_independent_mixed_side_interval_and_cash_do_not_net_away_reservations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mixed"
    bid, ask = _new(cid="bid"), _new(Side.ASK, "ask")
    with _session(root) as live:
        live.submit_new(bid, timestamp_ns=10, sink=lambda _: None)
        live.submit_new(ask, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        client.capture(Receipt("bid-two", 25, _fill_report(bid)))
        client.capture(
            Receipt(
                "ask-five",
                30,
                _fill_report(ask, quantity=5, remaining=2, identity="ask-five"),
            )
        )
        state = client.snapshot()
        # Independent scalars: buy 2*113, sell 5*113. Unseen bid 0..5 and
        # unseen ask 0..2 must remain separate before combining the interval.
        assert state["ledger"]["known_inventory"] == -3
        assert state["ledger"]["trade_cash_ticks"] == 339
        assert state["knowledge"]["inventory_min"] == -5
        assert state["knowledge"]["inventory_max"] == 2
        assert (
            sum(row["reservation_quantity"] for row in state["knowledge"]["orders"])
            == 7
        )
        assert not any(row["terminal"] for row in state["cancellation_work"])


def test_terminal_without_detail_stops_cancel_but_never_completes_accounting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        client.capture(Receipt("accepted", 12, _accepted(request)))
        client.capture(
            Receipt(
                "terminal-query",
                43,
                _response(request, status=OrderStatus.CANCELLED, remaining=5),
            )
        )
        state = client.snapshot()
        assert _work(state)["terminal"]
        assert state["ledger"]["known_inventory"] == 0
        assert state["ledger"]["trade_cash_ticks"] == 0
        assert state["knowledge"]["inventory_min"] == 2
        assert state["knowledge"]["inventory_max"] == 2
        assert state["knowledge"]["orders"][0]["missing_fill_quantity_min"] == 2
        assert state["knowledge"]["complete"] is False
        assert (
            client.recover_cancel(
                request.client_order_id,
                timestamp_ns=44,
                sink=lambda _: pytest.fail("terminal cancel sent"),
            )
            is False
        )
        assert state["entry_enabled"] is False


def test_terminal_query_without_any_venue_identity_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "unbound"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        before = client.snapshot()
        with pytest.raises(RecoveryError):
            client.capture(
                Receipt(
                    "unbound-terminal",
                    43,
                    _response(request, status=OrderStatus.FILLED, remaining=0),
                )
            )
    with _session(root, reopen=True) as client:
        assert client.snapshot() == before
        assert not _work(client.snapshot())["terminal"]


def test_unknown_absent_and_exhausted_cancel_work_remains_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "attempts"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    cancels: list[Any] = []
    with _session(root, reopen=True) as client:
        client.capture(Receipt("absent", 43, _response(request)))
        client.capture(Receipt("unknown", 44, _unknown(request, 43)))
        for attempt in range(4):
            assert (
                client.recover_cancel(
                    request.client_order_id,
                    timestamp_ns=50 + attempt,
                    sink=cancels.append,
                )
                is True
            )
            state = client.snapshot()
            assert _work(state)["attempts"] == attempt + 1
            assert not _work(state)["terminal"]
        assert all(isinstance(cancel, CancelRequest) for cancel in cancels)
        assert all(
            cancel.client_order_id == request.client_order_id
            and cancel.order_id is None
            for cancel in cancels
        )
        state = client.snapshot()
        assert _work(state)["exhausted"] is True
        assert state["knowledge"]["inventory_min"] == 0
        assert state["knowledge"]["inventory_max"] == 7
        with pytest.raises(RecoveryError):
            client.recover_cancel(
                request.client_order_id, timestamp_ns=55, sink=cancels.append
            )
    with _session(root, reopen=True) as client:
        assert _work(client.snapshot())["attempts"] == 4
        assert not _work(client.snapshot())["terminal"]
        assert client.snapshot()["entry_enabled"] is False
    assert len(cancels) == 4


@pytest.mark.parametrize(
    "case",
    (
        "pre_actual_dispatch_report",
        "pre_actual_dispatch_fill",
        "pre_actual_dispatch_query_state",
        "wrong_side",
        "wrong_strategy",
        "wrong_client",
        "wrong_venue",
        "bad_limit_price",
        "overfill",
        "bad_remaining",
        "noncausal_notification",
        "terminal_before_fill",
        "fill_identity_conflict",
        "report_identity_conflict",
        "receipt_identity_conflict",
    ),
)
def test_invalid_evidence_poisons_without_committing_or_erasing_prior_prefix(
    tmp_path: Path, case: str
) -> None:
    root = tmp_path / case
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        first = _fill_report(request)
        client.capture(Receipt("good-two", 21, first))
        before = client.snapshot()
        bad = _fill_report(
            request, quantity=5, remaining=0, timestamp=30, identity="five"
        )
        assert bad.fill is not None and first.fill is not None
        receipt = Receipt("bad", 40, bad)
        if case == "pre_actual_dispatch_report":
            receipt = Receipt("bad", 40, _accepted(request, timestamp=8))
        elif case == "pre_actual_dispatch_fill":
            receipt = Receipt(
                "bad",
                40,
                replace(
                    bad,
                    exchange_timestamp_ns=8,
                    fill=replace(bad.fill, exchange_fill_timestamp_ns=8),
                ),
            )
        elif case == "pre_actual_dispatch_query_state":
            response = _response(request, status=OrderStatus.CANCELLED, remaining=5)
            receipt = Receipt(
                "bad",
                50,
                replace(
                    response,
                    orders=(replace(response.orders[0], state_timestamp_ns=8),),
                ),
            )
        elif case == "wrong_side":
            receipt = replace(
                receipt, payload=replace(bad, fill=replace(bad.fill, side=Side.ASK))
            )
        elif case == "wrong_strategy":
            receipt = replace(
                receipt,
                payload=replace(bad, fill=replace(bad.fill, strategy_id="other")),
            )
        elif case == "wrong_client":
            receipt = replace(receipt, payload=replace(bad, client_order_id="other"))
        elif case == "wrong_venue":
            receipt = replace(
                receipt,
                payload=replace(
                    bad, order_id="other", fill=replace(bad.fill, order_id="other")
                ),
            )
        elif case == "bad_limit_price":
            receipt = replace(
                receipt, payload=replace(bad, fill=replace(bad.fill, price_ticks=114))
            )
        elif case == "overfill":
            receipt = replace(
                receipt, payload=replace(bad, fill=replace(bad.fill, quantity=6))
            )
        elif case == "bad_remaining":
            receipt = replace(receipt, payload=replace(bad, remaining_quantity=1))
        elif case == "noncausal_notification":
            receipt = replace(receipt, notification_timestamp_ns=29)
        elif case == "terminal_before_fill":
            receipt = Receipt(
                "bad", 40, replace(_cancelled(request), exchange_timestamp_ns=19)
            )
        elif case == "fill_identity_conflict":
            receipt = replace(
                receipt,
                payload=replace(
                    bad, fill=replace(bad.fill, fill_id=first.fill.fill_id)
                ),
            )
        elif case == "report_identity_conflict":
            receipt = replace(receipt, payload=replace(bad, report_id=first.report_id))
        elif case == "receipt_identity_conflict":
            receipt = replace(receipt, receipt_id="good-two")
        with pytest.raises(RecoveryError):
            client.capture(receipt)
        with pytest.raises(RecoveryError):
            client.snapshot()
    with _session(root, reopen=True) as verified:
        assert verified.snapshot() == before


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_fill_may_improve_limit_but_never_cross_adversely(
    tmp_path: Path, side: Side
) -> None:
    request = _new(side)
    root = tmp_path / "price"
    with _session(root) as client:
        client.submit_new(request, timestamp_ns=10, sink=lambda _: None)
        report = _fill_report(request)
        assert report.fill is not None
        good_price = request.price_ticks - int(side)
        client.capture(
            Receipt(
                "improved",
                21,
                replace(report, fill=replace(report.fill, price_ticks=good_price)),
            )
        )
        assert (
            client.snapshot()["ledger"]["trade_cash_ticks"]
            == -int(side) * 2 * good_price
        )
        adverse = _fill_report(
            request, quantity=5, remaining=0, timestamp=30, identity="adverse"
        )
        assert adverse.fill is not None
        with pytest.raises(RecoveryError):
            client.capture(
                Receipt(
                    "adverse",
                    31,
                    replace(
                        adverse,
                        fill=replace(
                            adverse.fill, price_ticks=request.price_ticks + int(side)
                        ),
                    ),
                )
            )


def test_decimal_accounting_replay_is_independent_of_callers_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "decimal"
    request = _new()
    with _session(root) as client:
        client.submit_new(request, timestamp_ns=10, sink=lambda _: None)
        first = _fill_report(request)
        last = _fill_report(
            request, quantity=5, remaining=0, timestamp=30, identity="five"
        )
        assert first.fill is not None and last.fill is not None
        first = replace(
            first,
            fill=replace(first.fill, fee=Decimal("100001"), rebate=Decimal("0.010001")),
        )
        last = replace(
            last,
            fill=replace(last.fill, fee=Decimal("200002"), rebate=Decimal("0.020002")),
        )
        with localcontext() as context:
            context.prec = 4
            context.Emax = 4
            client.capture(Receipt("two", 21, first))
            client.capture(Receipt("five", 31, last))
            assert Decimal(client.snapshot()["ledger"]["fees"]) == Decimal("300003")
            assert Decimal(client.snapshot()["ledger"]["rebates"]) == Decimal(
                "0.030003"
            )
    with _session(root, reopen=True) as client:
        assert Decimal(client.snapshot()["ledger"]["fees"]) == Decimal("300003")


_LOCK_PROBE = r"""
import sys
from pathlib import Path
from lobmm.durable_recovery import RecoveryError
from lobmm.outbound_recovery import OutboundRecoverySessionV2
try:
    with OutboundRecoverySessionV2.open(Path(sys.argv[1]), "independent-account", "independent-recovery", "independent-review"):
        pass
except RecoveryError:
    sys.exit(73)
"""


def _probe(root: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", _LOCK_PROBE, str(root)],
        timeout=5,
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def test_second_process_refused_and_close_releases_local_lifetime_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lock"
    client = _session(root)
    try:
        assert _probe(root) == 73
        with pytest.raises(RecoveryError):
            _session(root, reopen=True)
    finally:
        client.close()
    assert _probe(root) == 0
    with pytest.raises(RecoveryError):
        client.snapshot()


def test_forked_object_cannot_emit_capture_snapshot_or_unlock_parent(
    tmp_path: Path,
) -> None:
    script = r"""
import json, os, runpy, select, sys
from pathlib import Path
from lobmm.durable_recovery import Receipt, RecoveryError
symbols = runpy.run_path(sys.argv[1])
_session, _new, _accepted, _probe = (symbols[name] for name in ("_session", "_new", "_accepted", "_probe"))
root = Path(sys.argv[2])
request = _new()
client = _session(root)
client.submit_new(request, timestamp_ns=10, sink=lambda _: None)
reader, writer = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(reader)
    results = []
    for action in (
        client.snapshot,
        lambda: client.submit_new(
            _new(cid="child"), timestamp_ns=11, sink=lambda _: None
        ),
        lambda: client.capture(Receipt("child", 12, _accepted(request))),
        lambda: client.recover_cancel(
            request.client_order_id, timestamp_ns=13, sink=lambda _: None
        ),
        client.close,
    ):
        try:
            action()
        except RecoveryError:
            results.append(True)
        else:
            results.append(False)
    os.write(writer, json.dumps(results).encode())
    os._exit(0)
os.close(writer)
try:
    assert select.select([reader], [], [], 5)[0], "fork child did not answer"
    data = os.read(reader, 4096)
    assert os.waitpid(pid, 0)[1] == 0
    assert json.loads(data) == [True] * 5
    assert _probe(root) == 73
    assert client.snapshot()["entry_enabled"] is True
finally:
    os.close(reader)
    client.close()
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            script,
            str(Path(__file__).resolve()),
            str(tmp_path / "fork"),
        ],
        timeout=15,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("operation", ("submit", "capture", "cancel"))
def test_reentrant_access_poisons_outer_operation_even_if_callback_swallows_error(
    tmp_path: Path, operation: str
) -> None:
    root = tmp_path / operation
    request = _new()
    client = _session(root)
    if operation != "submit":
        client.submit_new(request, timestamp_ns=10, sink=lambda _: None)

    def nested(_: Any) -> None:
        with suppress(RecoveryError):
            client.snapshot()

    def hook(_: str) -> None:
        nested(None)

    try:
        with pytest.raises(RecoveryError):
            if operation == "submit":
                client.submit_new(request, timestamp_ns=10, sink=nested)
            elif operation == "capture":
                client.capture(Receipt("accepted", 12, _accepted(request)), hook=hook)
            else:
                client.recover_cancel(
                    request.client_order_id, timestamp_ns=13, sink=nested
                )
        with pytest.raises(RecoveryError):
            client.snapshot()
    finally:
        client.close()
    with _session(root, reopen=True) as reopened:
        assert reopened.snapshot()["entry_enabled"] is False


@pytest.mark.parametrize("operation", ("new", "cancel"))
def test_sink_failure_preserves_possibly_sent_work_and_never_retries_new(
    tmp_path: Path, operation: str
) -> None:
    root = tmp_path / operation
    request = _new()
    client = _session(root)
    if operation == "cancel":
        client.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    observed: list[Any] = []

    def fail(command: Any) -> None:
        observed.append(command)
        raise OSError("transport returned after an unknown remote effect")

    try:
        with pytest.raises((RecoveryError, OSError)):
            if operation == "new":
                client.submit_new(request, timestamp_ns=10, sink=fail)
            else:
                client.recover_cancel(
                    request.client_order_id, timestamp_ns=12, sink=fail
                )
        with pytest.raises(RecoveryError):
            client.snapshot()
    finally:
        client.close()
    with _session(root, reopen=True) as reopened:
        state = reopened.snapshot()
        assert not _work(state)["terminal"]
        assert _work(state)["attempts"] == int(operation == "cancel")
        assert len(state["outbound"]) == 1 + int(operation == "cancel")
        assert state["knowledge"]["inventory_min"] == 0
        assert state["knowledge"]["inventory_max"] == 7
        with pytest.raises(RecoveryError):
            reopened.submit_new(request, timestamp_ns=20, sink=observed.append)
    assert len(observed) == 1


def test_duplicate_receipt_with_impossible_notification_is_invalid_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "duplicate-causality"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        report = _accepted(request)
        client.capture(Receipt("same", 12, report))
        before = client.snapshot()
        with pytest.raises(RecoveryError):
            client.capture(Receipt("same", 8, report))
        with pytest.raises(RecoveryError):
            client.snapshot()
    with _session(root, reopen=True) as client:
        assert client.snapshot() == before


# This venue is an independent process with a deliberately small integer model.
# It imports no lobmm code and remains alive across real client SIGKILLs.
_SCALAR_VENUE = r"""
import json, socketserver, sys
state = {"order": None, "pending": None, "new_count": 0, "cancel_count": 0, "trace": []}
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        command = json.loads(self.rfile.readline())
        op = command["op"]
        if op in ("new", "new_delayed"):
            state["new_count"] += 1
            request = command["request"]
            order = dict(client_order_id=request["client_order_id"], quantity=request["quantity"],
                         side=request["side"], price=request["price_ticks"], filled=0,
                         remaining=request["quantity"], cash=0, live=True)
            if op == "new_delayed":
                state["pending"] = order
            else:
                state["order"] = order
            result = {"accepted": op == "new"}
        elif op == "advance_new":
            state["order"], state["pending"] = state["pending"], None
            result = {"accepted": True}
        elif op == "trade":
            order = state["order"]
            quantity = min(command["quantity"], order["remaining"]) if order and order["live"] else 0
            if quantity:
                order["remaining"] -= quantity
                order["filled"] += quantity
                order["cash"] -= order["side"] * quantity * order["price"]
            result = {"filled_now": quantity}
        elif op == "cancel":
            state["cancel_count"] += 1
            order = state["order"]
            if order is None:
                result = {"unknown": True}
            else:
                order["live"] = False
                result = {"unknown": False, "remaining": order["remaining"]}
        elif op == "state":
            result = json.loads(json.dumps(state))
        else:
            raise AssertionError(op)
        state["trace"].append({"input": command, "effect": result})
        self.wfile.write((json.dumps(result) + "\n").encode())
server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""

_KILLED_NEW_CLIENT = r"""
import json, os, signal, socket, sys
from dataclasses import asdict
from pathlib import Path
from lobmm.enums import Side
from lobmm.orders import NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2
root, port, delayed = Path(sys.argv[1]), int(sys.argv[2]), bool(int(sys.argv[3]))
client = OutboundRecoverySessionV2.create(root, "independent-account", "independent-recovery", "independent-review")
request = NewOrderRequest("independent-seven", Side.BID, 113, 7, "independent-review", 5, 6)
def sink(request):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall((json.dumps({"op": "new_delayed" if delayed else "new", "request": asdict(request)}) + "\n").encode())
        stream = connection.makefile("rb")
        assert json.loads(stream.readline())
    os.kill(os.getpid(), signal.SIGKILL)
client.submit_new(request, timestamp_ns=10, sink=sink)
raise AssertionError("client survived requested kill")
"""


def _venue_rpc(port: int, command: dict[str, Any]) -> dict[str, Any]:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall((json.dumps(command) + "\n").encode())
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline())  # type: ignore[no-any-return]


@pytest.mark.parametrize("delayed", (False, True))
@pytest.mark.parametrize("recover", (False, True))
def test_real_dead_client_surviving_integer_venue_and_delayed_old_new(
    tmp_path: Path, delayed: bool, recover: bool, pytestconfig: pytest.Config
) -> None:
    import select

    venue = subprocess.Popen(
        [sys.executable, "-u", "-c", _SCALAR_VENUE],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert venue.stdout is not None
        assert select.select([venue.stdout], [], [], 5)[0], "venue did not start"
        port = int(venue.stdout.readline())
        root = tmp_path / "real-death"
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _KILLED_NEW_CLIENT,
                str(root),
                str(port),
                str(int(delayed)),
            ],
            timeout=10,
            check=False,
            capture_output=True,
            text=True,
        )
        assert child.returncode == -9, child.stderr
        assert venue.poll() is None
        with _session(root, reopen=True) as recovered:
            request = _new()
            if delayed:
                replies = []
                assert recovered.recover_cancel(
                    request.client_order_id,
                    timestamp_ns=12,
                    sink=lambda _: replies.append(_venue_rpc(port, {"op": "cancel"})),
                )
                assert replies == [{"unknown": True}]
                recovered.capture(Receipt("early-unknown", 14, _unknown(request, 13)))
                assert not _work(recovered.snapshot())["terminal"]
                # The old client's accepted transport work arrives only after
                # its death and the new process's first cancellation attempt.
                _venue_rpc(port, {"op": "advance_new"})
            _venue_rpc(port, {"op": "trade", "quantity": 2})
            before = _venue_rpc(port, {"op": "state"})
            assert (
                before["order"]["quantity"]
                == before["order"]["filled"] + before["order"]["remaining"]
                == 7
            )
            assert before["order"]["filled"] == 2
            if recover:
                replies = []
                recovered.recover_cancel(
                    request.client_order_id,
                    timestamp_ns=30,
                    sink=lambda _: replies.append(_venue_rpc(port, {"op": "cancel"})),
                )
                assert replies == [{"unknown": False, "remaining": 5}]
                recovered.capture(Receipt("cancelled", 41, _cancelled(request)))
            _venue_rpc(port, {"op": "trade", "quantity": 5})
            after = _venue_rpc(port, {"op": "state"})
            expected = 2 if recover else 7
            assert after["order"]["filled"] == expected
            assert after["order"]["cash"] == -113 * expected
            assert (
                after["order"]["quantity"]
                == after["order"]["filled"] + after["order"]["remaining"]
                == 7
            )
            assert after["new_count"] == 1
            state = recovered.snapshot()
            assert state["entry_enabled"] is False
            assert state["account_fenced"] is False
            assert (
                state["knowledge"]["inventory_min"]
                <= expected
                <= state["knowledge"]["inventory_max"]
            )
            assert state["ledger"]["known_inventory"] == 0
            assert state["knowledge"]["complete"] is False
            assert _work(state)["terminal"] is recover
            assert _work(state)["attempts"] == int(delayed) + int(recover)
            # Preserve both causal sides as a test artifact for reproduction.
            _publish_independent(
                pytestconfig,
                tmp_path,
                f"new-delayed-{delayed}-recovered-{recover}",
                {
                    "process_deaths": 1,
                    "delayed": delayed,
                    "recovered": recover,
                    "venue_before": before,
                    "venue_after": after,
                    "client": state,
                    "journal": json.loads((root / "journal.json").read_text()),
                },
            )
    finally:
        venue.terminate()
        venue.communicate(timeout=5)


_KILLED_CAPTURE_CLIENT = r"""
import os, signal, sys
from decimal import Decimal
from pathlib import Path
from lobmm.durable_recovery import Receipt
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill
from lobmm.outbound_recovery import OutboundRecoverySessionV2
client = OutboundRecoverySessionV2.open(Path(sys.argv[1]), "independent-account", "independent-recovery", "independent-review")
fill = Fill("fill-two", "venue-independent-seven", "independent-seven", "independent-review", Side.BID,
            2, 113, LiquidityRole.MAKER, 20, Decimal("0.19"), Decimal("0.01"))
report = ExecutionReport("report-two", ReportType.PARTIAL_FILL, 20, fill.order_id, fill.client_order_id,
                         OrderStatus.PARTIALLY_FILLED, 5, fill)
def hook(stage):
    if stage == sys.argv[2]:
        os.kill(os.getpid(), signal.SIGKILL)
client.capture(Receipt("two", 21, report), hook=hook)
if sys.argv[2] == "after_return":
    os.kill(os.getpid(), signal.SIGKILL)
raise AssertionError("client survived requested kill")
"""


@pytest.mark.parametrize(
    "stage",
    (
        "partial_write",
        "after_file_fsync",
        "after_replace",
        "after_directory_fsync",
        "after_return",
    ),
)
def test_real_capture_death_redelivery_commits_each_fill_once(
    tmp_path: Path, stage: str, pytestconfig: pytest.Config
) -> None:
    root = tmp_path / "capture"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    killed = subprocess.run(
        [sys.executable, "-c", _KILLED_CAPTURE_CLIENT, str(root), stage],
        timeout=10,
        check=False,
        capture_output=True,
        text=True,
    )
    assert killed.returncode == -9, killed.stderr
    with _session(root, reopen=True) as client:
        prefix = client.snapshot()
        durable_prefix = json.loads((root / "journal.json").read_text())
        persisted = prefix["ledger"]["known_inventory"]
        assert persisted == (0 if stage in {"partial_write", "after_file_fsync"} else 2)
        client.capture(Receipt("two", 21, _fill_report(request)))
        client.capture(
            Receipt(
                "five",
                31,
                _fill_report(
                    request, quantity=5, remaining=0, timestamp=30, identity="five"
                ),
            )
        )
        state = client.snapshot()
        assert state["ledger"] == {
            "known_inventory": 7,
            "trade_cash_ticks": -791,
            "fees": "0.50",
            "rebates": "0.05",
        }
        assert state["knowledge"]["complete"] is True
        assert state["entry_enabled"] is False
        assert len(state["receipts"]) == 2
        _publish_independent(
            pytestconfig,
            tmp_path,
            "capture-" + stage,
            {
                "process_deaths": 1,
                "stage": stage,
                "prefix": prefix,
                "prefix_journal": durable_prefix,
                "final": state,
                "final_journal": json.loads((root / "journal.json").read_text()),
            },
        )


@pytest.mark.parametrize("access", ("snapshot", "close"))
@pytest.mark.parametrize(
    "stage",
    (
        "before_temp",
        "partial_write",
        "after_write",
        "after_file_fsync",
        "after_replace",
        "after_directory_fsync",
        "before_sink",
    ),
)
def test_swallowed_reentrancy_before_sink_never_emits_command(
    tmp_path: Path, access: str, stage: str
) -> None:
    root = tmp_path / "pre-sink"
    client = _session(root)
    sent: list[Any] = []

    def hook(current: str) -> None:
        if current == stage:
            with suppress(RecoveryError):
                getattr(client, access)()

    try:
        with pytest.raises(RecoveryError):
            client.submit_new(_new(), timestamp_ns=10, sink=sent.append, hook=hook)
        with pytest.raises(RecoveryError):
            client.snapshot()
        assert sent == []
    finally:
        client.close()
    with _session(root, reopen=True) as recovered:
        state = recovered.snapshot()
        assert state["entry_enabled"] is False
        assert len(state["outbound"]) in (0, 1)
        if state["outbound"]:
            assert not _work(state)["terminal"]
            assert state["knowledge"]["inventory_max"] == 7


_KILLED_CANCEL_CLIENT = r"""
import json, os, signal, socket, sys
from pathlib import Path
from lobmm.outbound_recovery import OutboundRecoverySessionV2
client = OutboundRecoverySessionV2.open(Path(sys.argv[1]), "independent-account", "independent-recovery", "independent-review")
def sink(request):
    with socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=3) as connection:
        connection.sendall(b'{"op":"cancel"}\n')
        with connection.makefile("rb") as stream:
            assert json.loads(stream.readline()) == {"unknown": False, "remaining": 5}
    os.kill(os.getpid(), signal.SIGKILL)
client.recover_cancel("independent-seven", timestamp_ns=30, sink=sink)
raise AssertionError("client survived requested kill")
"""


def test_real_cancel_death_repeats_request_without_claiming_single_venue_effect(
    tmp_path: Path,
    pytestconfig: pytest.Config,
) -> None:
    import select
    from dataclasses import asdict

    venue = subprocess.Popen(
        [sys.executable, "-u", "-c", _SCALAR_VENUE],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert venue.stdout is not None
        assert select.select([venue.stdout], [], [], 5)[0]
        port = int(venue.stdout.readline())
        root = tmp_path / "dead-cancel"
        request = _new()
        with _session(root) as live:
            live.submit_new(
                request,
                timestamp_ns=10,
                sink=lambda command: _venue_rpc(
                    port, {"op": "new", "request": asdict(command)}
                ),
            )
        _venue_rpc(port, {"op": "trade", "quantity": 2})
        killed = subprocess.run(
            [sys.executable, "-c", _KILLED_CANCEL_CLIENT, str(root), str(port)],
            timeout=10,
            check=False,
            capture_output=True,
            text=True,
        )
        assert killed.returncode == -9, killed.stderr
        with _session(root, reopen=True) as client:
            assert _work(client.snapshot())["attempts"] == 1
            assert not _work(client.snapshot())["terminal"]
            replies: list[dict[str, Any]] = []
            client.recover_cancel(
                request.client_order_id,
                timestamp_ns=35,
                sink=lambda _: replies.append(_venue_rpc(port, {"op": "cancel"})),
            )
            assert replies == [{"unknown": False, "remaining": 5}]
            assert _work(client.snapshot())["attempts"] == 2
            assert not _work(client.snapshot())["terminal"]
            client.capture(Receipt("terminal", 41, _cancelled(request)))
            _venue_rpc(port, {"op": "trade", "quantity": 5})
            state = _venue_rpc(port, {"op": "state"})
            assert state["new_count"] == 1
            assert state["cancel_count"] == 2
            assert state["order"]["filled"] == 2
            assert state["order"]["cash"] == -226
            assert client.snapshot()["knowledge"]["inventory_min"] == 2
            assert client.snapshot()["ledger"]["known_inventory"] == 0
            assert _work(client.snapshot())["terminal"]
            _publish_independent(
                pytestconfig,
                tmp_path,
                "cancel-repeated-after-real-death",
                {
                    "process_deaths": 1,
                    "venue": state,
                    "client": client.snapshot(),
                    "journal": json.loads((root / "journal.json").read_text()),
                },
            )
    finally:
        venue.terminate()
        venue.communicate(timeout=5)


def test_global_outbound_limit_exhausts_below_per_order_retry_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "global-limit"
    with _session(root) as client:
        for index in range(64):
            client.submit_new(
                _new(cid=f"limit-{index}"), timestamp_ns=10, sink=lambda _: None
            )
        for attempt in range(3):
            for index in range(64):
                client.recover_cancel(
                    f"limit-{index}", timestamp_ns=20 + attempt, sink=lambda _: None
                )
        state = client.snapshot()
        assert len(state["outbound"]) == 256
        assert all(
            row["attempts"] == 3 and row["exhausted"] and not row["terminal"]
            for row in state["cancellation_work"]
        )
        assert state["knowledge"]["inventory_max"] == 448
        with pytest.raises(RecoveryError):
            client.recover_cancel(
                "limit-0", timestamp_ns=30, sink=lambda _: pytest.fail("exhausted send")
            )
    with _session(root, reopen=True) as recovered:
        assert recovered.snapshot() == state | {"entry_enabled": False}


def test_order_limit_does_not_publish_or_dispatch_sixty_fifth_new(
    tmp_path: Path,
) -> None:
    root = tmp_path / "order-limit"
    sent: list[Any] = []
    with _session(root) as client:
        for index in range(64):
            client.submit_new(
                _new(cid=f"order-{index}"), timestamp_ns=10, sink=sent.append
            )
        state = client.snapshot()
        with pytest.raises(RecoveryError):
            client.submit_new(_new(cid="order-64"), timestamp_ns=10, sink=sent.append)
    with _session(root, reopen=True) as recovered:
        assert recovered.snapshot() == state | {"entry_enabled": False}
    assert len(sent) == 64


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


@pytest.mark.parametrize(
    "mutation",
    (
        "version",
        "account",
        "session",
        "strategy",
        "extra_field",
        "bool_quantity",
        "outbound_limit",
        "receipt_limit",
        "attempt_limit",
        "cancel_venue_address",
        "cancel_before_new",
        "reused_new",
        "backward_dispatch",
        "byte_limit",
    ),
)
def test_recomputed_checksum_cannot_hide_invalid_schema_context_or_limits(
    tmp_path: Path, mutation: str
) -> None:
    import copy
    import hashlib

    root = tmp_path / mutation
    with _session(root) as client:
        client.submit_new(_new(), timestamp_ns=10, sink=lambda _: None)
        client.recover_cancel("independent-seven", timestamp_ns=12, sink=lambda _: None)
    path = root / "journal.json"
    envelope = json.loads(path.read_text())
    document = envelope["journal"]
    if mutation == "version":
        document["version"] = "unreviewed-v3"
    elif mutation in {"account", "session", "strategy"}:
        document[mutation + "_id"] = "other"
    elif mutation == "extra_field":
        document["cursor"] = 1
    elif mutation == "bool_quantity":
        document["outbound"][0]["payload"]["quantity"] = True
    elif mutation == "outbound_limit":
        document["outbound"] *= 129
    elif mutation == "receipt_limit":
        document["receipts"] = [{}] * 1025
    elif mutation == "attempt_limit":
        document["outbound"] = [document["outbound"][0]] + [
            copy.deepcopy(document["outbound"][1]) for _ in range(5)
        ]
    elif mutation == "cancel_venue_address":
        document["outbound"][1]["payload"].update(
            order_id="venue-independent-seven", client_order_id=None
        )
    elif mutation == "cancel_before_new":
        document["outbound"] = list(reversed(document["outbound"]))
    elif mutation == "reused_new":
        document["outbound"] = [document["outbound"][0]] * 2
    elif mutation == "backward_dispatch":
        document["outbound"][1]["dispatch_timestamp_ns"] = 9
    envelope["sha256"] = hashlib.sha256(_canonical(document)).hexdigest()
    encoded = (
        b" " * (4 * 1024 * 1024 + 1)
        if mutation == "byte_limit"
        else _canonical(envelope)
    )
    path.write_bytes(encoded)
    with pytest.raises(RecoveryError):
        _session(root, reopen=True)
    assert path.read_bytes() == encoded


@pytest.mark.parametrize("operation", ("new", "cancel", "capture"))
@pytest.mark.parametrize("failure", ("file_fsync", "replace", "directory_fsync"))
def test_storage_failure_poisons_and_disk_reopen_uses_only_complete_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    import lobmm.durable_recovery as durable

    root = tmp_path / "storage"
    client = _session(root)
    request = _new()
    if operation != "new":
        client.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    calls = 0
    original_fsync = os.fsync

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected replace failure")

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == (1 if failure == "file_fsync" else 2):
            raise OSError("injected fsync failure")
        original_fsync(fd)

    try:
        with monkeypatch.context() as patch:
            if failure == "replace":
                patch.setattr(durable.os, "replace", fail_replace)
            else:
                patch.setattr(durable.os, "fsync", fsync)
            with pytest.raises((RecoveryError, OSError)):
                if operation == "new":
                    client.submit_new(
                        request,
                        timestamp_ns=10,
                        sink=lambda _: pytest.fail("undurable command dispatched"),
                    )
                elif operation == "cancel":
                    client.recover_cancel(
                        request.client_order_id,
                        timestamp_ns=12,
                        sink=lambda _: pytest.fail("undurable cancel dispatched"),
                    )
                else:
                    client.capture(Receipt("accepted", 12, _accepted(request)))
        with pytest.raises(RecoveryError):
            client.snapshot()
    finally:
        client.close()
    with _session(root, reopen=True) as recovered:
        state = recovered.snapshot()
        committed = int(failure == "directory_fsync")
        assert len(state["outbound"]) == (
            committed
            if operation == "new"
            else 1 + committed * int(operation == "cancel")
        )
        assert len(state["receipts"]) == committed * int(operation == "capture")
        assert state["entry_enabled"] is False


@pytest.mark.parametrize("source", ("report", "query"))
@pytest.mark.parametrize("terminal_first", (False, True))
def test_observed_live_state_after_terminal_is_a_conflicting_effect(
    tmp_path: Path, source: str, terminal_first: bool
) -> None:
    root = tmp_path / "revived"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    with _session(root, reopen=True) as client:
        live_payload: ExecutionReport | ReconciliationResponse = _accepted(
            request, timestamp=50
        )
        if source == "query":
            original = _response(request, status=OrderStatus.LIVE, remaining=7)
            live_payload = replace(
                original,
                request_timestamp_ns=51,
                exchange_snapshot_timestamp_ns=52,
                as_of_timestamp_ns=52,
                orders=(replace(original.orders[0], state_timestamp_ns=50),),
            )
            # Bind this exact venue identity before the query-only state.
            client.capture(Receipt("accepted-identity", 12, _accepted(request)))
        terminal = Receipt(
            "terminal", 41 if terminal_first else 61, _cancelled(request)
        )
        revived = Receipt("revived", 51 if source == "report" else 53, live_payload)
        client.capture(terminal if terminal_first else revived)
        before = client.snapshot()
        with pytest.raises(RecoveryError):
            client.capture(revived if terminal_first else terminal)
        with pytest.raises(RecoveryError):
            client.snapshot()
    with _session(root, reopen=True) as reopened:
        assert reopened.snapshot() == before


@pytest.mark.parametrize("first_cancelled", (False, True))
def test_terminal_status_kind_cannot_change_with_same_execution_quantity(
    tmp_path: Path, first_cancelled: bool
) -> None:
    root = tmp_path / "terminal-kind"
    request = _new()
    with _session(root) as live:
        live.submit_new(request, timestamp_ns=10, sink=lambda _: None)
    cancelled = _cancelled(request, remaining=7)
    expired = replace(
        cancelled,
        report_id="expired",
        report_type=ReportType.EXPIRED,
        order_status=OrderStatus.EXPIRED,
        exchange_timestamp_ns=50,
    )
    with _session(root, reopen=True) as client:
        client.capture(Receipt("first", 60, cancelled if first_cancelled else expired))
        before = client.snapshot()
        with pytest.raises(RecoveryError):
            client.capture(
                Receipt("second", 61, expired if first_cancelled else cancelled)
            )
        with pytest.raises(RecoveryError):
            client.snapshot()
    with _session(root, reopen=True) as reopened:
        assert reopened.snapshot() == before


def _publish_independent(
    config: pytest.Config, temporary: Path, name: str, data: dict[str, Any]
) -> None:
    import hashlib

    configured = config.getoption("--outbound-recovery-output")
    destination = (
        Path(configured) / "independent" if configured else temporary / "independent"
    )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (name + ".json")).write_text(
        json.dumps(data, sort_keys=True, indent=2) + "\n"
    )
    witnesses = sorted(
        path for path in destination.glob("*.json") if path.name != "audit.json"
    )
    audit = {
        "version": "independent-outbound-recovery-v2",
        "synthetic_uncalibrated": True,
        "case_count": len(witnesses),
        "process_deaths": sum(
            json.loads(path.read_text())["process_deaths"] for path in witnesses
        ),
        "artifact_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in witnesses
        },
        "source_sha256": {
            "independent_tests": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        },
    }
    (destination / "audit.json").write_text(
        json.dumps(audit, sort_keys=True, indent=2) + "\n"
    )
