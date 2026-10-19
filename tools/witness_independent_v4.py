"""Independent v4 raw-input reconstruction and separate interpreter worker.

The accounting auditor uses only SQLite JSON input events and integer arithmetic.
It never imports a production fold, replay function, or study oracle. Public API
imports are confined to the subprocess worker used for process experiments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any


def read_store(path: Path, table: str) -> dict[str, Any]:
    if table not in {"authority", "witness"}:
        raise ValueError("unsupported retained store")
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            f"SELECT body,digest FROM {table} WHERE id=1"
        ).fetchone()
        assert row is not None and isinstance(row[0], bytes)
        assert hashlib.sha256(row[0]).hexdigest() == row[1]
        return {"body": json.loads(row[0]), "sha256": row[1]}
    finally:
        connection.close()


def reconstruct(document: dict[str, Any]) -> dict[str, Any]:
    """Derive quantities and identity from event inputs, never venue snapshots.

    Reviewed cancellation cases intentionally address immutable client IDs.
    Exchange-ID cancellation requires a separately observed ID mapping, so this
    bounded independent auditor refuses it instead of copying production's ID
    algorithm or silently trusting a venue-derived order list.
    """
    accounts: dict[str, Any] = {}
    for account_id, events in document["accounts"].items():
        generation = 0
        owner: dict[str, Any] = {}
        owners: list[str] = []
        commands: dict[str, Any] = {}
        orders: dict[str, Any] = {}
        inventory = cash = last_timestamp = 0
        fills: list[dict[str, Any]] = []
        for sequence, event in enumerate(events, 1):
            kind = event["kind"]
            if kind == "claim":
                assert event["expected_generation"] == generation
                assert event["owner_id"] not in owners
                owners.append(event["owner_id"])
                generation += 1
                owner = {
                    "authority_id": document["authority_id"],
                    "account_id": account_id,
                    "generation": generation,
                    **{
                        key: event[key]
                        for key in ("owner_id", "session_id", "strategy_id")
                    },
                }
                continue
            stamp = event["timestamp_ns"]
            assert stamp >= last_timestamp
            last_timestamp = stamp
            if kind == "command":
                assert event["token"] == owner
                record = event["request"]
                assert record["dispatch_timestamp_ns"] == stamp
                command_id = event["command_id"]
                identity = {
                    "token": event["token"],
                    "kind": record["kind"],
                    "payload": record["payload"],
                }
                if command_id in commands:
                    assert commands[command_id] == identity
                    continue
                commands[command_id] = identity
                payload = record["payload"]
                if record["kind"] == "new":
                    assert generation == 1
                    client_id = payload["client_order_id"]
                    assert client_id not in orders
                    assert payload["strategy_id"] == owner["strategy_id"]
                    orders[client_id] = {
                        "client_order_id": client_id,
                        "side": payload["side"],
                        "price_ticks": payload["price_ticks"],
                        "original_quantity": payload["quantity"],
                        "remaining_quantity": payload["quantity"],
                        "filled_quantity": 0,
                        "cancelled_quantity": 0,
                        "accepted_generation": generation,
                    }
                else:
                    assert record["kind"] == "cancel"
                    assert payload["order_id"] is None, (
                        "auditor needs explicit venue-ID map"
                    )
                    target = orders.get(payload["client_order_id"])
                    if target is not None:
                        target["cancelled_quantity"] += target["remaining_quantity"]
                        target["remaining_quantity"] = 0
                continue
            assert kind == "trade" and owner
            budget = event["quantity"]
            side = event["side"]
            assert side in (-1, 1) and budget > 0
            for client_id, order in orders.items():
                if order["side"] != side:
                    continue
                executed = min(budget, order["remaining_quantity"])
                if not executed:
                    continue
                budget -= executed
                order["remaining_quantity"] -= executed
                order["filled_quantity"] += executed
                inventory += side * executed
                cash -= side * executed * order["price_ticks"]
                fills.append(
                    {
                        "event_sequence": sequence,
                        "client_order_id": client_id,
                        "quantity": executed,
                        "side": side,
                        "price_ticks": order["price_ticks"],
                    }
                )
        for order in orders.values():
            assert (
                order["original_quantity"]
                == order["remaining_quantity"]
                + order["filled_quantity"]
                + order["cancelled_quantity"]
            )
        accounts[account_id] = {
            "generation": generation,
            "current_owner": owner,
            "retained_owners": owners,
            "command_ids": list(commands),
            "orders": list(orders.values()),
            "fills_from_inputs": fills,
            "inventory": inventory,
            "cash_ticks": cash,
            "event_count": len(events),
        }
    return accounts


def worker(args: argparse.Namespace) -> None:
    from lobmm.account_fence import FenceToken
    from lobmm.authority_witness import WitnessedAccountAuthorityV4, WitnessToken
    from lobmm.durable_recovery import RecoveryError
    from lobmm.enums import Side
    from lobmm.orders import CancelRequest, NewOrderRequest

    facade = WitnessedAccountAuthorityV4.open(
        args.authority,
        args.witness,
        args.authority_id,
        args.witness_id,
        args.incarnation_id,
    )

    def emit(value: Any) -> None:
        print(json.dumps(value, sort_keys=True), flush=True)

    emit({"ready": True})
    while True:
        try:
            operation = json.loads(input())
        except EOFError:
            return

        selected_pause = operation.get("pause")

        def hook(stage: str, selected_pause: str | None = selected_pause) -> None:
            if stage == selected_pause:
                emit({"paused": stage})
                while True:
                    signal.pause()

        try:
            if operation["operation"] == "claim":
                result: Any = asdict(
                    facade.claim(
                        operation["issuance_id"],
                        operation["account_id"],
                        operation["owner_id"],
                        operation["session_id"],
                        operation["strategy_id"],
                        expected_generation=operation["expected_generation"],
                        hook=hook,
                    )
                )
            elif operation["operation"] == "dispatch":
                raw = operation["token"]
                token = WitnessToken(
                    raw["witness_id"],
                    raw["incarnation_id"],
                    raw["issuance_id"],
                    FenceToken(**raw["context"]),
                )
                payload = operation["request"]
                request: CancelRequest | NewOrderRequest
                if operation["kind"] == "new":
                    request = NewOrderRequest(
                        **(payload | {"side": Side(payload["side"])})
                    )
                else:
                    request = CancelRequest(**payload)
                result = facade.dispatch(
                    token,
                    operation["command_id"],
                    request,
                    timestamp_ns=operation["timestamp_ns"],
                    hook=hook,
                )
            else:
                raise ValueError("unsupported independent operation")
            emit({"result": result})
        except RecoveryError as exc:
            emit({"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("authority", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--witness", type=Path)
    parser.add_argument("--authority-id", default="independent-authority-v4")
    parser.add_argument("--witness-id", default="independent-witness-v4")
    parser.add_argument("--incarnation-id", default="independent-incarnation-v4")
    args = parser.parse_args()
    if args.worker:
        worker(args)
    else:
        raw = read_store(args.authority, "authority")
        print(
            json.dumps(
                {"retained": raw, "reconstructed": reconstruct(raw["body"])},
                sort_keys=True,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
