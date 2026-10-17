"""Isolated disk-only recovery worker used by the process-death experiment.

The parent never passes a cloned client. At a selected persistence boundary this
process announces readiness and waits for SIGKILL. Reopening happens in another
fresh interpreter, using only the committed checkpoint and immutable inbox.
"""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path

from lobmm.durable_recovery import DurableClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("inbox", type=Path)
    parser.add_argument("session")
    parser.add_argument("inbox_sha256")
    parser.add_argument("--kill-hook")
    parser.add_argument("--one", action="store_true")
    args = parser.parse_args()
    client = DurableClient.open(
        args.checkpoint,
        args.inbox,
        session_id=args.session,
        inbox_sha256=args.inbox_sha256,
    )
    recovered_offset = client.offset
    recovered_snapshot = client.snapshot()
    returned_to_caller = False

    def hook(boundary: str) -> None:
        if boundary == args.kill_hook:
            print(
                json.dumps(
                    {
                        "ready": boundary,
                        "offset_before": recovered_offset,
                        "returned_to_caller": returned_to_caller,
                    }
                ),
                flush=True,
            )
            while True:
                signal.pause()

    while client.advance(hook=hook):
        returned_to_caller = True
        # This boundary is in the caller, after advance has actually returned.
        # It is distinct from a callback inside persistence before return.
        if args.kill_hook == "after_ack":
            hook("after_ack")
        if args.one:
            break
    print(
        json.dumps(
            dict(
                recovered_offset=recovered_offset,
                recovered_snapshot=recovered_snapshot,
                final_offset=client.offset,
                final_snapshot=client.snapshot(),
            ),
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
