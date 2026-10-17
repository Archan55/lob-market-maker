"""Unchanged practical source-binding probe for pinned v3 before/after runs.

The initial boundary exposed bare report conversion followed by v2 capture.
The corrected adapter supplies source-bound capture. Genuine evidence from a
different modeled account shares a client ID; accepting it can suppress the
own account's cancellation and permit seven avoidable fills. This helper uses
the initial conversion flow only when the corrected method is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from lobmm.account_fence import (
    FencedRecoverySessionV3,
    SimulatedAccountAuthorityV3,
    to_receipt,
)
from lobmm.durable_recovery import RecoveryError
from lobmm.enums import Side
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.outbound_recovery import OutboundRecoverySessionV2


def witness() -> dict[str, Any]:
    with TemporaryDirectory() as directory:
        path = Path(directory)
        venue = SimulatedAccountAuthorityV3.create(
            path / "authority.sqlite", "independent-review"
        )
        own = venue.claim(
            "own-account", "own-owner", "own-session", "review", expected_generation=0
        )
        foreign = venue.claim(
            "foreign-account",
            "foreign-owner",
            "foreign-session",
            "review",
            expected_generation=0,
        )
        request = NewOrderRequest("shared-client-id", Side.BID, 113, 7, "review", 5, 6)
        venue.dispatch(foreign, "foreign-new", request, timestamp_ns=10)
        wrong = venue.dispatch(
            foreign,
            "foreign-cancel",
            CancelRequest(20, 20, client_order_id=request.client_order_id),
            timestamp_ns=20,
        )
        with OutboundRecoverySessionV2.create(
            path / "client", "own-account", "own-session", "review"
        ) as journal:
            adapter = FencedRecoverySessionV3(journal, venue, own)
            adapter.submit_new(request, timestamp_ns=10, deliver=lambda _: None)
            before = journal.snapshot()

            def initial_capture(
                report: dict[str, Any], *, notification_timestamp_ns: int
            ) -> bool:
                return journal.capture(to_receipt(report, notification_timestamp_ns))

            capture = getattr(adapter, "capture", initial_capture)
            rejected = False
            try:
                capture(wrong, notification_timestamp_ns=21)
            except RecoveryError:
                rejected = True
            after_foreign = journal.snapshot()
            delivered: list[dict[str, Any]] = []
            cancel_attempted = adapter.recover_cancel(
                request.client_order_id, timestamp_ns=22, deliver=delivered.append
            )
            for report in delivered:
                capture(report, notification_timestamp_ns=23)
            reports = venue.trade("own-account", Side.BID, 7, timestamp_ns=30)
            actual = venue.snapshot("own-account")
            scalar_quantity = sum(report["fill"]["quantity"] for report in reports)
            scalar_cash = -sum(
                report["fill"]["quantity"] * report["fill"]["price_ticks"]
                for report in reports
            )
            assert (actual["inventory"], actual["cash_ticks"]) == (
                scalar_quantity,
                scalar_cash,
            )
            return {
                "synthetic_uncalibrated": True,
                "foreign_source_rejected": rejected,
                "terminal_before": before["cancellation_work"][0]["terminal"],
                "terminal_after_foreign": after_foreign["cancellation_work"][0][
                    "terminal"
                ],
                "own_cancel_attempted": cancel_attempted,
                "own_cancel_reports": len(delivered),
                "actual_inventory": scalar_quantity,
                "actual_cash_ticks": scalar_cash,
                "live_residual": sum(o["remaining_quantity"] for o in actual["orders"]),
                "foreign_terminal": wrong,
                "own_account_events": actual["events"],
            }


if __name__ == "__main__":
    print(json.dumps(witness(), sort_keys=True))
