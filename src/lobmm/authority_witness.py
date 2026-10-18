"""Cancellation-only synthetic witness protocol, conditional on independent retention.

Two local files do not establish independent durable infrastructure. A surviving
write intent freezes the facade; joint rollback and direct v3 bypass are outside
its guarantee. No live fencing, authentication or process exclusivity claim.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from lobmm.account_fence import (
    FenceToken,
    SimulatedAccountAuthorityV3,
    _fold,
    _token,
    _validate,
)
from lobmm.active_restart import _record
from lobmm.durable_recovery import (
    MAX_BYTES,
    Hook,
    RecoveryError,
    _constant,
    _hash,
    _integer,
    _json,
    _object,
    _pairs,
    _text,
)
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest

VERSION = "independently-retained-witness-v4"
MAX_ISSUANCES = 64
MAX_MUTATIONS = 4096


@dataclass(frozen=True, slots=True)
class WitnessToken:
    witness_id: str
    incarnation_id: str
    issuance_id: str
    context: FenceToken


def _issued(raw: Any) -> WitnessToken:
    d = _object(raw, ("witness_id", "incarnation_id", "issuance_id", "context"))
    return WitnessToken(
        _text(d["witness_id"]),
        _text(d["incarnation_id"]),
        _text(d["issuance_id"]),
        _token(d["context"]),
    )


def _read(connection: sqlite3.Connection, table: str) -> tuple[dict[str, Any], str]:
    # Table names are internal constants, never caller input.
    row = connection.execute(f"SELECT body, digest FROM {table} WHERE id=1").fetchone()
    if (
        row is None
        or type(row[0]) is not bytes
        or len(row[0]) > MAX_BYTES
        or _hash(row[0]) != row[1]
    ):
        raise RecoveryError("invalid retained witness/authority bytes")
    return json.loads(row[0], object_pairs_hook=_pairs, parse_constant=_constant), row[
        1
    ]


def _write(connection: sqlite3.Connection, doc: dict[str, Any]) -> None:
    raw = _json(doc)
    if len(raw) > MAX_BYTES:
        raise RecoveryError("witness byte bound")
    connection.execute(
        "UPDATE witness SET body=?, digest=? WHERE id=1", (raw, _hash(raw))
    )


class WitnessedAccountAuthorityV4:
    """Opt-in facade. Enrollment requires a quiescent, trusted v3 baseline.

    Every facade operation checks a separately retained witness. The underlying
    v3 object is not a fenced external venue; direct calls bypass this boundary.
    """

    def __init__(
        self,
        authority_path: Path,
        witness_path: Path,
        authority_id: str,
        witness_id: str,
        incarnation_id: str,
    ) -> None:
        self.authority = SimulatedAccountAuthorityV3(authority_path, authority_id)
        self.witness_path = witness_path.resolve()
        self.witness_id = _text(witness_id)
        self.incarnation_id = _text(incarnation_id)
        if self.witness_path == self.authority.path:
            raise RecoveryError("witness and authority must be separate files")

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                f"file:{quote(str(self.witness_path), safe='/')}?mode=rw",
                uri=True,
                timeout=10,
            )
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.Error as exc:
            raise RecoveryError("independent witness unavailable") from exc

    def _validate_witness(self, doc: Any) -> None:
        d = _object(
            doc,
            (
                "version",
                "witness_id",
                "incarnation_id",
                "authority_id",
                "authority_digest",
                "watermark",
                "pending",
                "issuances",
            ),
        )
        if (
            d["version"] != VERSION
            or d["witness_id"] != self.witness_id
            or d["incarnation_id"] != self.incarnation_id
            or d["authority_id"] != self.authority.authority_id
        ):
            raise RecoveryError("witness lineage mismatch")
        digest = _text(d["authority_digest"])
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RecoveryError("invalid witness authority digest")
        if _integer(d["watermark"]) > MAX_MUTATIONS:
            raise RecoveryError("witness mutation bound")
        rows = d["issuances"]
        if type(rows) is not list or len(rows) > MAX_ISSUANCES:
            raise RecoveryError("witness issuance bound")
        identities = set()
        for row in rows:
            r = _object(row, ("issuance_id", "request", "token"))
            identity = _text(r["issuance_id"])
            token = _issued(r["token"])
            request = _object(
                r["request"],
                (
                    "account_id",
                    "owner_id",
                    "session_id",
                    "strategy_id",
                    "expected_generation",
                ),
            )
            for name in ("account_id", "owner_id", "session_id", "strategy_id"):
                _text(request[name])
            _integer(request["expected_generation"])
            if (
                identity in identities
                or token.issuance_id != identity
                or token.witness_id != self.witness_id
                or token.incarnation_id != self.incarnation_id
                or token.context.authority_id != self.authority.authority_id
            ):
                raise RecoveryError("invalid retained issuance identity")
            identities.add(identity)
        if d["pending"] is not None:
            _object(d["pending"], ("watermark", "old_digest", "proposed_digest"))
            # Even an apparently matching pending state is never auto-repaired.
            raise RecoveryError("pending witness intent; manual evidence required")

    @classmethod
    def enroll(
        cls,
        authority_path: Path,
        witness_path: Path,
        authority_id: str,
        witness_id: str,
        incarnation_id: str,
    ) -> Self:
        result = cls(
            authority_path, witness_path, authority_id, witness_id, incarnation_id
        )
        connection = result.authority._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            venue, digest = _read(connection, "authority")
            _validate(venue, authority_id)
            fd = os.open(
                result.witness_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(fd)
            witness = result._connect()
            try:
                witness.execute("PRAGMA journal_mode=DELETE")
                witness.execute(
                    "CREATE TABLE witness (id INTEGER PRIMARY KEY CHECK(id=1), body BLOB NOT NULL, digest TEXT NOT NULL)"
                )
                doc = dict(
                    version=VERSION,
                    witness_id=witness_id,
                    incarnation_id=incarnation_id,
                    authority_id=authority_id,
                    authority_digest=digest,
                    watermark=0,
                    pending=None,
                    issuances=[],
                )
                raw = _json(doc)
                witness.execute(
                    "INSERT INTO witness VALUES (1, ?, ?)", (raw, _hash(raw))
                )
                witness.commit()
            finally:
                witness.close()
        finally:
            connection.close()
        return result

    @classmethod
    def open(
        cls,
        authority_path: Path,
        witness_path: Path,
        authority_id: str,
        witness_id: str,
        incarnation_id: str,
    ) -> Self:
        result = cls(
            authority_path, witness_path, authority_id, witness_id, incarnation_id
        )
        result._operation(None, None)
        return result

    def _operation(
        self,
        account: str | None,
        event: dict[str, Any] | None,
        *,
        issuance: dict[str, Any] | None = None,
        token: WitnessToken | None = None,
        hook: Hook | None = None,
    ) -> tuple[dict[str, Any], Any]:
        witness = self._connect()
        venue: sqlite3.Connection | None = None
        try:
            witness.execute("BEGIN IMMEDIATE")
            evidence, _ = _read(witness, "witness")
            self._validate_witness(evidence)
            venue = self.authority._connect()
            venue.execute("BEGIN IMMEDIATE")
            doc, digest = _read(venue, "authority")
            _validate(doc, self.authority.authority_id)
            if digest != evidence["authority_digest"]:
                raise RecoveryError("authority/witness watermark mismatch")
            if token is not None and not any(
                r["token"] == asdict(token) for r in evidence["issuances"]
            ):
                raise RecoveryError("token lacks retained issuance")
            if issuance is not None:
                previous = next(
                    (
                        r
                        for r in evidence["issuances"]
                        if r["issuance_id"] == issuance["issuance_id"]
                    ),
                    None,
                )
                if previous is not None:
                    if previous["request"] != issuance["request"]:
                        raise RecoveryError("conflicting issuance request identity")
                    return {}, previous["token"]
                if len(evidence["issuances"]) >= MAX_ISSUANCES:
                    raise RecoveryError("witness issuance bound")
            state: dict[str, Any] = {}
            result: Any = None
            if account is not None:
                events = doc["accounts"].get(_text(account), [])
                if event is not None:
                    events = [*events, event]
                state, result = _fold(events, self.authority.authority_id, account)
                state["events"] = [dict(seq=i, **e) for i, e in enumerate(events, 1)]
                state["underlying_entry_enabled"] = state["entry_enabled"]
                state["entry_enabled"] = False
                state["witness_watermark"] = evidence["watermark"] + (event is not None)
                state["scope"] = "cancellation-only-independently-retained-witness"
                if event is not None:
                    doc["accounts"][account] = events
                    _validate(doc, self.authority.authority_id)
            if event is None:
                return state, result
            if evidence["watermark"] >= MAX_MUTATIONS:
                raise RecoveryError("witness mutation bound")
            raw = _json(doc)
            if len(raw) > MAX_BYTES:
                raise RecoveryError("authority byte bound")
            if issuance is not None:
                result = asdict(
                    WitnessToken(
                        self.witness_id,
                        self.incarnation_id,
                        issuance["issuance_id"],
                        _token(result),
                    )
                )
            if hook:
                hook("before_intent")
            evidence["pending"] = dict(
                watermark=evidence["watermark"] + 1,
                old_digest=digest,
                proposed_digest=_hash(raw),
            )
            _write(witness, evidence)
            witness.commit()  # independent intent precedes all venue effects
            if hook:
                hook("after_intent")
            witness.execute("BEGIN IMMEDIATE")
            retained, _ = _read(witness, "witness")
            if retained != evidence:
                raise RecoveryError("witness changed during pending operation")
            venue.execute(
                "UPDATE authority SET body=?, digest=? WHERE id=1", (raw, _hash(raw))
            )
            venue.commit()
            if hook:
                hook("after_authority_commit")
            evidence["authority_digest"] = _hash(raw)
            evidence["watermark"] += 1
            evidence["pending"] = None
            if issuance is not None:
                evidence["issuances"].append(dict(**issuance, token=result))
            _write(witness, evidence)
            witness.commit()
            if hook:
                hook("after_finalize")
            return state, result
        except RecoveryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise RecoveryError("invalid or unavailable witnessed authority") from exc
        finally:
            if venue is not None:
                venue.close()
            witness.close()

    def claim(
        self,
        issuance_id: str,
        account_id: str,
        owner_id: str,
        session_id: str,
        strategy_id: str,
        *,
        expected_generation: int,
        hook: Hook | None = None,
    ) -> WitnessToken:
        request = dict(
            account_id=_text(account_id),
            owner_id=_text(owner_id),
            session_id=_text(session_id),
            strategy_id=_text(strategy_id),
            expected_generation=_integer(expected_generation),
        )
        _, result = self._operation(
            account_id,
            dict(
                kind="claim", **{k: v for k, v in request.items() if k != "account_id"}
            ),
            issuance=dict(issuance_id=_text(issuance_id), request=request),
            hook=hook,
        )
        return _issued(result)

    def dispatch(
        self,
        token: WitnessToken,
        command_id: str,
        request: NewOrderRequest | CancelRequest,
        *,
        timestamp_ns: int,
        hook: Hook | None = None,
    ) -> dict[str, Any]:
        if type(token) is not WitnessToken:
            raise RecoveryError("invalid witnessed token")
        if type(request) is not CancelRequest:
            raise RecoveryError("new entry quarantined; cancellation only")
        _, result = self._operation(
            token.context.account_id,
            dict(
                kind="command",
                token=asdict(token.context),
                command_id=command_id,
                request=_record(request, timestamp_ns),
                timestamp_ns=timestamp_ns,
            ),
            token=token,
            hook=hook,
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
            hook=hook,
        )
        return list(result)

    def snapshot(self, account_id: str) -> dict[str, Any]:
        return self._operation(account_id, None)[0]
