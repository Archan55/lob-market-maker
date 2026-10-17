"""Opt-in synthetic account authority; atomic venue-side generation/effect model.

This SQLite file *is* the modeled venue, not a lock around an external sink.
No live fencing, authentication, distributed consensus or anti-rollback claim.
The existing permanent-quarantine and cancellation-only APIs are unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from lobmm.active_restart import _command, _record
from lobmm.durable_recovery import (
    MAX_BYTES,
    Hook,
    Receipt,
    RecoveryError,
    _hash,
    _integer,
    _json,
    _object,
    _text,
)
from lobmm.enums import LiquidityRole, OrderStatus, ReportType, Side
from lobmm.orders import CancelRequest, ExecutionReport, Fill, NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2

VERSION = "simulated-account-authority-v3"
MAX_ACCOUNTS = 8
MAX_CLAIMS = 64
MAX_ORDERS = 64
MAX_COMMANDS = 256
MAX_EVENTS = 1024


@dataclass(frozen=True, slots=True)
class FenceToken:
    authority_id: str
    account_id: str
    generation: int
    owner_id: str
    session_id: str
    strategy_id: str


def _token(raw: Any) -> FenceToken:
    d = _object(
        raw,
        (
            "authority_id",
            "account_id",
            "generation",
            "owner_id",
            "session_id",
            "strategy_id",
        ),
    )
    return FenceToken(
        _text(d["authority_id"]),
        _text(d["account_id"]),
        _integer(d["generation"], 1),
        _text(d["owner_id"]),
        _text(d["session_id"]),
        _text(d["strategy_id"]),
    )


def _current(state: dict[str, Any], authority: str, account: str) -> FenceToken:
    if not state["owners"]:
        raise RecoveryError("account has no owner")
    return FenceToken(authority, account, state["generation"], **state["owners"][-1])


def _report(
    order: dict[str, Any],
    identity: str,
    stamp: int,
    *,
    fill: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = order["status"]
    return dict(
        kind="report",
        authority_id=order["authority_id"],
        account_id=order["account_id"],
        report_id=identity,
        report_type={
            "live": "accepted",
            "partially_filled": "partial_fill",
            "filled": "fill",
            "cancelled": "cancelled",
        }[status],
        exchange_timestamp_ns=stamp,
        order_id=order["order_id"],
        client_order_id=order["client_order_id"],
        order_status=status,
        remaining_quantity=order["original_quantity"] - order["filled_quantity"],
        reason=None,
        fill=fill,
    )


def _apply(
    state: dict[str, Any], event: dict[str, Any], authority: str, account: str, seq: int
) -> Any:
    kind = event.get("kind")
    identity = f"v3-{seq}"
    if kind == "claim":
        _object(
            event,
            ("kind", "owner_id", "session_id", "strategy_id", "expected_generation"),
        )
        expected = _integer(event["expected_generation"])
        owner = {k: _text(event[k]) for k in ("owner_id", "session_id", "strategy_id")}
        if expected != state["generation"]:
            raise RecoveryError("account generation compare-and-swap failed")
        if any(o["owner_id"] == owner["owner_id"] for o in state["owners"]):
            raise RecoveryError("owner identity already retained; reuse refused")
        if len(state["owners"]) >= MAX_CLAIMS:
            raise RecoveryError("retained claim limit")
        state["owners"].append(owner)
        state["generation"] += 1
        state["entry_enabled"] = state["generation"] == 1
        return asdict(_current(state, authority, account))
    if kind not in ("command", "trade"):
        raise RecoveryError("unknown authority event")
    stamp = _integer(event.get("timestamp_ns"))
    if stamp < state["last_timestamp_ns"]:
        raise RecoveryError("authority processing timestamp moved backward")
    state["last_timestamp_ns"] = stamp
    if kind == "command":
        _object(event, ("kind", "token", "command_id", "request", "timestamp_ns"))
        token = _token(event["token"])
        if token != _current(state, authority, account):
            raise RecoveryError("stale or mismatched account fence")
        cid = _text(event["command_id"])
        record = event["request"]
        action = _command(record)
        if record["dispatch_timestamp_ns"] != stamp:
            raise RecoveryError("command processing timestamp mismatch")
        if (
            isinstance(action, NewOrderRequest)
            and action.strategy_id != token.strategy_id
        ):
            raise RecoveryError("new strategy differs from current owner")
        previous = next((c for c in state["commands"] if c["command_id"] == cid), None)
        if previous is not None:
            if (
                previous["token"] != event["token"]
                or previous["request"]["kind"] != record["kind"]
                or previous["request"]["payload"] != record["payload"]
            ):
                raise RecoveryError("conflicting immutable command identity")
            return previous["result"]
        if len(state["commands"]) >= MAX_COMMANDS:
            raise RecoveryError("retained command limit")
        if isinstance(action, NewOrderRequest):
            if not state["entry_enabled"]:
                raise RecoveryError(
                    "account new entry permanently disabled after takeover"
                )
            if any(
                o["client_order_id"] == action.client_order_id for o in state["orders"]
            ):
                raise RecoveryError("client identity already retained; reuse refused")
            if len(state["orders"]) >= MAX_ORDERS:
                raise RecoveryError("retained order limit")
            if (
                action.post_only
                or not action.rest_unfilled
                or action.expire_timestamp_ns is not None
            ):
                raise RecoveryError("unsupported synthetic order option")
            order: dict[str, Any] = dict(
                authority_id=authority,
                account_id=account,
                order_id=f"order-{_hash(_json([authority, account]))[:16]}-{len(state['orders']) + 1}",
                client_order_id=action.client_order_id,
                strategy_id=action.strategy_id,
                side=action.side.value,
                price_ticks=action.price_ticks,
                original_quantity=action.quantity,
                remaining_quantity=action.quantity,
                filled_quantity=0,
                cancelled_quantity=0,
                accepted_generation=token.generation,
                status="live",
            )
            state["orders"].append(order)
            result = _report(order, identity, stamp)
        else:
            order = next(
                (
                    o
                    for o in state["orders"]
                    if (
                        action.client_order_id is not None
                        and o["client_order_id"] == action.client_order_id
                    )
                    or (
                        action.order_id is not None and o["order_id"] == action.order_id
                    )
                ),
                None,
            )
            if order is None:
                result = dict(
                    kind="report",
                    authority_id=authority,
                    account_id=account,
                    report_id=identity,
                    report_type="cancel_rejected",
                    exchange_timestamp_ns=stamp,
                    order_id="",
                    client_order_id=action.client_order_id or "unknown-venue-id",
                    order_status="rejected",
                    remaining_quantity=0,
                    reason="unknown_order",
                    fill=None,
                )
            else:
                if order["remaining_quantity"]:
                    order["cancelled_quantity"] += order["remaining_quantity"]
                    order["remaining_quantity"] = 0
                    order["status"] = "cancelled"
                result = _report(order, identity, stamp)
        state["commands"].append(
            dict(command_id=cid, token=event["token"], request=record, result=result)
        )
        return result
    _object(event, ("kind", "side", "quantity", "timestamp_ns"))
    _current(state, authority, account)
    if type(event["side"]) is not int:
        raise RecoveryError("invalid synthetic side")
    side = Side(event["side"])
    budget = _integer(event["quantity"], 1)
    reports = []
    for order in state["orders"]:
        if order["side"] != side.value or not order["remaining_quantity"] or not budget:
            continue
        quantity = min(budget, order["remaining_quantity"])
        budget -= quantity
        order["remaining_quantity"] -= quantity
        order["filled_quantity"] += quantity
        order["status"] = (
            "partially_filled" if order["remaining_quantity"] else "filled"
        )
        fill = dict(
            fill_id=f"fill-{seq}-{order['order_id']}",
            order_id=order["order_id"],
            client_order_id=order["client_order_id"],
            strategy_id=order["strategy_id"],
            side=side.value,
            quantity=quantity,
            price_ticks=order["price_ticks"],
            exchange_fill_timestamp_ns=stamp,
            fee="0",
            rebate="0",
        )
        reports.append(
            _report(order, f"{identity}-{order['order_id']}", stamp, fill=fill)
        )
        sign = 1 if side is Side.BID else -1
        state["inventory"] += sign * quantity
        state["cash_ticks"] -= sign * quantity * order["price_ticks"]
    return reports


def _fold(events: Any, authority: str, account: str) -> tuple[dict[str, Any], Any]:
    if type(events) is not list or len(events) > MAX_EVENTS:
        raise RecoveryError("retained effect limit or invalid events")
    state: dict[str, Any] = dict(
        generation=0,
        entry_enabled=False,
        owners=[],
        orders=[],
        commands=[],
        inventory=0,
        cash_ticks=0,
        last_timestamp_ns=-1,
    )
    result: Any = None
    for seq, event in enumerate(events, 1):
        if type(event) is not dict:
            raise RecoveryError("invalid authority event")
        result = _apply(state, event, authority, account, seq)
    return state, result


def _validate(doc: Any, authority: str) -> None:
    d = _object(doc, ("version", "authority_id", "accounts"))
    if d["version"] != VERSION or d["authority_id"] != authority:
        raise RecoveryError("authority identity or version mismatch")
    if type(d["accounts"]) is not dict or len(d["accounts"]) > MAX_ACCOUNTS:
        raise RecoveryError("account count limit or invalid accounts")
    for account, events in d["accounts"].items():
        _fold(events, authority, _text(account))


class SimulatedAccountAuthorityV3:
    """Retained single-authority model. All order effects occur inside its DB.

    Each operation opens its own connection. A killed caller cannot retain a
    stale in-memory view or reuse an inherited SQLite connection. Tokens can be
    serialized; only the authority's current retained generation grants effects.
    """

    def __init__(self, path: Path, authority_id: str) -> None:
        self.path = path.resolve()
        self.authority_id = _text(authority_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{quote(str(self.path), safe='/')}?mode=rw", uri=True, timeout=10
        )
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @classmethod
    def create(cls, path: Path, authority_id: str) -> Self:
        result = cls(path, authority_id)
        fd = os.open(result.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with result._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                "CREATE TABLE authority (id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL, digest TEXT NOT NULL)"
            )
            doc = dict(version=VERSION, authority_id=result.authority_id, accounts={})
            raw = _json(doc)
            connection.execute(
                "INSERT INTO authority VALUES (1, ?, ?)", (raw, _hash(raw))
            )
        return result

    @classmethod
    def open(cls, path: Path, authority_id: str) -> Self:
        result = cls(path, authority_id)
        result._operation(None, None)
        return result

    def _operation(
        self,
        account: str | None,
        event: dict[str, Any] | None,
        hook: Hook | None = None,
    ) -> tuple[dict[str, Any], Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body, digest FROM authority WHERE id=1"
            ).fetchone()
            if (
                row is None
                or type(row[0]) is not bytes
                or len(row[0]) > MAX_BYTES
                or _hash(row[0]) != row[1]
            ):
                raise RecoveryError("invalid authority state/checksum/size")
            doc = json.loads(row[0])
            _validate(doc, self.authority_id)
            state: dict[str, Any] = {}
            result: Any = None
            if account is not None:
                _text(account)
                events = doc["accounts"].get(account, [])
                if event is not None:
                    if (
                        account not in doc["accounts"]
                        and len(doc["accounts"]) >= MAX_ACCOUNTS
                    ):
                        raise RecoveryError("retained account limit")
                    events = [*events, event]
                state, result = _fold(events, self.authority_id, account)
                state["events"] = [dict(seq=i, **e) for i, e in enumerate(events, 1)]
                state["account_id"] = account
                state["authority_id"] = self.authority_id
                if event is not None:
                    doc["accounts"][account] = events
                    raw = _json(doc)
                    if len(raw) > MAX_BYTES:
                        raise RecoveryError("authority state exceeds byte limit")
                    connection.execute(
                        "UPDATE authority SET body=?, digest=? WHERE id=1",
                        (raw, _hash(raw)),
                    )
            if hook is not None:
                hook("before_commit")
            connection.commit()
            if hook is not None:
                hook("after_commit")
            return json.loads(_json(state)), json.loads(_json(result))
        except RecoveryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise RecoveryError("invalid or unavailable simulated authority") from exc
        finally:
            connection.close()

    def claim(
        self,
        account_id: str,
        owner_id: str,
        session_id: str,
        strategy_id: str,
        *,
        expected_generation: int,
        hook: Hook | None = None,
    ) -> FenceToken:
        _, result = self._operation(
            account_id,
            dict(
                kind="claim",
                owner_id=owner_id,
                session_id=session_id,
                strategy_id=strategy_id,
                expected_generation=expected_generation,
            ),
            hook,
        )
        return _token(result)

    def dispatch(
        self,
        token: FenceToken,
        command_id: str,
        request: NewOrderRequest | CancelRequest,
        *,
        timestamp_ns: int,
        hook: Hook | None = None,
    ) -> dict[str, Any]:
        if type(token) is not FenceToken or type(request) not in (
            NewOrderRequest,
            CancelRequest,
        ):
            raise RecoveryError("unsupported account command/token")
        _, result = self._operation(
            token.account_id,
            dict(
                kind="command",
                token=asdict(token),
                command_id=command_id,
                request=_record(request, timestamp_ns),
                timestamp_ns=timestamp_ns,
            ),
            hook,
        )
        return dict(result)

    def trade(
        self,
        account_id: str,
        side: Side,
        quantity: int,
        *,
        timestamp_ns: int,
        hook: Hook | None = None,
    ) -> list[dict[str, Any]]:
        if type(side) is not Side:
            raise RecoveryError("invalid synthetic trade side")
        _, result = self._operation(
            account_id,
            dict(
                kind="trade",
                side=side.value,
                quantity=quantity,
                timestamp_ns=timestamp_ns,
            ),
            hook,
        )
        return list(result)

    def snapshot(self, account_id: str) -> dict[str, Any]:
        return self._operation(account_id, None)[0]

    def lookup(self, account_id: str, command_id: str) -> dict[str, Any] | None:
        state = self.snapshot(account_id)
        result = next(
            (c["result"] for c in state["commands"] if c["command_id"] == command_id),
            None,
        )
        return result


def to_receipt(report: dict[str, Any], notification_timestamp_ns: int) -> Receipt:
    """Translate retained simulator evidence; old-generation evidence stays valid."""
    fill = report["fill"]
    payload = (
        None
        if fill is None
        else Fill(
            fill["fill_id"],
            fill["order_id"],
            fill["client_order_id"],
            fill["strategy_id"],
            Side(fill["side"]),
            fill["quantity"],
            fill["price_ticks"],
            LiquidityRole.MAKER,
            fill["exchange_fill_timestamp_ns"],
            Decimal(fill["fee"]),
            Decimal(fill["rebate"]),
        )
    )
    return Receipt(
        report["report_id"],
        notification_timestamp_ns,
        ExecutionReport(
            report["report_id"],
            ReportType(report["report_type"]),
            report["exchange_timestamp_ns"],
            report["order_id"],
            report["client_order_id"],
            OrderStatus(report["order_status"]),
            report["remaining_quantity"],
            payload,
            reason=report["reason"],
        ),
    )


class FencedRecoverySessionV3:
    """Explicit adapter: v2 local durability plus the modeled authority gate.

    Claim separately *before* constructing this adapter. Reopen v2 with its
    original identity; the current account token may belong to a new owner.
    Lost returns require authority evidence lookup, never automatic new retry.
    """

    def __init__(
        self,
        journal: OutboundRecoverySessionV2,
        authority: SimulatedAccountAuthorityV3,
        token: FenceToken,
    ) -> None:
        saved = journal.snapshot()
        if (
            token.authority_id != authority.authority_id
            or saved["account_id"] != token.account_id
        ):
            raise RecoveryError("adapter account/authority mismatch")
        self.journal, self.authority, self.token = journal, authority, token

    def submit_new(
        self,
        request: NewOrderRequest,
        *,
        timestamp_ns: int,
        deliver: Callable[[dict[str, Any]], None],
        hook: Hook | None = None,
    ) -> None:
        def sink(command: NewOrderRequest) -> None:
            result = self.authority.dispatch(
                self.token,
                "new-" + command.client_order_id,
                command,
                timestamp_ns=timestamp_ns,
            )
            deliver(result)

        self.journal.submit_new(
            request, timestamp_ns=timestamp_ns, sink=sink, hook=hook
        )

    def recover_cancel(
        self,
        client_order_id: str,
        *,
        timestamp_ns: int,
        deliver: Callable[[dict[str, Any]], None],
        hook: Hook | None = None,
    ) -> bool:
        state = self.journal.snapshot()
        work = next(
            (
                w
                for w in state["cancellation_work"]
                if w["client_order_id"] == client_order_id
            ),
            None,
        )
        if work is None:
            raise RecoveryError("unsaved cancellation identity")
        attempt = work["attempts"] + 1

        def sink(command: CancelRequest) -> None:
            result = self.authority.dispatch(
                self.token,
                f"cancel-{self.token.generation}-{client_order_id}-{attempt}",
                command,
                timestamp_ns=timestamp_ns,
            )
            deliver(result)

        return self.journal.recover_cancel(
            client_order_id,
            timestamp_ns=timestamp_ns,
            sink=sink,
            hook=hook,
        )
