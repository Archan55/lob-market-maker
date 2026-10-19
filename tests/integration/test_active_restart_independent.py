"""Independent guarded-restart review, with a separate seven-unit witness.

No primary study fixture or verdict is imported. Literal signed quantity and
cash expectations below come from a two-unit fill followed by five units.
These disk/reopen and fork controls supplement the primary SIGKILL study; they
do not claim a restarted strategy or exactly-once exchange effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lobmm.active_restart import GuardedStrategySession
from lobmm.config import StrategyConfig
from lobmm.durable_recovery import DurableClient, Receipt, RecoveryError, write_inbox
from lobmm.enums import EventType, LiquidityRole, OrderStatus, ReportType, Side
from lobmm.events import MarketEvent
from lobmm.orders import CancelRequest, ExecutionReport, Fill, NewOrderRequest
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse
from lobmm.strategies import make_strategy


def _strategy() -> Any:
    return make_strategy(StrategyConfig(strategy_id="independent", order_size=7))


def _session(path: Path, *, reopen: bool = False) -> GuardedStrategySession:
    constructor = (
        GuardedStrategySession.open if reopen else GuardedStrategySession.create
    )
    return constructor(
        path,
        "independent-active-session",
        strategy=_strategy(),
        max_abs_inventory=21,
    )


def _intent(side: Side = Side.BID) -> NewOrderRequest:
    return NewOrderRequest("independent-seven", side, 113, 7, "independent", 5, 10)


def _evidence(
    root: Path, intents: list[NewOrderRequest], receipts: list[Receipt]
) -> DurableClient:
    root.mkdir()
    inbox, checkpoint = root / "inbox.json", root / "checkpoint.json"
    digest = write_inbox(inbox, "independent-active-session", intents, receipts)
    client = DurableClient.create(
        checkpoint, inbox, "independent-active-session", digest
    )
    while client.advance():
        pass
    return DurableClient.open(checkpoint, inbox, "independent-active-session", digest)


def _market() -> tuple[MarketEvent, MarketEvent]:
    return (
        MarketEvent(1, 1, EventType.ADD, Side.BID, 112, 30),
        MarketEvent(1, 2, EventType.ADD, Side.ASK, 116, 30),
    )


def test_reopen_blocks_cached_and_external_commands_before_strategy_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    live = _session(path)
    for event in _market():
        live.deliver_market(event, notification_timestamp_ns=1)
    actions = live.decide(timestamp_ns=10)
    assert len(actions) == 2
    sink: list[Any] = []
    live.dispatch(actions[0], timestamp_ns=10, sink=sink.append)
    before = path.read_bytes()
    recovered = _session(path, reopen=True)
    recovered_before = recovered.snapshot()
    for quotes_allowed in (True, False):
        with pytest.raises(RecoveryError):
            recovered.decide(timestamp_ns=20, allow_new_quotes=quotes_allowed)
    for command in (
        actions[0],
        actions[1],
        CancelRequest(20, 20, client_order_id=actions[0].client_order_id),
    ):
        with pytest.raises(RecoveryError):
            recovered.dispatch(command, timestamp_ns=20, sink=sink.append)
    with pytest.raises(RecoveryError):
        recovered.deliver_market(_market()[0], notification_timestamp_ns=20)
    assert len(sink) == 1
    assert path.read_bytes() == before
    assert recovered.snapshot() == recovered_before
    # A second reopen cannot mistake inspection for successful restoration.
    with pytest.raises(RecoveryError):
        _session(path, reopen=True).decide(timestamp_ns=21)


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_unknown_cancel_and_lost_detail_remain_unresolved_until_late_fill(
    tmp_path: Path, side: Side
) -> None:
    intent = _intent(side)
    path = tmp_path / "active.json"
    live = _session(path)
    sent: list[Any] = []
    live.dispatch(intent, timestamp_ns=10, sink=sent.append)
    cancel = CancelRequest(12, 12, client_order_id=intent.client_order_id)
    live.dispatch(cancel, timestamp_ns=12, sink=sent.append)
    recovered = _session(path, reopen=True)
    unknown = ExecutionReport(
        "unknown-cancel",
        ReportType.CANCEL_REJECTED,
        13,
        "",
        intent.client_order_id,
        OrderStatus.REJECTED,
        0,
        reason="unknown_order",
    )
    unknown_client = _evidence(
        tmp_path / "unknown", [intent], [Receipt("unknown-envelope", 14, unknown)]
    )
    assert unknown_client.snapshot()["knowledge"] == {
        "known_inventory": 0,
        "inventory_min": 0 if side is Side.BID else -7,
        "inventory_max": 7 if side is Side.BID else 0,
        "complete": False,
        "orders": [
            {
                "client_order_id": intent.client_order_id,
                "side": int(side),
                "sent_quantity": 7,
                "notified_fill_quantity": 0,
                "executed_quantity_min": 0,
                "executed_quantity_max": 7,
                "terminal_report_received": False,
                "reservation_quantity": 7,
                "missing_fill_quantity_min": 0,
            }
        ],
    }
    unknown_assessment = recovered.assessment(unknown_client)
    assert unknown_assessment["resume_allowed"] is False
    assert unknown_assessment["inventory_min"] == (0 if side is Side.BID else -7)
    assert unknown_assessment["inventory_max"] == (7 if side is Side.BID else 0)
    assert (
        unknown_assessment["potentially_outstanding"]
        == unknown_client.snapshot()["knowledge"]["orders"]
    )
    assert unknown_assessment["missing_execution_detail"] == []
    assert len(unknown_assessment["cancellation_reservations"]) == 1
    assert (
        unknown_assessment["cancellation_reservations"][0]["payload"]["client_order_id"]
        == intent.client_order_id
    )
    first = Fill(
        "two-units",
        "venue-seven",
        intent.client_order_id,
        "independent",
        side,
        2,
        113,
        LiquidityRole.MAKER,
        20,
        Decimal("0.19"),
        Decimal("0.01"),
    )
    last = replace(
        first,
        fill_id="five-units",
        quantity=5,
        exchange_fill_timestamp_ns=30,
        fee=Decimal("0.31"),
    )
    terminal = ReconciliationResponse(
        "terminal-query",
        31,
        32,
        32,
        100,
        True,
        (
            OrderEvidence(
                intent.client_order_id, 30, OrderStatus.FILLED, 0, (first,), False
            ),
        ),
    )
    receipts = [
        Receipt("unknown-envelope", 14, unknown),
        Receipt("terminal-envelope", 40, terminal),
    ]
    incomplete = _evidence(tmp_path / "terminal-missing", [intent], receipts)
    state = incomplete.snapshot()
    assert state["ledger"] == {
        "known_inventory": 2 * int(side),
        "trade_cash_ticks": -226 * int(side),
        "fees": "0.19",
        "rebates": "0.01",
    }
    assert state["knowledge"]["inventory_min"] == 7 * int(side)
    assert state["knowledge"]["inventory_max"] == 7 * int(side)
    assert state["knowledge"]["orders"][0]["missing_fill_quantity_min"] == 5
    missing_assessment = recovered.assessment(incomplete)
    assert missing_assessment["resume_allowed"] is False
    assert missing_assessment["known_ledger"] == {
        "known_inventory": 2 * int(side),
        "trade_cash_ticks": -226 * int(side),
        "fees": "0.19",
        "rebates": "0.01",
    }
    assert missing_assessment["potentially_outstanding"] == []
    assert (
        missing_assessment["missing_execution_detail"][0]["missing_fill_quantity_min"]
        == 5
    )
    assert len(missing_assessment["cancellation_reservations"]) == 1
    late = ExecutionReport(
        "late-five",
        ReportType.FILL,
        30,
        "venue-seven",
        intent.client_order_id,
        OrderStatus.FILLED,
        0,
        last,
    )
    receipts += [
        Receipt("late-envelope", 50, late),
        Receipt("late-copy", 60, replace(late, report_id="late-copy")),
    ]
    complete = _evidence(tmp_path / "terminal-complete", [intent], receipts)
    assert complete.snapshot()["ledger"] == {
        "known_inventory": 7 * int(side),
        "trade_cash_ticks": -791 * int(side),
        "fees": "0.50",
        "rebates": "0.02",
    }
    assert complete.snapshot()["knowledge"]["complete"] is True
    complete_assessment = recovered.assessment(complete)
    assert complete_assessment["resume_allowed"] is False
    assert complete_assessment["saved_order_evidence_complete"] is True
    assert complete_assessment["known_ledger"] == {
        "known_inventory": 7 * int(side),
        "trade_cash_ticks": -791 * int(side),
        "fees": "0.50",
        "rebates": "0.02",
    }
    assert complete_assessment["potentially_outstanding"] == []
    assert complete_assessment["missing_execution_detail"] == []
    assert len(complete_assessment["cancellation_reservations"]) == 1
    # Exact terminal quantity and economics close this evidence set only.
    for command in (intent, cancel):
        with pytest.raises(RecoveryError):
            recovered.dispatch(command, timestamp_ns=70, sink=sent.append)
    with pytest.raises(RecoveryError):
        recovered.decide(timestamp_ns=70)
    assert sent == [intent, cancel]


@pytest.mark.parametrize("mismatch", ("missing", "extra", "changed"))
def test_assessment_requires_exact_reserved_intents(
    tmp_path: Path, mismatch: str
) -> None:
    first = _intent()
    second = replace(first, client_order_id="independent-second", side=Side.ASK)
    live = _session(tmp_path / "active.json")
    live.dispatch(first, timestamp_ns=10, sink=lambda _: None)
    live.dispatch(second, timestamp_ns=10, sink=lambda _: None)
    intents = [first, second]
    if mismatch == "missing":
        intents.pop()
    elif mismatch == "extra":
        intents.append(replace(first, client_order_id="unreserved-third"))
    else:
        intents[0] = replace(first, price_ticks=114)
    client = _evidence(tmp_path / "evidence", intents, [])
    with pytest.raises(RecoveryError):
        _session(tmp_path / "active.json", reopen=True).assessment(client)


def test_duplicate_identity_and_cancel_do_not_reach_sink(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    intent = _intent()
    sink: list[Any] = []
    session.dispatch(intent, timestamp_ns=10, sink=sink.append)
    committed = path.read_bytes()
    for duplicate in (intent, replace(intent, quantity=6)):
        with pytest.raises(RecoveryError):
            session.dispatch(duplicate, timestamp_ns=11, sink=sink.append)
    assert sink == [intent]
    assert path.read_bytes() == committed
    cancel = CancelRequest(12, 12, client_order_id=intent.client_order_id)
    session.dispatch(cancel, timestamp_ns=12, sink=sink.append)
    with pytest.raises(RecoveryError):
        session.dispatch(cancel, timestamp_ns=13, sink=sink.append)
    assert sink == [intent, cancel]


def test_base_exception_during_sink_leaves_reserved_poisoned_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    intent = _intent()
    effects: list[Any] = []

    def interrupted(command: Any) -> None:
        effects.append(command)
        raise KeyboardInterrupt("independent ambiguous sink effect")

    with pytest.raises((RecoveryError, KeyboardInterrupt)):
        session.dispatch(intent, timestamp_ns=10, sink=interrupted)
    assert effects == [intent]
    for command in (intent, replace(intent, client_order_id="second-after-error")):
        with pytest.raises(RecoveryError):
            session.dispatch(command, timestamp_ns=11, sink=effects.append)
    with pytest.raises(RecoveryError):
        session.decide(timestamp_ns=11)
    assert effects == [intent]
    recovered = _session(path, reopen=True)
    recovered.assessment(_evidence(tmp_path / "evidence", [intent], []))


def test_inherited_writer_cannot_emit_or_commit_in_child(tmp_path: Path) -> None:
    # Pytest may already own Polars threads. The actual inheritance witness
    # belongs in a clean interpreter; neither pipe read nor subprocess can wait
    # indefinitely if an implementation failure prevents the child response.
    script = r"""
import json
import os
import select
import signal
import sys
from pathlib import Path

from lobmm.active_restart import GuardedStrategySession
from lobmm.config import StrategyConfig
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import NewOrderRequest
from lobmm.strategies import make_strategy

path = Path(sys.argv[1])
strategy = make_strategy(StrategyConfig(strategy_id="independent", order_size=7))
session = GuardedStrategySession.create(
    path, "independent-active-session", strategy, 21
)
intent = NewOrderRequest(
    "independent-seven", Side.BID, 113, 7, "independent", 5, 10
)
before = path.read_bytes()
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(read_fd)
    result = {"sink_calls": 0, "blocked": False}

    def child_sink(_):
        result["sink_calls"] += 1

    try:
        session.dispatch(intent, timestamp_ns=10, sink=child_sink)
    except RecoveryError:
        result["blocked"] = True
    finally:
        os.write(write_fd, json.dumps(result).encode())
        os.close(write_fd)
        os._exit(0)
os.close(write_fd)
reaped = False
try:
    assert select.select([read_fd], [], [], 5)[0], "forked writer did not answer"
    result = json.loads(os.read(read_fd, 4096))
    _, status = os.waitpid(child, 0)
    reaped = True
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == {"sink_calls": 0, "blocked": True}
    assert path.read_bytes() == before
    # Rejecting inherited authority must leave the originating writer usable.
    sink = []
    session.dispatch(intent, timestamp_ns=10, sink=sink.append)
    assert sink == [intent]
    print(json.dumps({"child": result, "originating_sink_calls": len(sink)}))
finally:
    os.close(read_fd)
    if not reaped:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            script,
            str(tmp_path / "active.json"),
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "child": {"sink_calls": 0, "blocked": True},
        "originating_sink_calls": 1,
    }


def _rewrite_journal(path: Path, envelope: dict[str, Any]) -> None:
    """Recompute the accidental-corruption checksum independently."""
    import hashlib

    journal_bytes = (
        json.dumps(envelope["journal"], sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    envelope["sha256"] = hashlib.sha256(journal_bytes).hexdigest()
    path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")


def test_wrong_strategy_command_cannot_use_bound_session_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    before = path.read_bytes()
    sink: list[Any] = []
    wrong = replace(_intent(), strategy_id="another-owner")
    with pytest.raises(RecoveryError):
        session.dispatch(wrong, timestamp_ns=10, sink=sink.append)
    assert sink == []
    assert path.read_bytes() == before
    # Input rejection must not poison an otherwise valid local writer.
    session.dispatch(_intent(), timestamp_ns=10, sink=sink.append)
    assert sink == [_intent()]


def test_recomputed_checksum_cannot_rebind_persisted_command_strategy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    session.dispatch(_intent(), timestamp_ns=10, sink=lambda _: None)
    envelope = json.loads(path.read_bytes())
    envelope["journal"]["commands"][0]["payload"]["strategy_id"] = "another-owner"
    _rewrite_journal(path, envelope)
    with pytest.raises(RecoveryError):
        _session(path, reopen=True)


def test_recomputed_checksum_does_not_bypass_context_schema_or_scalar_checks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    session.dispatch(_intent(), timestamp_ns=10, sink=lambda _: None)
    original = path.read_bytes()
    mutations = (
        ("version", "active-restart-boundary-v999"),
        ("context_sha256", "0" * 64),
        ("session_id", "other-session"),
        ("commands", "not-a-command-list"),
        ("unknown_journal_field", 1),
    )
    for field, value in mutations:
        envelope = json.loads(original)
        envelope["journal"][field] = value
        _rewrite_journal(path, envelope)
        with pytest.raises(RecoveryError):
            _session(path, reopen=True)
    command_mutations = (
        ("dispatch_timestamp_ns", True),
        ("dispatch_timestamp_ns", 10.0),
        ("dispatch_timestamp_ns", 9),
        ("kind", "replace"),
        ("unknown_command_field", 1),
    )
    for field, value in command_mutations:
        envelope = json.loads(original)
        envelope["journal"]["commands"][0][field] = value
        _rewrite_journal(path, envelope)
        with pytest.raises(RecoveryError):
            _session(path, reopen=True)
    path.write_bytes(original)
    assert _session(path, reopen=True).snapshot()["quarantined"] is True


def test_cancel_scalar_rejection_precedes_reservation_and_sink(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    session = _session(path)
    sink: list[Any] = []
    before = path.read_bytes()
    malformed = (
        CancelRequest(True, 10, client_order_id="independent-seven"),
        CancelRequest(0, True, client_order_id="independent-seven"),
        CancelRequest(10.0, 10, client_order_id="independent-seven"),
        CancelRequest(10, 10.0, client_order_id="independent-seven"),
    )
    for command in malformed:
        with pytest.raises(RecoveryError):
            session.dispatch(command, timestamp_ns=10, sink=sink.append)
        assert sink == []
        assert path.read_bytes() == before
    valid = CancelRequest(10, 10, client_order_id="independent-seven")
    session.dispatch(valid, timestamp_ns=10, sink=sink.append)
    assert sink == [valid]
    committed = path.read_bytes()
    for field in ("decision_timestamp_ns", "send_timestamp_ns"):
        envelope = json.loads(committed)
        envelope["journal"]["commands"][0]["payload"][field] = True
        _rewrite_journal(path, envelope)
        with pytest.raises(RecoveryError):
            _session(path, reopen=True)


def test_complete_flat_evidence_cannot_restore_lost_active_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    live = _session(path)
    bid = _intent()
    ask = replace(bid, client_order_id="independent-ask", side=Side.ASK)
    sent: list[Any] = []
    receipts = []
    for index, intent in enumerate((bid, ask)):
        live.dispatch(intent, timestamp_ns=10, sink=sent.append)
        fill = Fill(
            f"flat-fill-{index}",
            f"flat-venue-{index}",
            intent.client_order_id,
            "independent",
            intent.side,
            7,
            113,
            LiquidityRole.MAKER,
            20 + index,
        )
        report = ExecutionReport(
            f"flat-report-{index}",
            ReportType.FILL,
            20 + index,
            fill.order_id,
            intent.client_order_id,
            OrderStatus.FILLED,
            0,
            fill,
        )
        receipts.append(Receipt(f"flat-envelope-{index}", 30 + index, report))
    client = _evidence(tmp_path / "flat-evidence", [bid, ask], receipts)
    recovered = _session(path, reopen=True)
    assessment = recovered.assessment(client)
    assert assessment["known_ledger"] == {
        "known_inventory": 0,
        "trade_cash_ticks": 0,
        "fees": "0",
        "rebates": "0",
    }
    assert assessment["saved_order_evidence_complete"] is True
    assert assessment["supplied_inbox_drained"] is True
    assert assessment["potentially_outstanding"] == []
    assert assessment["missing_execution_detail"] == []
    assert assessment["inventory_min"] == assessment["inventory_max"] == 0
    assert assessment["resume_allowed"] is False
    assert "risk_and_stop_state" in assessment["unresolved_runtime"]
    assert "complete_account_order_universe" in assessment["unresolved_runtime"]
    with pytest.raises(RecoveryError):
        recovered.decide(timestamp_ns=40)
    assert sent == [bid, ask]


def test_real_storage_failures_never_call_sink_and_require_quarantined_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four actual I/O failures; replacement determines the old/new journal."""
    import stat

    import lobmm.durable_recovery as recovery

    original_fdopen = os.fdopen
    original_fsync = os.fsync

    class PartialWriter:
        def __init__(self, stream: Any) -> None:
            self.stream = stream
            self.calls = 0

        def __enter__(self) -> PartialWriter:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            self.stream.__exit__(*args)

        def write(self, value: bytes) -> int:
            self.calls += 1
            if self.calls == 2:
                raise OSError("independent partial stream.write failure")
            return self.stream.write(value)

        def flush(self) -> None:
            self.stream.flush()

        def fileno(self) -> int:
            return self.stream.fileno()

    def failed_writer(*args: Any, **kwargs: Any) -> PartialWriter:
        return PartialWriter(original_fdopen(*args, **kwargs))

    def failed_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("independent replace failure")

    for operation in ("write", "file_fsync", "replace", "directory_fsync"):
        root = tmp_path / operation
        root.mkdir()
        path = root / "active.json"
        writer = _session(path)
        old_bytes = path.read_bytes()
        sink: list[Any] = []

        def failing_fsync(fd: int, selected: str = operation) -> None:
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if (selected == "directory_fsync" and directory) or (
                selected == "file_fsync" and not directory
            ):
                raise OSError(f"independent {selected} failure")
            original_fsync(fd)

        with monkeypatch.context() as patch:
            if operation == "write":
                patch.setattr(recovery.os, "fdopen", failed_writer)
            elif operation == "replace":
                patch.setattr(recovery.os, "replace", failed_replace)
            else:
                patch.setattr(recovery.os, "fsync", failing_fsync)
            with pytest.raises(OSError, match="independent"):
                writer.dispatch(_intent(), timestamp_ns=10, sink=sink.append)
        assert sink == []
        with pytest.raises(RecoveryError):
            writer.snapshot()
        with pytest.raises(RecoveryError):
            writer.decide(timestamp_ns=11)
        with pytest.raises(RecoveryError):
            writer.dispatch(
                replace(_intent(), client_order_id="later-intent"),
                timestamp_ns=11,
                sink=sink.append,
            )
        recovered = _session(path, reopen=True)
        state = recovered.snapshot()
        assert state["quarantined"] is True
        if operation == "directory_fsync":
            assert path.read_bytes() != old_bytes
            assert len(state["commands"]) == 1
            assert (
                state["commands"][0]["payload"]["client_order_id"]
                == "independent-seven"
            )
        else:
            assert path.read_bytes() == old_bytes
            assert state["commands"] == []
        with pytest.raises(RecoveryError):
            recovered.dispatch(_intent(), timestamp_ns=11, sink=sink.append)
        assert sink == []
        assert list(root.glob("active.json.pending-*")) == []


def test_command_and_byte_limits_preserve_committed_state_and_block_sink(
    tmp_path: Path,
) -> None:
    path = tmp_path / "count.json"
    session = _session(path)
    sink: list[Any] = []
    # Exercise the published real bound, rather than patching it down for tests.
    for index in range(256):
        session.dispatch(
            replace(_intent(), client_order_id=f"bounded-{index}"),
            timestamp_ns=10,
            sink=sink.append,
        )
    assert len(sink) == 256
    committed = path.read_bytes()
    with pytest.raises(RecoveryError):
        session.dispatch(
            replace(_intent(), client_order_id="over-count"),
            timestamp_ns=10,
            sink=sink.append,
        )
    assert len(sink) == 256
    assert path.read_bytes() == committed
    assert len(_session(path, reopen=True).snapshot()["commands"]) == 256
    envelope = json.loads(committed)
    envelope["journal"]["commands"].append(envelope["journal"]["commands"][0])
    _rewrite_journal(path, envelope)
    with pytest.raises(RecoveryError):
        _session(path, reopen=True)

    huge_path = tmp_path / "bytes.json"
    writer = _session(huge_path)
    old = huge_path.read_bytes()
    effects: list[Any] = []
    huge = replace(_intent(), client_order_id="x" * (4 * 1024 * 1024))
    with pytest.raises(RecoveryError):
        writer.dispatch(huge, timestamp_ns=10, sink=effects.append)
    assert effects == []
    assert huge_path.read_bytes() == old
    with pytest.raises(RecoveryError):
        writer.dispatch(_intent(), timestamp_ns=11, sink=effects.append)
    assert _session(huge_path, reopen=True).snapshot()["commands"] == []
    huge_path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(RecoveryError):
        _session(huge_path, reopen=True)


def test_unreadable_state_never_creates_or_exposes_writer_and_constructor_is_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    with pytest.raises(RecoveryError):
        _session(path, reopen=True)
    assert not path.exists()
    with pytest.raises(RecoveryError):
        GuardedStrategySession(path, "independent-active-session", _strategy(), 21, 5)
    assert not path.exists()
    live = _session(path)
    live.dispatch(_intent(), timestamp_ns=10, sink=lambda _: None)
    original = path.read_bytes()
    envelope = json.loads(original)
    envelope["sha256"] = "0" * 64
    bad_checksum = json.dumps(envelope).encode()
    malformed = (
        b"",
        original[: len(original) // 2],
        bad_checksum,
        original.replace(b'"journal":', b'"journal":{},"journal":', 1),
    )
    for raw in malformed:
        path.write_bytes(raw)
        with pytest.raises(RecoveryError):
            _session(path, reopen=True)
        with pytest.raises(RecoveryError):
            _session(path)
        assert path.read_bytes() == raw
    path.write_bytes(original)
    assert _session(path, reopen=True).snapshot()["quarantined"] is True
    with pytest.raises(RecoveryError):
        _session(path)


def test_rollback_to_valid_old_journal_still_cannot_restore_command_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.json"
    live = _session(path)
    old_empty = path.read_bytes()
    effects: list[Any] = []
    live.dispatch(_intent(), timestamp_ns=10, sink=effects.append)
    old_one = path.read_bytes()
    live.dispatch(
        CancelRequest(11, 11, client_order_id="independent-seven"),
        timestamp_ns=11,
        sink=effects.append,
    )
    assert len(effects) == 2
    for old, count in ((old_empty, 0), (old_one, 1)):
        path.write_bytes(old)
        recovered = _session(path, reopen=True)
        assert recovered.snapshot()["quarantined"] is True
        assert len(recovered.snapshot()["commands"]) == count
        with pytest.raises(RecoveryError):
            recovered.decide(timestamp_ns=12)
        with pytest.raises(RecoveryError):
            recovered.dispatch(_intent(), timestamp_ns=12, sink=effects.append)
        assert len(effects) == 2
    # Deliberately no claim that a rolled-back journal still proves all sends.


def test_evidence_before_actual_dispatch_fails_closed(tmp_path: Path) -> None:
    """Valid intent-relative evidence can still predate actual guarded dispatch."""
    intent = _intent()
    fill = Fill(
        "pre-dispatch-fill",
        "pre-dispatch-venue",
        intent.client_order_id,
        "independent",
        Side.BID,
        7,
        113,
        LiquidityRole.MAKER,
        20,
    )
    report = ExecutionReport(
        "pre-dispatch-report",
        ReportType.FILL,
        20,
        fill.order_id,
        intent.client_order_id,
        OrderStatus.FILLED,
        0,
        fill,
    )
    query = ReconciliationResponse(
        "pre-dispatch-state",
        21,
        22,
        22,
        200,
        True,
        (OrderEvidence(intent.client_order_id, 20, OrderStatus.FILLED, 0, (), False),),
    )
    unknown = ExecutionReport(
        "pre-dispatch-unknown",
        ReportType.CANCEL_REJECTED,
        20,
        "",
        intent.client_order_id,
        OrderStatus.REJECTED,
        0,
        reason="unknown_order",
    )
    absent = replace(
        query,
        query_id="pre-dispatch-absence",
        orders=(OrderEvidence(intent.client_order_id, None, None, None, (), False),),
    )
    rejected = []
    accepted = []
    for label, payload in (("fill-report", report), ("terminal-query", query)):
        root = tmp_path / label
        root.mkdir()
        path = root / "active.json"
        session = _session(path)
        effects: list[Any] = []
        # The adapter sent this old cached intent at t100, not its requested t10.
        session.dispatch(intent, timestamp_ns=100, sink=effects.append)
        client = _evidence(
            root / "evidence", [intent], [Receipt("pre-dispatch-envelope", 30, payload)]
        )
        verified = client.snapshot()
        assert verified["knowledge"]["inventory_min"] == 7
        assert verified["knowledge"]["inventory_max"] == 7
        assert verified["ledger"]["known_inventory"] == (
            7 if label == "fill-report" else 0
        )
        recovered = _session(path, reopen=True)
        before = path.read_bytes()
        try:
            assessment = recovered.assessment(client)
        except RecoveryError:
            rejected.append(label)
        else:
            accepted.append((label, assessment["known_ledger"]))
        with pytest.raises(RecoveryError):
            recovered.decide(timestamp_ns=101)
        with pytest.raises(RecoveryError):
            recovered.dispatch(intent, timestamp_ns=101, sink=effects.append)
        assert effects == [intent]
        assert path.read_bytes() == before
        # Absence and unknown-order rejection assert no execution outcome. They
        # remain valid pre-dispatch observations and cannot remove the gate.
        for description, no_outcome in (("unknown", unknown), ("absent", absent)):
            no_outcome_client = _evidence(
                root / description, [intent], [Receipt(description, 30, no_outcome)]
            )
            result = recovered.assessment(no_outcome_client)
            assert result["known_ledger"]["known_inventory"] == 0
            assert result["inventory_min"] == 0
            assert result["inventory_max"] == 7
            assert result["resume_allowed"] is False
        after_dispatch = replace(
            report,
            report_id="valid-after-dispatch-report",
            exchange_timestamp_ns=120,
            fill=replace(fill, exchange_fill_timestamp_ns=120),
        )
        valid_client = _evidence(
            root / "valid-after-dispatch",
            [intent],
            [Receipt("valid-after-dispatch-envelope", 130, after_dispatch)],
        )
        valid_assessment = recovered.assessment(valid_client)
        assert valid_assessment["known_ledger"]["known_inventory"] == 7
        assert valid_assessment["saved_order_evidence_complete"] is True
        assert valid_assessment["resume_allowed"] is False
    assert rejected == ["fill-report", "terminal-query"], accepted
