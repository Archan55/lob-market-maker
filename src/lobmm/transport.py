"""Opt-in, offline transport faults and report-only client uncertainty.

This is a constructed research protocol: one send has a stable channel ordinal;
duplicates carry the same identity and arrive together. The injector adds no
retries, timeouts, networking or recovery queries; the existing stop controller
can retry an early unknown cancel on delivered acceptance. The plan is simulator truth
and is never delivered to the strategy or client stop controller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from lobmm.orders import ExecutionReport, Fill, NewOrderRequest

ChannelName = Literal[
    "new_orders",
    "cancellations",
    "execution_reports",
    "reconciliation_requests",
    "reconciliation_responses",
]
FaultAction = Literal["drop", "duplicate"]


@dataclass(frozen=True, slots=True)
class TransportFault:
    channel: ChannelName
    sequence: int
    action: FaultAction

    def __post_init__(self) -> None:
        if self.channel not in {
            "new_orders",
            "cancellations",
            "execution_reports",
            "reconciliation_requests",
            "reconciliation_responses",
        }:
            raise ValueError("unsupported research fault channel")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("fault sequence must be a nonnegative integer")
        if self.action not in {"drop", "duplicate"}:
            raise ValueError("fault action must be drop or duplicate")


@dataclass(frozen=True, slots=True)
class TransportFaultPlan:
    faults: tuple[TransportFault, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.faults, tuple):
            raise ValueError("faults must be an immutable tuple")
        keys = [(fault.channel, fault.sequence) for fault in self.faults]
        if len(keys) != len(set(keys)):
            raise ValueError("one action per channel sequence is required")

    def artifact(self) -> dict[str, Any]:
        return dict(
            version="transport-faults-v1", faults=[asdict(f) for f in self.faults]
        )


class ResearchTransport:
    def __init__(self, plan: TransportFaultPlan) -> None:
        self.plan = plan
        self._actions: dict[tuple[str, int], str] = {
            (f.channel, f.sequence): f.action for f in plan.faults
        }
        self.sends: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []
        self._seen: set[tuple[str, int]] = set()

    def copies(
        self,
        channel: str,
        sequence: int,
        send_ns: int,
        arrival_ns: int,
        payload: object,
    ) -> int:
        action = self._actions.get((channel, sequence), "deliver")
        copies = {"drop": 0, "duplicate": 2, "deliver": 1}[action]
        self.sends.append(
            dict(
                channel=channel,
                sequence=sequence,
                action=action,
                copies=copies,
                send_timestamp_ns=send_ns,
                nominal_delivery_timestamp_ns=arrival_ns,
                client_order_id=getattr(payload, "client_order_id", None),
                report_id=getattr(payload, "report_id", None),
            )
        )
        return copies

    def receive(self, channel: str, sequence: int, timestamp_ns: int) -> bool:
        key = (channel, sequence)
        duplicate = key in self._seen
        self._seen.add(key)
        self.deliveries.append(
            dict(
                channel=channel,
                sequence=sequence,
                timestamp_ns=timestamp_ns,
                duplicate_ignored=duplicate,
            )
        )
        return not duplicate

    def artifact(self) -> dict[str, Any]:
        used = {(s["channel"], s["sequence"]) for s in self.sends}
        return dict(
            **self.plan.artifact(),
            sends=self.sends,
            deliveries=self.deliveries,
            unused_faults=[
                asdict(f)
                for f in self.plan.faults
                if (f.channel, f.sequence) not in used
            ],
        )


class ClientKnowledge:
    """Inventory bounds from sent quantities and delivered report evidence only.

    A terminal status resolves future execution but cannot replace lost fill
    details. Inventory inferred from a remaining-quantity snapshot is disclosed
    separately from inventory actually notified by fill reports.
    """

    def __init__(self) -> None:
        self.entries: dict[str, NewOrderRequest] = {}
        self.filled: dict[str, int] = {}
        self.remaining: dict[str, int] = {}
        self.terminal: set[str] = set()
        self._reports: set[str] = set()
        self._fills: set[str] = set()
        self.fill_details: dict[str, Fill] = {}

    def sent(self, request: NewOrderRequest) -> None:
        self.entries[request.client_order_id] = request
        self.filled[request.client_order_id] = 0
        self.remaining[request.client_order_id] = request.quantity

    def delivered(self, report: ExecutionReport) -> None:
        if report.report_id in self._reports:
            return
        self._reports.add(report.report_id)
        cid = report.client_order_id
        if cid not in self.entries or report.reason == "unknown_order":
            return
        if report.fill is not None:
            self.recovered_fill(report.fill)
        self.remaining[cid] = min(self.remaining[cid], report.remaining_quantity)
        if report.order_status.terminal:
            self.terminal.add(cid)

    def recovered_fill(self, fill: Fill) -> bool:
        if fill.fill_id in self._fills:
            return False
        cid = fill.client_order_id
        if cid not in self.entries:
            return False
        self._fills.add(fill.fill_id)
        self.fill_details[fill.fill_id] = fill
        self.filled[cid] += fill.quantity
        self.remaining[cid] = min(
            self.remaining[cid], self.entries[cid].quantity - self.filled[cid]
        )
        return True

    def reconciled_state(self, cid: str, remaining: int, *, terminal: bool) -> None:
        self.remaining[cid] = min(self.remaining[cid], remaining)
        if terminal:
            self.terminal.add(cid)

    def artifact(self) -> dict[str, Any]:
        known = minimum = maximum = 0
        orders: list[dict[str, Any]] = []
        for cid, request in self.entries.items():
            notified = self.filled[cid]
            lower = request.quantity - self.remaining[cid]
            upper = lower if cid in self.terminal else request.quantity
            side = int(request.side)
            known += side * notified
            minimum += side * (lower if side == 1 else upper)
            maximum += side * (upper if side == 1 else lower)
            orders.append(
                dict(
                    client_order_id=cid,
                    side=side,
                    sent_quantity=request.quantity,
                    notified_fill_quantity=notified,
                    executed_quantity_min=lower,
                    executed_quantity_max=upper,
                    terminal_report_received=cid in self.terminal,
                    reservation_quantity=0
                    if cid in self.terminal
                    else request.quantity - notified,
                    missing_fill_quantity_min=max(0, lower - notified),
                )
            )
        complete = all(
            o["terminal_report_received"] and o["missing_fill_quantity_min"] == 0
            for o in orders
        )
        return dict(
            known_inventory=known,
            inventory_min=minimum,
            inventory_max=maximum,
            complete=complete,
            orders=orders,
        )
