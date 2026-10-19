"""Separate interpreter for independently authored account-fence race probes.

This worker uses only the opt-in public synthetic authority API. The parent
coordinates genuinely live processes using JSON lines; a paused committed or
uncommitted transaction can be killed with the actual operating-system signal.
"""

from __future__ import annotations

import argparse
import json
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lobmm.account_fence import FenceToken, SimulatedAccountAuthorityV3
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("authority_id")
    args = parser.parse_args()
    authority = SimulatedAccountAuthorityV3.open(args.path, args.authority_id)
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
            name = operation["operation"]
            account = operation.get("account_id", "independent-account")
            result: Any
            if name == "claim":
                result = asdict(
                    authority.claim(
                        account,
                        operation["owner_id"],
                        operation["session_id"],
                        operation.get("strategy_id", "independent-review"),
                        expected_generation=operation["expected_generation"],
                        hook=hook,
                    )
                )
            elif name == "dispatch":
                token = FenceToken(**operation["token"])
                raw = operation["request"]
                request: NewOrderRequest | CancelRequest
                if operation["kind"] == "new":
                    request = NewOrderRequest(**(raw | {"side": Side(raw["side"])}))
                else:
                    request = CancelRequest(**raw)
                result = authority.dispatch(
                    token,
                    operation["command_id"],
                    request,
                    timestamp_ns=operation["timestamp_ns"],
                    hook=hook,
                )
            elif name == "trade":
                result = authority.trade(
                    account,
                    Side(operation["side"]),
                    operation["quantity"],
                    timestamp_ns=operation["timestamp_ns"],
                    hook=hook,
                )
            elif name == "lookup":
                result = authority.lookup(account, operation["command_id"])
            elif name == "snapshot":
                result = authority.snapshot(account)
            else:
                raise ValueError("unsupported independent worker operation")
            emit({"result": result})
        except RecoveryError as exc:
            emit({"error": str(exc)})


if __name__ == "__main__":
    main()
