"""Explicit v2 durable command/receipt capture and cancellation-only recovery.

Offline synthetic protocol. A local lifetime flock is not venue/account fencing.
The existing permanent-quarantine API is deliberately untouched.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from lobmm.active_restart import _command, _record
from lobmm.durable_recovery import (
    MAX_BYTES,
    Hook,
    Receipt,
    RecoveryError,
    _atomic,
    _Fold,
    _hash,
    _integer,
    _json,
    _object,
    _read,
    _receipt,
    _receipt_wire,
    _text,
)
from lobmm.enums import Side
from lobmm.orders import CancelRequest, ExecutionReport, Fill, NewOrderRequest
from lobmm.reconciliation import ReconciliationResponse

VERSION = "outbound-cancel-recovery-v2"
MAX_ORDERS = 64
MAX_OUTBOUND = 256
MAX_RECEIPTS = 1024
MAX_CANCEL_ATTEMPTS = 4
_VERIFIED = object()


def _validate(doc: Any) -> _Fold:
    """Rebuild from raw durable events; never trust a separately stored fold."""
    d = _object(
        doc,
        ("version", "account_id", "session_id", "strategy_id", "outbound", "receipts"),
    )
    if d["version"] != VERSION:
        raise RecoveryError("incompatible outbound recovery version")
    for key in ("account_id", "session_id", "strategy_id"):
        _text(d[key])
    outgoing, receipts = d["outbound"], d["receipts"]
    if (
        type(outgoing) is not list
        or len(outgoing) > MAX_OUTBOUND
        or type(receipts) is not list
        or len(receipts) > MAX_RECEIPTS
    ):
        raise RecoveryError("outbound recovery count exceeds limit")
    intents: dict[str, NewOrderRequest] = {}
    dispatches: dict[str, int] = {}
    attempts: dict[str, int] = {}
    last = -1
    for record in outgoing:
        action = _command(record)
        stamp = record["dispatch_timestamp_ns"]
        if stamp < last:
            raise RecoveryError("outbound dispatch moved backward")
        last = stamp
        if isinstance(action, NewOrderRequest):
            cid = action.client_order_id
            if cid in intents or action.strategy_id != d["strategy_id"]:
                raise RecoveryError("reused new identity or wrong strategy")
            intents[cid] = action
            dispatches[cid] = stamp
        else:
            cancel_cid = action.client_order_id
            if action.order_id is not None or cancel_cid not in intents:
                raise RecoveryError("cancel must address a reserved client identity")
            assert cancel_cid is not None
            attempts[cancel_cid] = attempts.get(cancel_cid, 0) + 1
            if attempts[cancel_cid] > MAX_CANCEL_ATTEMPTS:
                raise RecoveryError("cancellation attempt count exceeds limit")
    if len(intents) > MAX_ORDERS:
        raise RecoveryError("saved order count exceeds limit")
    fold = _Fold(tuple(intents.values()))

    def causal(cid: str, timestamp: int) -> None:
        if cid not in dispatches or timestamp < dispatches[cid]:
            raise RecoveryError("evidence predates actual reserved dispatch")

    def detail(fill: Fill) -> None:
        causal(fill.client_order_id, fill.exchange_fill_timestamp_ns)
        intent = intents[fill.client_order_id]
        if (intent.side is Side.BID and fill.price_ticks > intent.price_ticks) or (
            intent.side is Side.ASK and fill.price_ticks < intent.price_ticks
        ):
            raise RecoveryError("fill violates reserved order limit")

    last = -1
    for raw in receipts:
        receipt = _receipt(raw)
        if receipt.notification_timestamp_ns < last:
            raise RecoveryError("receipt delivery timestamp moved backward")
        last = receipt.notification_timestamp_ns
        payload = receipt.payload
        if isinstance(payload, ExecutionReport):
            causal(payload.client_order_id, payload.exchange_timestamp_ns)
            if payload.fill is not None:
                detail(payload.fill)
        else:
            for order in payload.orders:
                if order.client_order_id not in intents:
                    raise RecoveryError("response references unsaved intent")
                for fill in order.fills:
                    detail(fill)
                if order.state_timestamp_ns is not None:
                    causal(order.client_order_id, order.state_timestamp_ns)
                if order.status is not None and order.status.terminal:
                    bound = order.client_order_id in fold.order_ids.values() or any(
                        f.client_order_id == order.client_order_id for f in order.fills
                    )
                    if not bound:
                        raise RecoveryError(
                            "terminal query lacks venue identity binding"
                        )
        fold.apply(receipt)
    return fold


class OutboundRecoverySessionV2:
    """New orders only on explicit create; open permits evidence/cancels only.

    Adapter acknowledgment follows durable receipt capture. The adapter must
    retain/redeliver until acknowledgment. No method re-enables entry on open.
    All inventory bounds concern saved orders and a zero starting position.
    """

    def __init__(
        self, root: Path, fd: int, doc: dict[str, Any], *, permit: object
    ) -> None:
        if permit is not _VERIFIED:
            raise RecoveryError("use explicit create or verified open")
        self._root = root
        self._fd = -1
        self._owner = os.getpid()
        self._doc = doc
        self._fold = _validate(doc)
        self._entry = False
        self._poisoned = False
        self._busy = False
        self._mutex = threading.RLock()
        self._fd = fd

    @classmethod
    def _acquire(
        cls,
        root: Path,
        account_id: str,
        session_id: str,
        strategy_id: str,
        *,
        create: bool,
    ) -> Self:
        context = dict(
            version=VERSION,
            account_id=_text(account_id),
            session_id=_text(session_id),
            strategy_id=_text(strategy_id),
        )
        root = root.resolve()
        if create:
            root.mkdir()  # exclusive directory creation; never overwrite old state
        fd = os.open(
            root / "writer.lock",
            os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0),
            0o600,
        )
        result: Self | None = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError("another local writer holds this journal") from exc
            if create:
                doc = dict(**context, outbound=[], receipts=[])
            else:
                raw, _ = _read(root / "journal.json")
                envelope = _object(raw, ("sha256", "journal"))
                doc = envelope["journal"]
                if envelope["sha256"] != _hash(_json(doc)):
                    raise RecoveryError("outbound journal checksum mismatch")
                _validate(doc)
                if any(doc.get(k) != v for k, v in context.items()):
                    raise RecoveryError("outbound journal identity/context mismatch")
            result = cls(root, fd, doc, permit=_VERIFIED)
            if create:
                _atomic(root / "journal.json", result._encoded())
                result._entry = True
            return result
        except BaseException:
            if result is not None:
                result._fd = -1
            os.close(fd)
            raise

    @classmethod
    def create(
        cls, root: Path, account_id: str, session_id: str, strategy_id: str
    ) -> Self:
        """Explicit new synthetic session; existing directories are never reused."""
        return cls._acquire(root, account_id, session_id, strategy_id, create=True)

    @classmethod
    def open(
        cls, root: Path, account_id: str, session_id: str, strategy_id: str
    ) -> Self:
        """Verified disk-only recovery with new entry permanently disabled."""
        return cls._acquire(root, account_id, session_id, strategy_id, create=False)

    def __enter__(self) -> Self:
        self._healthy()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Never LOCK_UN: an inherited descriptor must not revoke parent authority.
        fd = getattr(self, "_fd", -1)
        if fd >= 0:
            self._fd = -1
            os.close(fd)

    def _healthy(self) -> None:
        if self._owner != os.getpid():
            raise RecoveryError("outbound authority cannot cross process boundaries")
        if self._fd < 0:
            raise RecoveryError("outbound session is closed")
        if self._poisoned:
            raise RecoveryError("uncertain operation: close and reopen recovery-only")
        if self._busy:
            raise RecoveryError("reentrant outbound access")

    def close(self) -> None:
        with self._mutex:
            if self._owner != os.getpid():
                raise RecoveryError(
                    "outbound authority cannot cross process boundaries"
                )
            if self._busy:
                raise RecoveryError("cannot close during outbound operation")
            if self._fd >= 0:
                fd, self._fd = self._fd, -1
                os.close(fd)

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._mutex:
            self._healthy()
            self._busy = True
            try:
                yield
            finally:
                self._busy = False

    def _encoded(self) -> bytes:
        return _json(dict(sha256=_hash(_json(self._doc)), journal=self._doc))

    def _commit(self, candidate: dict[str, Any], hook: Hook | None) -> None:
        self._poisoned = True
        fold = _validate(candidate)
        self._doc = candidate
        data = self._encoded()
        if len(data) > MAX_BYTES:
            raise RecoveryError("outbound journal exceeds byte limit")
        _atomic(self._root / "journal.json", data, hook)
        self._fold = fold
        # The enclosing call clears poison only after sink/ack boundary completes.

    def _latest(self) -> int:
        result: int = max(
            [-1]
            + [r["dispatch_timestamp_ns"] for r in self._doc["outbound"]]
            + [r["notification_timestamp_ns"] for r in self._doc["receipts"]]
        )
        return result

    def _send(
        self,
        action: NewOrderRequest | CancelRequest,
        timestamp_ns: int,
        sink: Callable[[Any], None],
        hook: Hook | None,
    ) -> None:
        stamp = _integer(timestamp_ns)
        if stamp < self._latest():
            raise RecoveryError("dispatch predates client evidence/commands")
        record = _record(action, stamp)
        self._commit(self._doc | {"outbound": [*self._doc["outbound"], record]}, hook)
        if hook is not None:
            hook("before_sink")
        sink(action)
        if hook is not None:
            hook("after_sink")
        self._poisoned = False

    def submit_new(
        self,
        request: NewOrderRequest,
        *,
        timestamp_ns: int,
        sink: Callable[[NewOrderRequest], None],
        hook: Hook | None = None,
    ) -> None:
        with self._operation():
            if not self._entry:
                raise RecoveryError("new entry disabled permanently on recovery")
            if type(request) is not NewOrderRequest:
                raise RecoveryError("only new-order requests are accepted")
            self._send(request, timestamp_ns, sink, hook)

    def recover_cancel(
        self,
        client_order_id: str,
        *,
        timestamp_ns: int,
        sink: Callable[[CancelRequest], None],
        hook: Hook | None = None,
    ) -> bool:
        """One bounded attempt; only positive terminal evidence returns False.

        Limits raise while work stays unresolved. A sink return does not mark
        work complete, and an unknown-order rejection cannot stop future retries.
        """
        with self._operation():
            self._entry = False
            cid = _text(client_order_id)
            if cid not in self._fold.knowledge.entries:
                raise RecoveryError("cancel references unsaved intent")
            if cid in self._fold.knowledge.terminal:
                return False
            self._send(
                CancelRequest(timestamp_ns, timestamp_ns, client_order_id=cid),
                timestamp_ns,
                sink,
                hook,
            )
            return True

    def capture(self, receipt: Receipt, *, hook: Hook | None = None) -> bool:
        """Validate and persist raw receipt before acknowledgment; dedup on retry."""
        with self._operation():
            self._poisoned = True
            if type(receipt) is not Receipt or type(receipt.payload) not in (
                ExecutionReport,
                ReconciliationResponse,
            ):
                raise RecoveryError("unsupported receipt type")
            raw = json.loads(_json(_receipt_wire(receipt)))
            parsed = _receipt(raw)
            existing = next(
                (
                    r
                    for r in self._doc["receipts"]
                    if r["receipt_id"] == parsed.receipt_id
                ),
                None,
            )
            if existing is not None:
                if any(existing[k] != raw[k] for k in ("kind", "payload")):
                    raise RecoveryError("conflicting immutable receipt identity")
                self._poisoned = False
                return False
            if parsed.notification_timestamp_ns < self._latest():
                raise RecoveryError("receipt predates client evidence/commands")
            self._commit(self._doc | {"receipts": [*self._doc["receipts"], raw]}, hook)
            self._poisoned = False
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            self._healthy()
            state = self._fold.snapshot()
            counts: dict[str, int] = {}
            for r in self._doc["outbound"]:
                if r["kind"] == "cancel":
                    cid = r["payload"]["client_order_id"]
                    counts[cid] = counts.get(cid, 0) + 1
            result: dict[str, Any] = json.loads(
                _json(
                    dict(
                        version=VERSION,
                        account_id=self._doc["account_id"],
                        session_id=self._doc["session_id"],
                        strategy_id=self._doc["strategy_id"],
                        entry_enabled=self._entry,
                        account_fenced=False,
                        account_complete=False,
                        **state,
                        outbound=self._doc["outbound"],
                        receipts=self._doc["receipts"],
                        cancellation_work=[
                            dict(
                                client_order_id=cid,
                                terminal=cid in self._fold.knowledge.terminal,
                                attempts=counts.get(cid, 0),
                                exhausted=counts.get(cid, 0) >= MAX_CANCEL_ATTEMPTS
                                or len(self._doc["outbound"]) >= MAX_OUTBOUND,
                            )
                            for cid in self._fold.knowledge.entries
                        ],
                    )
                )
            )
            return result
