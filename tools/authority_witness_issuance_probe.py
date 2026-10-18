"""Unchanged semantic-malformation probe for pinned before/after source."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from witness_independent_v4 import read_store, reconstruct

from lobmm.account_fence import SimulatedAccountAuthorityV3
from lobmm.authority_witness import WitnessedAccountAuthorityV4
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="witness-probe-") as temporary:
        root = Path(temporary)
        venue = SimulatedAccountAuthorityV3.create(root / "venue", "probe")
        original = venue.claim("account", "a", "a", "strategy", expected_generation=0)
        venue.dispatch(
            original,
            "accepted",
            NewOrderRequest("seven", Side.BID, 113, 7, "strategy", 1, 1),
            timestamp_ns=1,
        )
        facade = WitnessedAccountAuthorityV4.enroll(
            venue.path, root / "witness", "probe", "probe-w", "probe-i"
        )
        token = facade.claim(
            "takeover", "account", "b", "b", "strategy", expected_generation=1
        )
        evidence = read_store(facade.witness_path, "witness")["body"]
        evidence["issuances"][0]["request"]["owner_id"] = "contradictory-owner"
        encoded = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        with sqlite3.connect(facade.witness_path) as connection:
            connection.execute(
                "UPDATE witness SET body=?,digest=? WHERE id=1",
                (encoded, hashlib.sha256(encoded).hexdigest()),
            )
        before = read_store(venue.path, "authority")
        refused = False
        try:
            facade.dispatch(
                token,
                "cancel",
                CancelRequest(2, 2, client_order_id="seven"),
                timestamp_ns=2,
            )
        except RecoveryError:
            refused = True
        after = read_store(venue.path, "authority")
        oracle = reconstruct(after["body"])
        print(
            json.dumps(
                dict(
                    refused=refused,
                    authority_unchanged=before == after,
                    raw_authority=after,
                    malformed_witness=evidence,
                    independently_reconstructed=oracle,
                ),
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
