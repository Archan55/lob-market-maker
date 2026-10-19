"""Independent durable-client review, with a separate 5-unit 2+3 witness.

This file imports no study oracle or fixture. Expected accounting, receipt-ID
unions and terminal bounds are specified from the immutable fixture below.
These are ordinary disk/reopen controls; actual process-death evidence lives
in the dedicated subprocess study and is not claimed by these tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

import lobmm.durable_recovery as recovery
from lobmm.durable_recovery import DurableClient, Receipt, RecoveryError, write_inbox
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse


def _fixture(side: Side = Side.BID) -> tuple[NewOrderRequest, list[Receipt]]:
    price = 107 if side is Side.BID else 109
    intent = NewOrderRequest("independent-five", side, price, 5, "review", 5, 10)
    first = Fill(
        "detail-two",
        "venue-five",
        intent.client_order_id,
        "review",
        side,
        2,
        price,
        LiquidityRole.MAKER,
        20,
        Decimal("0.17"),
        Decimal("0.03"),
    )
    second = replace(
        first,
        fill_id="detail-three",
        quantity=3,
        exchange_fill_timestamp_ns=40,
        fee=Decimal("0.23"),
        rebate=Decimal("0.04"),
    )
    old = ReconciliationResponse(
        "query-old",
        25,
        30,
        30,
        100,
        True,
        (
            OrderEvidence(
                intent.client_order_id,
                20,
                OrderStatus.PARTIALLY_FILLED,
                3,
                (first,),
                True,
            ),
        ),
    )
    newer = ReconciliationResponse(
        "query-new",
        45,
        50,
        50,
        100,
        True,
        (
            OrderEvidence(
                intent.client_order_id, 40, OrderStatus.FILLED, 0, (first,), False
            ),
        ),
    )
    part = ExecutionReport(
        "original-two",
        ReportType.PARTIAL_FILL,
        20,
        "venue-five",
        intent.client_order_id,
        OrderStatus.PARTIALLY_FILLED,
        3,
        first,
    )
    final = ExecutionReport(
        "original-three",
        ReportType.FILL,
        40,
        "venue-five",
        intent.client_order_id,
        OrderStatus.FILLED,
        0,
        second,
    )
    complete = replace(
        newer,
        query_id="query-complete",
        request_timestamp_ns=55,
        exchange_snapshot_timestamp_ns=60,
        as_of_timestamp_ns=60,
        orders=(
            replace(
                newer.orders[0],
                fills=(first, second),
                execution_detail_complete_as_of=True,
            ),
        ),
    )
    return intent, [
        Receipt("envelope-new", 60, newer),
        Receipt("envelope-old", 70, old),
        Receipt("envelope-old", 72, old),
        Receipt("envelope-copy", 75, newer),
        Receipt("envelope-part", 80, part),
        Receipt("envelope-final", 90, final),
        Receipt("envelope-final-copy", 95, replace(final, report_id="copy-three")),
        Receipt("envelope-complete", 100, complete),
    ]


def _files(
    tmp_path: Path, intent: NewOrderRequest, receipts: list[Receipt]
) -> tuple[Path, Path, str]:
    inbox, checkpoint = tmp_path / "inbox.json", tmp_path / "checkpoint.json"
    digest = write_inbox(inbox, "independent-session", [intent], receipts)
    return inbox, checkpoint, digest


def _open(checkpoint: Path, inbox: Path, digest: str) -> DurableClient:
    return DurableClient.open(checkpoint, inbox, "independent-session", digest)


@pytest.mark.parametrize("side", (Side.BID, Side.ASK))
def test_independent_scalar_bounds_identity_and_economics_across_every_restart(
    tmp_path: Path, side: Side
) -> None:
    intent, receipts = _fixture(side)
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
    seen_envelopes: set[str] = set()
    for offset, receipt in enumerate(receipts, 1):
        assert client.advance()
        seen_envelopes.add(receipt.receipt_id)
        client = _open(checkpoint, inbox, digest)
        state = client.snapshot()
        known = 2 if offset < 6 else 5
        assert client.offset == offset
        assert set(state["receipt_identities"]) == seen_envelopes
        assert set(
            state["fills"][i]["fill_id"] for i in range(len(state["fills"]))
        ) == ({"detail-two"} if known == 2 else {"detail-two", "detail-three"})
        assert state["notifications"] == (
            {"detail-two": 60} if known == 2 else {"detail-two": 60, "detail-three": 90}
        )
        knowledge = state["knowledge"]
        assert knowledge["known_inventory"] == int(side) * known
        assert knowledge["inventory_min"] == knowledge["inventory_max"] == int(side) * 5
        assert knowledge["complete"] == (known == 5)
        order = knowledge["orders"][0]
        assert order["executed_quantity_min"] == order["executed_quantity_max"] == 5
        assert order["notified_fill_quantity"] == known
        assert order["missing_fill_quantity_min"] == 5 - known
        assert order["reservation_quantity"] == 0
        assert order["terminal_report_received"]
        assert state["terminal_quantities"] == {intent.client_order_id: 5}
        assert state["terminal_times"] == {intent.client_order_id: 40}
        ledger = state["ledger"]
        assert ledger["known_inventory"] == int(side) * known
        assert ledger["trade_cash_ticks"] == -int(side) * known * intent.price_ticks
        assert Decimal(ledger["fees"]) == Decimal("0.17" if known == 2 else "0.40")
        assert Decimal(ledger["rebates"]) == Decimal("0.03" if known == 2 else "0.07")
        # The public result is detached from the committed replay witness.
        state["knowledge"]["inventory_min"] = 999
        assert client.snapshot()["knowledge"]["inventory_min"] == int(side) * 5
    assert not client.advance()
    assert set(client.snapshot()["query_identities"]) == {
        "query-new",
        "query-old",
        "query-complete",
    }
    assert set(client.snapshot()["report_identities"]) == {
        "original-two",
        "original-three",
        "copy-three",
    }


def test_exact_costs_and_restart_are_independent_of_decimal_context(
    tmp_path: Path,
) -> None:
    intent, receipts = _fixture()
    part = receipts[4].payload
    final = receipts[5].payload
    assert isinstance(part, ExecutionReport) and part.fill is not None
    assert isinstance(final, ExecutionReport) and final.fill is not None
    part = replace(
        part,
        fill=replace(part.fill, fee=Decimal("0.100001"), rebate=Decimal("0.010001")),
    )
    final = replace(
        final,
        fill=replace(final.fill, fee=Decimal("0.200002"), rebate=Decimal("0.020002")),
    )
    inbox, checkpoint, digest = _files(
        tmp_path, intent, [Receipt("two", 50, part), Receipt("three", 60, final)]
    )
    with localcontext() as context:
        context.prec = 4
        client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
        assert client.advance() and client.advance()
        ledger = client.snapshot()["ledger"]
        assert Decimal(ledger["fees"]) == Decimal("0.300003")
        assert Decimal(ledger["rebates"]) == Decimal("0.030003")
    assert _open(checkpoint, inbox, digest).offset == 2
    # A fixed precision alone would still inherit the caller's exponent traps.
    exponent_directory = tmp_path / "small-exponent-context"
    exponent_directory.mkdir()
    first_scaled = replace(part, fill=replace(part.fill, fee=Decimal("100001")))
    final_scaled = replace(final, fill=replace(final.fill, fee=Decimal("200002")))
    inbox, checkpoint, digest = _files(
        exponent_directory,
        intent,
        [Receipt("two", 50, first_scaled), Receipt("three", 60, final_scaled)],
    )
    with localcontext() as context:
        context.Emax = 4
        client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
        assert client.advance() and client.advance()
        assert Decimal(client.snapshot()["ledger"]["fees"]) == Decimal("300003")
    assert _open(checkpoint, inbox, digest).offset == 2


def _bad_evidence(case: str) -> tuple[NewOrderRequest, list[Receipt]]:
    intent, receipts = _fixture()
    query = receipts[0].payload
    part, final = receipts[4].payload, receipts[5].payload
    assert isinstance(query, ReconciliationResponse)
    assert isinstance(part, ExecutionReport) and part.fill is not None
    assert isinstance(final, ExecutionReport) and final.fill is not None
    if case == "false_complete_detail":
        payload = replace(
            query,
            orders=(replace(query.orders[0], execution_detail_complete_as_of=True),),
        )
        return intent, [Receipt("bad", 60, payload)]
    if case == "terminal_quantity_contradiction":
        cancellation = replace(
            part,
            report_id="cancel",
            report_type=ReportType.CANCELLED,
            exchange_timestamp_ns=50,
            order_status=OrderStatus.CANCELLED,
            fill=None,
        )
        return intent, [
            Receipt("cancel", 60, cancellation),
            Receipt("two", 70, part),
            Receipt("three", 80, final),
        ]
    if case == "one_intent_two_venue_ids":
        other = replace(final.fill, order_id="second-venue-order")
        payload = replace(final, order_id=other.order_id, fill=other)
        return intent, [Receipt("two", 50, part), Receipt("three", 60, payload)]
    if case in {"fill_after_terminal", "older_terminal_after_fill"}:
        premature_terminal = replace(
            query, orders=(replace(query.orders[0], state_timestamp_ns=30),)
        )
        if case == "fill_after_terminal":
            return intent, [
                Receipt("terminal", 60, premature_terminal),
                Receipt("three", 70, final),
            ]
        return intent, [
            Receipt("three", 50, final),
            Receipt("terminal", 60, premature_terminal),
        ]
    if case == "report_status_mismatch":
        return intent, [
            Receipt("bad", 60, replace(final, report_type=ReportType.ACCEPTED))
        ]
    if case == "report_detail_exceeds_cumulative":
        return intent, [Receipt("bad", 60, replace(part, remaining_quantity=4))]
    if case == "response_detail_exceeds_cumulative":
        payload = replace(
            query,
            orders=(
                replace(
                    query.orders[0],
                    status=OrderStatus.PARTIALLY_FILLED,
                    state_timestamp_ns=20,
                    remaining_quantity=4,
                ),
            ),
        )
        return intent, [Receipt("bad", 60, payload)]
    if case == "fill_identity_collision":
        payload = replace(final, fill=replace(final.fill, fill_id=part.fill.fill_id))
        return intent, [Receipt("two", 50, part), Receipt("three", 60, payload)]
    if case == "receipt_identity_collision":
        return intent, [Receipt("same", 50, part), Receipt("same", 60, final)]
    if case == "report_identity_collision":
        return intent, [
            Receipt("two", 50, part),
            Receipt("three", 60, replace(final, report_id=part.report_id)),
        ]
    if case == "query_identity_collision":
        return intent, [
            Receipt("new", 60, query),
            Receipt("old", 70, replace(query, as_of_timestamp_ns=49)),
        ]
    if case == "noncausal_notification":
        return intent, [Receipt("bad", 19, part)]
    if case == "noncausal_snapshot":
        return intent, [Receipt("bad", 60, replace(query, as_of_timestamp_ns=51))]
    if case == "wrong_side":
        return intent, [
            Receipt("bad", 50, replace(part, fill=replace(part.fill, side=Side.ASK)))
        ]
    if case == "wrong_strategy":
        return intent, [
            Receipt(
                "bad", 50, replace(part, fill=replace(part.fill, strategy_id="other"))
            )
        ]
    if case == "wrong_order":
        return intent, [
            Receipt("bad", 50, replace(part, fill=replace(part.fill, order_id="other")))
        ]
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    (
        "false_complete_detail",
        "terminal_quantity_contradiction",
        "one_intent_two_venue_ids",
        "fill_after_terminal",
        "older_terminal_after_fill",
        "report_status_mismatch",
        "report_detail_exceeds_cumulative",
        "response_detail_exceeds_cumulative",
        "fill_identity_collision",
        "receipt_identity_collision",
        "report_identity_collision",
        "query_identity_collision",
        "noncausal_notification",
        "noncausal_snapshot",
        "wrong_side",
        "wrong_strategy",
        "wrong_order",
    ),
)
def test_independent_impossible_evidence_fails_before_inbox_publication(
    tmp_path: Path, case: str
) -> None:
    intent, receipts = _bad_evidence(case)
    with pytest.raises(RecoveryError):
        _files(tmp_path, intent, receipts)
    assert not (tmp_path / "inbox.json").exists()


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


@pytest.mark.parametrize(
    "mutation",
    (
        "offset",
        "prefix",
        "ledger",
        "notification",
        "identity",
        "unknown_field",
        "missing_state",
        "version",
        "session",
        "inbox",
        "boolean_offset",
    ),
)
def test_recomputed_checksum_cannot_hide_invalid_committed_state(
    tmp_path: Path, mutation: str
) -> None:
    intent, receipts = _fixture()
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
    assert client.advance()
    envelope = json.loads(checkpoint.read_text())
    document = envelope["checkpoint"]
    if mutation == "offset":
        document["offset"] = 0
    elif mutation == "prefix":
        document["prefix_sha256"] = "0" * 64
    elif mutation == "ledger":
        document["state"]["ledger"]["trade_cash_ticks"] += 1
    elif mutation == "notification":
        document["state"]["notifications"]["detail-two"] = 59
    elif mutation == "identity":
        document["state"]["query_identities"] = {}
    elif mutation == "unknown_field":
        document["state"]["extra"] = 1
    elif mutation == "missing_state":
        del document["state"]["knowledge"]
    elif mutation == "version":
        document["version"] = "future-version"
    elif mutation == "session":
        document["session_id"] = "other-session"
    elif mutation == "inbox":
        document["inbox_sha256"] = "0" * 64
    else:
        document["offset"] = True
    envelope["sha256"] = hashlib.sha256(_canonical(document)).hexdigest()
    checkpoint.write_bytes(_canonical(envelope))
    with pytest.raises(RecoveryError):
        _open(checkpoint, inbox, digest)


@pytest.mark.parametrize(
    "bad_bytes",
    (
        b"",
        b"{",
        b"{}",
        b'{"sha256":"a","sha256":"b","checkpoint":{}}',
        b'{"sha256":NaN,"checkpoint":{}}',
    ),
)
def test_unreadable_committed_state_and_missing_state_fail_closed(
    tmp_path: Path, bad_bytes: bytes
) -> None:
    intent, receipts = _fixture()
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    with pytest.raises(RecoveryError):
        _open(checkpoint, inbox, digest)
    assert not checkpoint.exists()
    checkpoint.write_bytes(bad_bytes)
    with pytest.raises(RecoveryError):
        _open(checkpoint, inbox, digest)
    with pytest.raises(RecoveryError):
        DurableClient.create(checkpoint, inbox, "independent-session", digest)
    assert checkpoint.read_bytes() == bad_bytes


def test_direct_constructor_cannot_bypass_committed_state_verification(
    tmp_path: Path,
) -> None:
    intent, receipts = _fixture()
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    with pytest.raises(RecoveryError):
        DurableClient(checkpoint, inbox, "independent-session", digest)
    assert not checkpoint.exists()
    client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
    assert client.advance() and client.advance()
    committed = checkpoint.read_bytes()
    with pytest.raises(RecoveryError):
        DurableClient(checkpoint, inbox, "independent-session", digest)
    assert checkpoint.read_bytes() == committed
    assert _open(checkpoint, inbox, digest).offset == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "boolean_quantity",
        "boolean_side",
        "negative_time",
        "invalid_expiry",
        "missing_field",
        "unknown_field",
        "nonfinite_cost",
        "excess_cost_precision",
        "excess_cost_exponent",
        "incomplete_response_state",
        "version",
        "session",
        "expected_digest",
        "delivery_order",
        "order_limit",
        "receipt_limit",
        "byte_limit",
    ),
)
def test_invalid_persisted_inbox_schema_and_limits_never_create_checkpoint(
    tmp_path: Path, mutation: str
) -> None:
    intent, receipts = _fixture()
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    document = json.loads(inbox.read_text())
    if mutation == "boolean_quantity":
        document["intents"][0]["quantity"] = True
    elif mutation == "boolean_side":
        document["intents"][0]["side"] = True
    elif mutation == "negative_time":
        document["intents"][0]["decision_timestamp_ns"] = -1
    elif mutation == "invalid_expiry":
        document["intents"][0]["expire_timestamp_ns"] = 9
    elif mutation == "missing_field":
        del document["intents"][0]["quantity"]
    elif mutation == "unknown_field":
        document["intents"][0]["extra"] = 1
    elif mutation in {
        "nonfinite_cost",
        "excess_cost_precision",
        "excess_cost_exponent",
    }:
        document["receipts"][0]["payload"]["orders"][0]["fills"][0]["fee"] = {
            "nonfinite_cost": "Infinity",
            "excess_cost_precision": "0." + "1" * 65,
            "excess_cost_exponent": "1E+65",
        }[mutation]
    elif mutation == "incomplete_response_state":
        document["receipts"][0]["payload"]["orders"][0]["status"] = None
    elif mutation == "version":
        document["version"] = "future-version"
    elif mutation == "session":
        document["session_id"] = "other-session"
    elif mutation == "delivery_order":
        document["receipts"] = list(reversed(document["receipts"]))
    elif mutation == "order_limit":
        document["intents"] = [document["intents"][0]] * 257
    elif mutation == "receipt_limit":
        document["receipts"] = [document["receipts"][0]] * 4097
    raw = (
        b" " * (4 * 1024 * 1024 + 1)
        if mutation == "byte_limit"
        else _canonical(document)
    )
    inbox.write_bytes(raw)
    digest = (
        "0" * 64 if mutation == "expected_digest" else hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(RecoveryError):
        DurableClient.create(checkpoint, inbox, "independent-session", digest)
    assert not checkpoint.exists()


@pytest.mark.parametrize(
    "operation", ("write", "file_fsync", "replace", "directory_fsync")
)
def test_storage_errors_poison_instance_and_reopen_a_valid_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    intent, receipts = _fixture()
    inbox, checkpoint, digest = _files(tmp_path, intent, receipts)
    client = DurableClient.create(checkpoint, inbox, "independent-session", digest)
    calls = 0
    original_fsync = os.fsync
    original_fdopen = os.fdopen

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if (operation == "file_fsync" and calls == 1) or (
            operation == "directory_fsync" and calls == 2
        ):
            raise OSError("injected storage failure")
        original_fsync(fd)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected storage failure")

    class FailedWriter:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> FailedWriter:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            self.stream.__exit__(*args)

        def write(self, value: bytes) -> None:
            raise OSError("injected stream.write failure")

    def failed_writer(*args: Any, **kwargs: Any) -> FailedWriter:
        return FailedWriter(original_fdopen(*args, **kwargs))

    with monkeypatch.context() as patch:
        if operation == "write":
            patch.setattr(recovery.os, "fdopen", failed_writer)
        elif operation == "replace":
            patch.setattr(recovery.os, "replace", fail)
        else:
            patch.setattr(recovery.os, "fsync", fsync)
        with pytest.raises(RecoveryError):
            client.advance()
    with pytest.raises(RecoveryError):
        client.snapshot()
    with pytest.raises(RecoveryError):
        _ = client.offset
    with pytest.raises(RecoveryError):
        client.advance()
    reopened = _open(checkpoint, inbox, digest)
    assert reopened.offset == (1 if operation == "directory_fsync" else 0)
    while reopened.advance():
        pass
    assert reopened.offset == len(receipts)
    assert reopened.snapshot()["ledger"]["known_inventory"] == 5
