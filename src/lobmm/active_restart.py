"""Opt-in write-ahead command reservations and permanent restart quarantine.

This is an offline research boundary, not an exchange recovery protocol.
Only a newly created single-process session can emit synthetic commands.
See docs/ACTIVE_RESTART_CONTRACT_V1.md for the deliberately limited contract.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from lobmm.durable_recovery import (
    MAX_BYTES,
    DurableClient,
    Hook,
    RecoveryError,
    _atomic,
    _hash,
    _integer,
    _intent,
    _json,
    _object,
    _read,
    _text,
    _wire,
)
from lobmm.events import MarketEvent
from lobmm.orders import CancelRequest, ExecutionReport, Fill, NewOrderRequest
from lobmm.strategies.base import MarketMakingStrategy, StrategyAction
from lobmm.strategy_runtime import StrategyRuntime

VERSION = "active-restart-boundary-v1"
MAX_COMMANDS = 256
_VERIFIED = object()


def _command(raw: Any) -> StrategyAction:
    record = _object(raw, ("kind", "payload", "dispatch_timestamp_ns"))
    stamp = _integer(record["dispatch_timestamp_ns"])
    if record["kind"] == "new":
        action: StrategyAction = _intent(record["payload"])
    elif record["kind"] == "cancel":
        d = _object(record["payload"], [f.name for f in fields(CancelRequest)])
        action = CancelRequest(
            _integer(d["decision_timestamp_ns"]),
            _integer(d["send_timestamp_ns"]),
            None if d["order_id"] is None else _text(d["order_id"]),
            None if d["client_order_id"] is None else _text(d["client_order_id"]),
        )
    else:
        raise RecoveryError("unsupported command kind")
    if stamp < action.send_timestamp_ns:
        raise RecoveryError("dispatch cannot precede send timestamp")
    return action


def _record(action: StrategyAction, timestamp_ns: int) -> dict[str, Any]:
    if type(action) not in (NewOrderRequest, CancelRequest):
        raise RecoveryError("unsupported command type")
    return dict(
        kind="new" if isinstance(action, NewOrderRequest) else "cancel",
        payload=_wire(asdict(action)),
        dispatch_timestamp_ns=_integer(timestamp_ns),
    )


def _unique(records: list[dict[str, Any]]) -> None:
    clients: set[str] = set()
    cancellations: set[bytes] = set()
    previous = -1
    for record in records:
        action = _command(record)
        stamp = record["dispatch_timestamp_ns"]
        if stamp < previous:
            raise RecoveryError("dispatch timestamp moved backward")
        previous = stamp
        if isinstance(action, NewOrderRequest):
            if action.client_order_id in clients:
                raise RecoveryError("client order identity already reserved")
            clients.add(action.client_order_id)
        else:
            key = _json(record["payload"])
            if key in cancellations:
                raise RecoveryError("cancellation instruction already reserved")
            cancellations.add(key)


class GuardedStrategySession:
    """Complete mediation for callers using this opt-in synthetic command path.

    create returns the only process-local writer. open never returns a writer,
    even when the journal is empty or all supplied evidence is terminal/flat.
    A reservation is potentially dispatched; it is never a venue acknowledgment.
    """

    def __init__(
        self,
        path: Path,
        session_id: str,
        strategy: MarketMakingStrategy,
        max_abs_inventory: int,
        depth: int,
        *,
        _permit: object | None = None,
    ) -> None:
        if _permit is not _VERIFIED:
            raise RecoveryError("use explicit create or verified open")
        self._path = path
        self._session = _text(session_id)
        self._context = _hash(
            _json(
                dict(
                    strategy=strategy.config.model_dump(mode="json"),
                    max_abs_inventory=_integer(max_abs_inventory, 1),
                    depth=_integer(depth, 1),
                )
            )
        )
        self._runtime = StrategyRuntime(
            strategy=strategy, max_abs_inventory=max_abs_inventory, depth=depth
        )
        self._commands: list[dict[str, Any]] = []
        self._owner = os.getpid()
        self._quarantined = True
        self._poisoned = False
        self._dispatching = False

    def _encoded(self) -> bytes:
        document = dict(
            version=VERSION,
            session_id=self._session,
            context_sha256=self._context,
            commands=self._commands,
        )
        return _json(dict(sha256=_hash(_json(document)), journal=document))

    @classmethod
    def create(
        cls,
        path: Path,
        session_id: str,
        strategy: MarketMakingStrategy,
        max_abs_inventory: int,
        depth: int = 5,
    ) -> GuardedStrategySession:
        if path.exists():
            raise RecoveryError("session journal already exists")
        result = cls(
            path, session_id, strategy, max_abs_inventory, depth, _permit=_VERIFIED
        )
        # Only successful durable initialization grants process-local authority.
        _atomic(path, result._encoded())
        result._quarantined = False
        return result

    @classmethod
    def open(
        cls,
        path: Path,
        session_id: str,
        strategy: MarketMakingStrategy,
        max_abs_inventory: int,
        depth: int = 5,
    ) -> GuardedStrategySession:
        raw, _ = _read(path)
        envelope = _object(raw, ("sha256", "journal"))
        doc = _object(
            envelope["journal"],
            ("version", "session_id", "context_sha256", "commands"),
        )
        if envelope["sha256"] != _hash(_json(doc)):
            raise RecoveryError("session checksum mismatch")
        records = doc["commands"]
        if type(records) is not list or len(records) > MAX_COMMANDS:
            raise RecoveryError("session command count exceeds limit")
        _unique(records)
        result = cls(
            path, session_id, strategy, max_abs_inventory, depth, _permit=_VERIFIED
        )
        result._commands = records
        if result._encoded() != _json(envelope):
            raise RecoveryError("session identity/context/version mismatch")
        return result

    def _healthy(self) -> None:
        if self._owner != os.getpid():
            raise RecoveryError("session authority cannot cross process boundaries")
        if self._poisoned:
            raise RecoveryError("uncertain dispatch: reopen quarantined session")
        if self._dispatching:
            raise RecoveryError("reentrant session access during dispatch")

    def _active(self) -> None:
        self._healthy()
        if self._quarantined:
            raise RecoveryError("reopened session quarantined; active restart unsupported")

    def snapshot(self) -> dict[str, Any]:
        self._healthy()
        return dict(
            session_id=self._session,
            quarantined=self._quarantined,
            commands=json.loads(_json(self._commands)),
            resume_supported=False,
            unresolved_runtime=[
                "strategy_state",
                "market_history",
                "scheduler_and_transport",
                "pending_cancellation_work",
                "risk_and_stop_state",
                "complete_account_order_universe",
            ],
        )

    def decide(
        self, *, timestamp_ns: int, allow_new_quotes: bool = True
    ) -> tuple[StrategyAction, ...]:
        self._active()
        return self._runtime.decide(
            timestamp_ns=timestamp_ns, allow_new_quotes=allow_new_quotes
        )

    def deliver_market(
        self, event: MarketEvent, *, notification_timestamp_ns: int
    ) -> None:
        self._active()
        self._runtime.deliver_market(
            event, notification_timestamp_ns=notification_timestamp_ns
        )

    def deliver_report(
        self, report: ExecutionReport, *, notification_timestamp_ns: int
    ) -> None:
        self._active()
        self._runtime.deliver_report(
            report, notification_timestamp_ns=notification_timestamp_ns
        )

    def deliver_recovered_fill(
        self, fill: Fill, *, notification_timestamp_ns: int
    ) -> None:
        self._active()
        self._runtime.deliver_recovered_fill(
            fill, notification_timestamp_ns=notification_timestamp_ns
        )

    def dispatch(
        self,
        action: StrategyAction,
        *,
        timestamp_ns: int,
        sink: Callable[[StrategyAction], None],
        hook: Hook | None = None,
    ) -> None:
        """Reserve durably, call the synthetic sink once, then record locally.

        No retry is provided. Failure after reservation is intentionally ambiguous.
        A caller's cached action still passes through all checks after reopen.
        """
        self._active()
        record = _record(action, timestamp_ns)
        records = [*self._commands, record]
        if len(records) > MAX_COMMANDS:
            raise RecoveryError("session command count exceeds limit")
        _unique(records)
        # Poison before mutation; only the complete call clears uncertainty.
        self._poisoned = True
        self._dispatching = True
        try:
            self._commands = records
            encoded = self._encoded()
            if len(encoded) > MAX_BYTES:
                raise RecoveryError("session journal exceeds byte limit")
            _atomic(self._path, encoded, hook)
            if hook is not None:
                hook("before_sink")
            sink(action)
            if hook is not None:
                hook("after_sink")
            self._runtime.record_action_sent(action, timestamp_ns=timestamp_ns)
            self._poisoned = False
        finally:
            self._dispatching = False

    def assessment(self, client: DurableClient) -> dict[str, Any]:
        """Join exact reserved new intents to verified delivered evidence.

        No evidence can grant command authority. Cancel reservations remain
        possibly pending; a terminal order can be shown separately as resolved.
        """
        self._healthy()
        if client.session_id != self._session:
            raise RecoveryError("evidence session mismatch")
        reserved = tuple(
            _command(record)
            for record in self._commands
            if record["kind"] == "new"
        )
        expected = {action.client_order_id: action for action in reserved}
        supplied = {action.client_order_id: action for action in client.sent_intents}
        if expected != supplied:
            raise RecoveryError("evidence must match every reserved new intent exactly")
        evidence = client.snapshot()
        knowledge = evidence["knowledge"]
        return dict(
            resume_allowed=False,
            quarantined=self._quarantined,
            supplied_inbox_drained=client.inbox_drained,
            saved_order_evidence_complete=knowledge["complete"],
            known_ledger=evidence["ledger"],
            inventory_min=knowledge["inventory_min"],
            inventory_max=knowledge["inventory_max"],
            potentially_outstanding=[
                row for row in knowledge["orders"] if not row["terminal_report_received"]
            ],
            missing_execution_detail=[
                row for row in knowledge["orders"] if row["missing_fill_quantity_min"]
            ],
            cancellation_reservations=[
                json.loads(_json(record))
                for record in self._commands
                if record["kind"] == "cancel"
            ],
            unresolved_runtime=self.snapshot()["unresolved_runtime"],
        )
