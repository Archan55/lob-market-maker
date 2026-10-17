"""Fresh-interpreter primary fence worker with explicit pre-operation barrier."""

from __future__ import annotations

import json
import sys
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
    request = json.loads(sys.stdin.readline())
    authority = SimulatedAccountAuthorityV3.open(Path(sys.argv[1]), "primary-authority")
    emit({"ready": True})
    if sys.stdin.readline().strip() != "go":
        raise RuntimeError("missing barrier release")

    if request.get("announce"):
        emit({"attempting": True})

    def hook(boundary: str) -> None:
        if request.get("pause") == boundary:
            emit({"boundary": boundary})
            if sys.stdin.readline().strip() != "go":
                raise RuntimeError("missing transaction release")

    try:
        if request["operation"] == "claim":
            token = authority.claim(
                "primary-account",
                request["owner"],
                request["owner"],
                "primary-strategy",
                expected_generation=request["expected_generation"],
                hook=hook,
            )
            emit({"result": asdict(token)})
        else:
            token = FenceToken(**request["token"])
            if request["operation"] == "new":
                action: NewOrderRequest | CancelRequest = NewOrderRequest(
                    request["client_order_id"],
                    Side(request.get("side", 1)),
                    101,
                    6,
                    "primary-strategy",
                    1,
                    1,
                )
            else:
                action = CancelRequest(1, 1, client_order_id=request["client_order_id"])
            report = authority.dispatch(
                token,
                request["command_id"],
                action,
                timestamp_ns=request.get("timestamp_ns", 10),
                hook=hook,
            )
            emit({"result": report})
    except RecoveryError as exc:
        emit({"error": str(exc)})


if __name__ == "__main__":
    main()
