"""Actual client process; venue and delayed transport survive in the parent.

The only interprocess command channel is a synchronous JSON line sink. No
strategy object or memory checkpoint crosses process death/reopen.
"""

from __future__ import annotations

import argparse
import json
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lobmm.active_restart import GuardedStrategySession
from lobmm.config import StrategyConfig
from lobmm.durable_recovery import DurableClient, RecoveryError, _report, _wire
from lobmm.enums import EventType, Side
from lobmm.events import MarketEvent
from lobmm.orders import CancelRequest, NewOrderRequest
from lobmm.strategies import make_strategy


def strategy() -> Any:
    return make_strategy(
        StrategyConfig(
            strategy_id="restart-witness",
            order_size=6,
            base_half_spread_ticks=2,
            minimum_quote_lifetime_ns=0,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("mode", choices=("new", "cancel", "recover"))
    parser.add_argument("--side", type=int, choices=(1, -1), default=1)
    parser.add_argument("--kill-hook")
    parser.add_argument("--evidence", action="store_true")
    args = parser.parse_args()
    journal = args.root / "session.json"
    constructor = (
        GuardedStrategySession.open
        if args.mode == "recover"
        else GuardedStrategySession.create
    )
    session = constructor(journal, "active-witness", strategy(), 12)
    if args.mode == "recover":
        before = journal.read_bytes()
        blocked: list[str] = []
        effects: list[Any] = []
        for allowed in (True, False):
            try:
                session.decide(timestamp_ns=100, allow_new_quotes=allowed)
            except RecoveryError:
                blocked.append(f"decide:{allowed}")
            else:
                raise AssertionError("reopened decision was allowed")
        cached = NewOrderRequest(
            "restart-witness-000000000001",
            Side.BID,
            100,
            6,
            "restart-witness",
            10,
            10,
        )
        for action in (
            cached,
            CancelRequest(100, 100, client_order_id=cached.client_order_id),
        ):
            try:
                session.dispatch(action, timestamp_ns=100, sink=effects.append)
            except RecoveryError:
                blocked.append(type(action).__name__)
            else:
                raise AssertionError("reopened dispatch was allowed")
        assessment = None
        if args.evidence:
            identity = json.loads((args.root / "evidence-identity.json").read_text())
            client = DurableClient.open(
                args.root / "checkpoint.json",
                args.root / "inbox.json",
                "active-witness",
                identity["inbox_sha256"],
            )
            assessment = session.assessment(client)
        assert not effects and journal.read_bytes() == before
        print(
            json.dumps(
                dict(
                    snapshot=session.snapshot(),
                    blocked=blocked,
                    effects=effects,
                    assessment=assessment,
                ),
                sort_keys=True,
            ),
            flush=True,
        )
        return

    def pause(stage: str) -> None:
        if stage == args.kill_hook:
            print(json.dumps(dict(ready=stage)), flush=True)
            while True:
                signal.pause()

    selected_kind = args.mode

    # Reports cannot re-enter the guarded API during dispatch. Queue them in the
    # adapter until dispatch has returned, just as an event loop would do.
    pending_reports: list[Any] = []

    def queued_sink(action: Any) -> None:
        kind = "new" if isinstance(action, NewOrderRequest) else "cancel"
        print(json.dumps(dict(command=kind, payload=_wire(asdict(action)))), flush=True)
        response = json.loads(input())
        if kind == selected_kind:
            pause("sink_effect")
        pending_reports.extend(response["reports"])

    for event in (
        MarketEvent(1, 1, EventType.ADD, Side.BID, 100, 10),
        MarketEvent(2, 2, EventType.ADD, Side.ASK, 104, 10),
    ):
        session.deliver_market(event, notification_timestamp_ns=event.timestamp_ns)
    actions = session.decide(timestamp_ns=10)
    new = next(
        a
        for a in actions
        if isinstance(a, NewOrderRequest) and int(a.side) == args.side
    )
    session.dispatch(
        new,
        timestamp_ns=10,
        sink=queued_sink,
        hook=pause if args.mode == "new" else None,
    )
    if args.mode == "new":
        pause("after_ack")
    for report in pending_reports:
        session.deliver_report(_report(report), notification_timestamp_ns=20)
    if args.mode == "cancel":
        actions = session.decide(timestamp_ns=30, allow_new_quotes=False)
        cancel = next(
            a
            for a in actions
            if isinstance(a, CancelRequest)
            and (a.order_id is not None or a.client_order_id == new.client_order_id)
        )
        session.dispatch(cancel, timestamp_ns=30, sink=queued_sink, hook=pause)
        pause("after_ack")
    print(json.dumps(dict(completed=True)), flush=True)


if __name__ == "__main__":
    main()
