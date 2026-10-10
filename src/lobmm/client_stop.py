"""Client knowledge and cancellation work, separate from venue truth.

This orderly stop keeps report reception and cancellation transport alive. It
does not model a process crash, disconnect, venue-enforced cancel-on-disconnect,
or guaranteed cancellation. Unknown-order rejection never terminates an entry.
"""

from __future__ import annotations

from collections.abc import Callable

from lobmm.enums import ReportType
from lobmm.orders import CancelRequest, ExecutionReport, NewOrderRequest


class ClientStopController:
    def __init__(self, send_cancel: Callable[[CancelRequest], None]) -> None:
        self._send_cancel = send_cancel
        self.stopped = False
        self.entries: dict[str, NewOrderRequest] = {}
        self.accepted: set[str] = set()
        self.pending_cancels: set[str] = set()
        self.cancel_sends: list[CancelRequest] = []
        self.reports: list[tuple[int, ExecutionReport]] = []

    def sent_entry(self, request: NewOrderRequest) -> None:
        self.entries[request.client_order_id] = request

    def stop(self, timestamp_ns: int) -> None:
        self.stopped = True
        for cid in tuple(self.entries):
            self._cancel(cid, timestamp_ns)

    def delivered(self, report: ExecutionReport, timestamp_ns: int) -> None:
        self.reports.append((timestamp_ns, report))
        cid = report.client_order_id
        if report.report_type is ReportType.CANCEL_REJECTED:
            self.pending_cancels.discard(cid)
            if report.reason == "unknown_order":
                # Entry may still be travelling on the independent channel.
                return
        if report.order_status.terminal:
            self.entries.pop(cid, None)
            self.accepted.discard(cid)
            self.pending_cancels.discard(cid)
        elif report.report_type is ReportType.ACCEPTED:
            self.accepted.add(cid)
            if self.stopped:
                self._cancel(cid, timestamp_ns)

    def _cancel(self, cid: str, timestamp_ns: int) -> None:
        if cid in self.pending_cancels:
            return
        request = CancelRequest(timestamp_ns, timestamp_ns, client_order_id=cid)
        self.pending_cancels.add(cid)
        self.cancel_sends.append(request)
        self._send_cancel(request)
