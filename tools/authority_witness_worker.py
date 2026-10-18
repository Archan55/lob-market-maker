"""Fresh-process issuance/command worker with explicit protocol-boundary barriers."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from lobmm.account_fence import FenceToken
from lobmm.authority_witness import WitnessedAccountAuthorityV4, WitnessToken
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def main():
    request = json.loads(sys.stdin.readline())
    authority = WitnessedAccountAuthorityV4(
        Path(sys.argv[1]), Path(sys.argv[2]), "venue", "witness", "incarnation"
    )
    emit({"ready": True})
    assert sys.stdin.readline().strip() == "go"

    def hook(boundary):
        if request.get("pause") == boundary:
            emit({"boundary": boundary})
            assert sys.stdin.readline().strip() == "go"

    try:
        if request["operation"] == "claim":
            result = asdict(
                authority.claim(
                    request["issuance_id"],
                    "account",
                    request["owner"],
                    request["owner"],
                    "strategy",
                    expected_generation=request.get("expected_generation", 1),
                    hook=hook,
                )
            )
        else:
            raw = request["token"]
            token = WitnessToken(
                raw["witness_id"],
                raw["incarnation_id"],
                raw["issuance_id"],
                FenceToken(**raw["context"]),
            )
            action = (
                NewOrderRequest("unaccepted", Side.BID, 101, 6, "strategy", 1, 1)
                if request["operation"] == "new"
                else CancelRequest(1, 1, client_order_id="six")
            )
            result = authority.dispatch(
                token,
                request.get("command_id", "cancel"),
                action,
                timestamp_ns=10,
                hook=hook,
            )
        emit({"result": result})
    except RecoveryError as exc:
        emit({"error": str(exc)})


if __name__ == "__main__":
    main()
