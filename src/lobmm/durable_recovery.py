"""Bounded offline client recovery over an already durable immutable inbox.

Single writer, local POSIX filesystem. No network, command emission, simulator
restart or active strategy restoration. See DURABLE_RECOVERY_CONTRACT_V1.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import ExecutionReport, Fill, NewOrderRequest
from lobmm.reconciliation import OrderEvidence, ReconciliationResponse
from lobmm.transport import ClientKnowledge

MAX_BYTES = 4 * 1024 * 1024
MAX_ORDERS = 256
MAX_RECEIPTS = 4096
VERSION = "durable-client-v1"
Hook = Callable[[str], None]
E = TypeVar("E", bound=Enum)


class RecoveryError(ValueError):
    """Recovery refuses to expose unverified or uncertain client state."""


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    notification_timestamp_ns: int
    payload: ExecutionReport | ReconciliationResponse


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(v) for v in value]
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise RecoveryError(f"non-finite JSON constant: {value}")


def _read(path: Path) -> tuple[Any, bytes]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise RecoveryError("persisted file exceeds byte limit")
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant), raw
    except (OSError, ValueError, RecursionError) as exc:
        raise RecoveryError(f"cannot read valid recovery file: {path.name}") from exc


def _object(value: Any, keys: Sequence[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        raise RecoveryError("incomplete or unknown recovery fields")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RecoveryError("invalid recovery integer")
    return value


def _text(value: Any, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise RecoveryError("invalid recovery identity/text")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise RecoveryError("invalid recovery boolean")
    return value


def _enum[E: Enum](cls: type[E], value: Any) -> E:
    if isinstance(value, bool):
        raise RecoveryError("invalid recovery enum")
    try:
        return cls(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryError("invalid recovery enum") from exc


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(_text(value))
    except InvalidOperation as exc:
        raise RecoveryError("invalid recovery cost") from exc
    if not result.is_finite() or result < 0:
        raise RecoveryError("invalid recovery cost")
    return result


def _intent(raw: Any) -> NewOrderRequest:
    d = _object(raw, [f.name for f in fields(NewOrderRequest)])
    return NewOrderRequest(
        _text(d["client_order_id"]),
        _enum(Side, d["side"]),
        _integer(d["price_ticks"], 1),
        _integer(d["quantity"], 1),
        _text(d["strategy_id"]),
        _integer(d["decision_timestamp_ns"]),
        _integer(d["send_timestamp_ns"]),
        _boolean(d["post_only"]),
        _boolean(d["rest_unfilled"]),
        None
        if d["expire_timestamp_ns"] is None
        else _integer(d["expire_timestamp_ns"]),
    )


def _fill(raw: Any) -> Fill:
    d = _object(raw, [f.name for f in fields(Fill)])
    return Fill(
        _text(d["fill_id"]),
        _text(d["order_id"]),
        _text(d["client_order_id"]),
        _text(d["strategy_id"]),
        _enum(Side, d["side"]),
        _integer(d["quantity"], 1),
        _integer(d["price_ticks"], 1),
        _enum(LiquidityRole, d["liquidity_role"]),
        _integer(d["exchange_fill_timestamp_ns"]),
        _decimal(d["fee"]),
        _decimal(d["rebate"]),
    )


def _report(raw: Any) -> ExecutionReport:
    d = _object(raw, [f.name for f in fields(ExecutionReport)])
    return ExecutionReport(
        _text(d["report_id"]),
        _enum(ReportType, d["report_type"]),
        _integer(d["exchange_timestamp_ns"]),
        _text(d["order_id"], empty=True),
        _text(d["client_order_id"]),
        _enum(OrderStatus, d["order_status"]),
        _integer(d["remaining_quantity"]),
        None if d["fill"] is None else _fill(d["fill"]),
        None if d["reason"] is None else _text(d["reason"], empty=True),
    )


def _response(raw: Any) -> ReconciliationResponse:
    d = _object(raw, [f.name for f in fields(ReconciliationResponse)])
    if type(d["orders"]) is not list or len(d["orders"]) > MAX_ORDERS:
        raise RecoveryError("invalid response order list")
    orders = []
    for raw_order in d["orders"]:
        o = _object(raw_order, [f.name for f in fields(OrderEvidence)])
        if type(o["fills"]) is not list or len(o["fills"]) > MAX_RECEIPTS:
            raise RecoveryError("invalid response fill list")
        state = (o["state_timestamp_ns"], o["status"], o["remaining_quantity"])
        if any(x is None for x in state) and not all(x is None for x in state):
            raise RecoveryError("incomplete response state")
        orders.append(
            OrderEvidence(
                _text(o["client_order_id"]),
                None if state[0] is None else _integer(state[0]),
                None if state[1] is None else _enum(OrderStatus, state[1]),
                None if state[2] is None else _integer(state[2]),
                tuple(_fill(f) for f in o["fills"]),
                _boolean(o["execution_detail_complete_as_of"]),
            )
        )
    return ReconciliationResponse(
        _text(d["query_id"]),
        _integer(d["request_timestamp_ns"]),
        _integer(d["exchange_snapshot_timestamp_ns"]),
        _integer(d["as_of_timestamp_ns"]),
        _integer(d["tape_end_timestamp_ns"]),
        _boolean(d["snapshot_within_tape"]),
        tuple(orders),
    )


def _receipt_wire(receipt: Receipt) -> dict[str, Any]:
    return dict(
        receipt_id=receipt.receipt_id,
        notification_timestamp_ns=receipt.notification_timestamp_ns,
        kind="report" if isinstance(receipt.payload, ExecutionReport) else "response",
        payload=_wire(asdict(receipt.payload)),
    )


def _receipt(raw: Any) -> Receipt:
    d = _object(raw, ("receipt_id", "notification_timestamp_ns", "kind", "payload"))
    if d["kind"] not in {"report", "response"}:
        raise RecoveryError("unsupported receipt kind")
    return Receipt(
        _text(d["receipt_id"]),
        _integer(d["notification_timestamp_ns"]),
        _report(d["payload"]) if d["kind"] == "report" else _response(d["payload"]),
    )


def _bind(table: dict[str, Any], identity: str, payload: Any) -> bool:
    if identity in table:
        if table[identity] != payload:
            raise RecoveryError("conflicting immutable identity: " + identity)
        return False
    table[identity] = payload
    return True


class _Fold:
    def __init__(self, intents: Sequence[NewOrderRequest]) -> None:
        self.knowledge = ClientKnowledge()
        self.receipts: dict[str, Any] = {}
        self.reports: dict[str, Any] = {}
        self.queries: dict[str, Any] = {}
        self.notifications: dict[str, int] = {}
        self.order_ids: dict[str, str] = {}
        for intent in intents:
            if intent.client_order_id in self.knowledge.entries:
                raise RecoveryError("reused intent identity")
            self.knowledge.sent(intent)

    def _request(self, cid: str) -> NewOrderRequest:
        if cid not in self.knowledge.entries:
            raise RecoveryError("evidence references unsaved intent")
        return self.knowledge.entries[cid]

    def _detail(self, fill: Fill, timestamp: int) -> None:
        request = self._request(fill.client_order_id)
        if (
            fill.side != request.side
            or fill.strategy_id != request.strategy_id
            or not request.send_timestamp_ns
            <= fill.exchange_fill_timestamp_ns
            <= timestamp
        ):
            raise RecoveryError("fill binding or causality mismatch")
        _bind(self.order_ids, fill.order_id, fill.client_order_id)
        existing = self.knowledge.fill_details.get(fill.fill_id)
        if existing is not None and existing != fill:
            raise RecoveryError("conflicting immutable fill identity")
        if self.knowledge.recovered_fill(fill):
            self.notifications[fill.fill_id] = timestamp
        if self.knowledge.filled[fill.client_order_id] > request.quantity:
            raise RecoveryError("fill quantity exceeds saved intent")

    def _state(self, cid: str, remaining: int, status: OrderStatus) -> None:
        request = self._request(cid)
        if remaining > request.quantity:
            raise RecoveryError("remaining quantity exceeds saved intent")
        if status is OrderStatus.FILLED and remaining != 0:
            raise RecoveryError("filled status has nonzero remaining quantity")
        self.knowledge.reconciled_state(cid, remaining, terminal=status.terminal)

    def apply(self, receipt: Receipt) -> None:
        payload = receipt.payload
        timestamp = receipt.notification_timestamp_ns
        wire = _receipt_wire(receipt)
        identity_payload = dict(kind=wire["kind"], payload=wire["payload"])
        if not _bind(self.receipts, receipt.receipt_id, identity_payload):
            return
        if isinstance(payload, ExecutionReport):
            if not _bind(self.reports, payload.report_id, wire["payload"]):
                return
            request = self._request(payload.client_order_id)
            if (
                not request.send_timestamp_ns
                <= payload.exchange_timestamp_ns
                <= timestamp
            ):
                raise RecoveryError("noncausal report")
            if payload.reason == "unknown_order":
                if (
                    payload.report_type is not ReportType.CANCEL_REJECTED
                    or payload.fill is not None
                ):
                    raise RecoveryError("invalid unknown-order evidence")
                return
            _bind(self.order_ids, _text(payload.order_id), payload.client_order_id)
            if payload.fill is not None:
                fill = payload.fill
                if (
                    fill.client_order_id != payload.client_order_id
                    or fill.order_id != payload.order_id
                    or fill.exchange_fill_timestamp_ns != payload.exchange_timestamp_ns
                ):
                    raise RecoveryError("report/fill binding mismatch")
                self._detail(fill, timestamp)
            self._state(
                payload.client_order_id,
                payload.remaining_quantity,
                payload.order_status,
            )
        else:
            if not _bind(self.queries, payload.query_id, wire["payload"]):
                return
            if (
                not payload.request_timestamp_ns
                <= payload.exchange_snapshot_timestamp_ns
                <= timestamp
                or payload.as_of_timestamp_ns
                > min(
                    payload.exchange_snapshot_timestamp_ns,
                    payload.tape_end_timestamp_ns,
                )
                or payload.snapshot_within_tape
                != (
                    payload.exchange_snapshot_timestamp_ns
                    <= payload.tape_end_timestamp_ns
                )
            ):
                raise RecoveryError("noncausal reconciliation response")
            seen_orders: set[str] = set()
            for evidence in payload.orders:
                cid = evidence.client_order_id
                self._request(cid)
                if cid in seen_orders:
                    raise RecoveryError("duplicate response order")
                seen_orders.add(cid)
                for fill in evidence.fills:
                    if (
                        fill.client_order_id != cid
                        or fill.exchange_fill_timestamp_ns > payload.as_of_timestamp_ns
                    ):
                        raise RecoveryError("response/fill binding mismatch")
                    self._detail(fill, timestamp)
                if evidence.status is not None:
                    assert evidence.state_timestamp_ns is not None
                    assert evidence.remaining_quantity is not None
                    if (
                        not self._request(cid).send_timestamp_ns
                        <= evidence.state_timestamp_ns
                        <= payload.as_of_timestamp_ns
                    ):
                        raise RecoveryError("noncausal response state")
                    self._state(cid, evidence.remaining_quantity, evidence.status)
        for cid, request in self.knowledge.entries.items():
            if (
                self.knowledge.filled[cid]
                > request.quantity - self.knowledge.remaining[cid]
            ):
                raise RecoveryError("execution detail contradicts quantity evidence")

    def snapshot(self) -> dict[str, Any]:
        details = tuple(self.knowledge.fill_details.values())
        return dict(
            knowledge=self.knowledge.artifact(),
            fills=[_wire(asdict(f)) for f in details],
            notifications=self.notifications,
            receipt_identities=self.receipts,
            report_identities=self.reports,
            query_identities=self.queries,
            order_identities=self.order_ids,
            ledger=dict(
                known_inventory=sum(f.signed_quantity for f in details),
                trade_cash_ticks=sum(
                    -f.signed_quantity * f.price_ticks for f in details
                ),
                fees=str(sum((f.fee for f in details), Decimal(0))),
                rebates=str(sum((f.rebate for f in details), Decimal(0))),
            ),
        )


def _inbox(
    path: Path, session_id: str, expected: str
) -> tuple[list[NewOrderRequest], list[Receipt]]:
    raw, encoded = _read(path)
    d = _object(raw, ("version", "session_id", "intents", "receipts"))
    if (
        d["version"] != VERSION
        or d["session_id"] != _text(session_id)
        or _hash(encoded) != expected
    ):
        raise RecoveryError("incompatible inbox identity/version/digest")
    if (
        type(d["intents"]) is not list
        or not 1 <= len(d["intents"]) <= MAX_ORDERS
        or type(d["receipts"]) is not list
        or len(d["receipts"]) > MAX_RECEIPTS
    ):
        raise RecoveryError("inbox count exceeds limits")
    try:
        intents = [_intent(i) for i in d["intents"]]
        receipts = [_receipt(r) for r in d["receipts"]]
    except (ValueError, TypeError, KeyError) as exc:
        raise RecoveryError("invalid inbox schema") from exc
    times = [r.notification_timestamp_ns for r in receipts]
    if times != sorted(times):
        raise RecoveryError("inbox must preserve client delivery order")
    return intents, receipts


def _atomic(path: Path, data: bytes, hook: Hook | None = None) -> None:
    def reached(stage: str) -> None:
        if hook is not None:
            hook(stage)

    if len(data) > MAX_BYTES:
        raise RecoveryError("checkpoint exceeds byte limit")
    reached("before_temp")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            split = len(data) // 2
            stream.write(data[:split])
            stream.flush()
            reached("partial_write")
            stream.write(data[split:])
            stream.flush()
            reached("after_write")
            os.fsync(stream.fileno())
            reached("after_file_fsync")
        os.replace(temporary, path)
        reached("after_replace")
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        reached("after_directory_fsync")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_inbox(
    path: Path,
    session_id: str,
    intents: Sequence[NewOrderRequest],
    receipts: Sequence[Receipt],
) -> str:
    """Save finite evidence before initializing recovery; caller retains digest."""
    if path.exists():
        raise RecoveryError("inbox already exists")
    data = _json(
        dict(
            version=VERSION,
            session_id=_text(session_id),
            intents=[_wire(asdict(i)) for i in intents],
            receipts=[_receipt_wire(r) for r in receipts],
        )
    )
    # Validate the entire finite fold before publishing an inbox.
    if (
        len(data) > MAX_BYTES
        or not 1 <= len(intents) <= MAX_ORDERS
        or len(receipts) > MAX_RECEIPTS
    ):
        raise RecoveryError("inbox exceeds limits")
    parsed_intents = [_intent(i) for i in json.loads(data)["intents"]]
    parsed_receipts = [_receipt(r) for r in json.loads(data)["receipts"]]
    fold = _Fold(parsed_intents)
    last = -1
    for receipt in parsed_receipts:
        if receipt.notification_timestamp_ns < last:
            raise RecoveryError("inbox must preserve client delivery order")
        fold.apply(receipt)
        last = receipt.notification_timestamp_ns
    _atomic(path, data)
    return _hash(data)


class DurableClient:
    """Verified prefix with atomic fill knowledge/identity/offset persistence.

    create is explicit initialization; open never silently initializes missing
    state. An instance is unusable after any interrupted/failed commit.
    """

    def __init__(
        self, checkpoint: Path, inbox: Path, session_id: str, inbox_sha256: str
    ) -> None:
        self._checkpoint = checkpoint
        self._session = session_id
        self._inbox_sha = inbox_sha256
        self._intents, self._receipts = _inbox(inbox, session_id, inbox_sha256)
        self._offset = 0
        self._fold = _Fold(self._intents)
        self._poisoned = False

    @classmethod
    def create(
        cls, checkpoint: Path, inbox: Path, session_id: str, inbox_sha256: str
    ) -> DurableClient:
        if checkpoint.exists():
            raise RecoveryError("checkpoint already exists")
        client = cls(checkpoint, inbox, session_id, inbox_sha256)
        _atomic(checkpoint, client._encoded())
        return client

    @classmethod
    def open(
        cls, checkpoint: Path, inbox: Path, session_id: str, inbox_sha256: str
    ) -> DurableClient:
        raw, _ = _read(checkpoint)
        envelope = _object(raw, ("sha256", "checkpoint"))
        document = _object(
            envelope["checkpoint"],
            (
                "version",
                "session_id",
                "inbox_sha256",
                "offset",
                "prefix_sha256",
                "state",
            ),
        )
        if envelope["sha256"] != _hash(_json(document)):
            raise RecoveryError("checkpoint checksum mismatch")
        client = cls(checkpoint, inbox, session_id, inbox_sha256)
        offset = _integer(document["offset"])
        if offset > len(client._receipts):
            raise RecoveryError("checkpoint offset exceeds inbox")
        for receipt in client._receipts[:offset]:
            client._fold.apply(receipt)
        client._offset = offset
        if _json(envelope) != client._encoded():
            raise RecoveryError("checkpoint state/prefix/identity/version mismatch")
        return client

    @property
    def offset(self) -> int:
        self._healthy()
        return self._offset

    def _healthy(self) -> None:
        if self._poisoned:
            raise RecoveryError("uncertain commit: reopen before using state")

    def snapshot(self) -> dict[str, Any]:
        self._healthy()
        # Detached JSON data; caller mutations cannot alter the durable fold.
        return json.loads(_json(self._fold.snapshot()))

    def _encoded(self) -> bytes:
        document = dict(
            version=VERSION,
            session_id=self._session,
            inbox_sha256=self._inbox_sha,
            offset=self._offset,
            prefix_sha256=_hash(
                _json([_receipt_wire(r) for r in self._receipts[: self._offset]])
            ),
            state=self._fold.snapshot(),
        )
        return _json(dict(sha256=_hash(_json(document)), checkpoint=document))

    def advance(self, hook: Hook | None = None) -> bool:
        self._healthy()
        if self._offset == len(self._receipts):
            return False
        # Poison before mutating anything. Only durable acknowledgment clears it.
        self._poisoned = True
        try:
            self._fold.apply(self._receipts[self._offset])
            self._offset += 1
            _atomic(self._checkpoint, self._encoded(), hook)
            self._poisoned = False
            if hook is not None:
                hook("after_ack")
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._poisoned = True
            raise RecoveryError("receipt commit failed; reopen required") from exc
