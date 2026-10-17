"""Disposable client process; the caller owns the independently surviving venue.

The JSON-lines channel transfers commands/receipts only. A new interpreter opens
the journal after each SIGKILL; no Python client state is transferred on restart.
"""

from __future__ import annotations

import argparse
import json
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lobmm.durable_recovery import RecoveryError, _receipt, _wire
from lobmm.enums import Side
from lobmm.orders import NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("mode", choices=("create", "open"))
    args = parser.parse_args()
    constructor = (
        OutboundRecoverySessionV2.create
        if args.mode == "create"
        else OutboundRecoverySessionV2.open
    )
    try:
        session = constructor(args.root, "study-account", "study-session", "study")
    except RecoveryError as exc:
        emit(dict(open_error=str(exc)))
        return
    with session:
        emit(dict(opened=session.snapshot()))
        while True:
            try:
                operation = json.loads(input())
            except EOFError:
                return
            selected_hook = operation.get("kill_hook")

            def pause(stage: str, selected_hook: str | None = selected_hook) -> None:
                if stage == selected_hook:
                    emit(dict(ready=stage))
                    while True:
                        signal.pause()

            def sink(action: Any) -> None:
                kind = "new" if isinstance(action, NewOrderRequest) else "cancel"
                emit(dict(command=kind, payload=_wire(asdict(action))))
                response = json.loads(input())
                if response != {"transport_returned": True}:
                    raise RuntimeError("invalid simulated transport acknowledgment")
                pause("sink_effect")

            kind = operation["operation"]
            if kind == "close":
                emit(dict(closed=True))
                return
            try:
                value: Any = None
                if kind == "new":
                    raw = operation["request"]
                    request = NewOrderRequest(**(raw | {"side": Side(raw["side"])}))
                    session.submit_new(
                        request,
                        timestamp_ns=operation["timestamp_ns"],
                        sink=sink,
                        hook=pause,
                    )
                elif kind == "cancel":
                    value = session.recover_cancel(
                        operation["client_order_id"],
                        timestamp_ns=operation["timestamp_ns"],
                        sink=sink,
                        hook=pause,
                    )
                elif kind == "capture":
                    value = session.capture(_receipt(operation["receipt"]), hook=pause)
                elif kind != "snapshot":
                    raise RuntimeError("unsupported worker operation")
                pause("after_ack")
                emit(dict(result=value, snapshot=session.snapshot()))
            except RecoveryError as exc:
                emit(dict(error=str(exc)))


if __name__ == "__main__":
    main()
