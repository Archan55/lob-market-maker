"""Finite offline query protocol; immutable evidence, never a trading action.

Queries inspect a covered journal at venue arrival, not at response delivery.
Absence is deliberately not a closed-world assertion about an order's history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from lobmm.enums import OrderStatus
from lobmm.orders import ExecutionReport, Fill


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    query_timestamps_ns: tuple[int, ...]
    request_latency_ns: int = 0
    response_latency_ns: int = 0
    snapshot_lag_ns: int = 0
    fill_limit_per_order: int | None = None
    include_order_state: bool = True

    def __post_init__(self) -> None:
        times = self.query_timestamps_ns
        if not isinstance(times, tuple) or not 1 <= len(times) <= 16:
            raise ValueError(
                "reconciliation requires an immutable 1..16 query schedule"
            )
        if any(type(t) is not int or t < 0 for t in times):
            raise ValueError("query times must be nonnegative integers")
        if tuple(sorted(set(times))) != times:
            raise ValueError("query times must be strictly increasing")
        for delay in (
            self.request_latency_ns,
            self.response_latency_ns,
            self.snapshot_lag_ns,
        ):
            if type(delay) is not int or delay < 0:
                raise ValueError("query delays must be nonnegative integers")
        if self.fill_limit_per_order is not None and (
            type(self.fill_limit_per_order) is not int or self.fill_limit_per_order < 0
        ):
            raise ValueError("fill limit must be a nonnegative integer or None")
        if type(self.include_order_state) is not bool:
            raise ValueError("include_order_state must be boolean")

    def artifact(self) -> dict[str, Any]:
        return dict(version="reconciliation-v1", **asdict(self))


@dataclass(frozen=True, slots=True)
class QueryTimer:
    sequence: int


@dataclass(frozen=True, slots=True)
class ReconciliationRequest:
    query_id: str
    request_timestamp_ns: int
    client_order_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrderEvidence:
    client_order_id: str
    state_timestamp_ns: int | None
    status: OrderStatus | None
    remaining_quantity: int | None
    fills: tuple[Fill, ...]
    execution_detail_complete_as_of: bool


@dataclass(frozen=True, slots=True)
class ReconciliationResponse:
    query_id: str
    request_timestamp_ns: int
    exchange_snapshot_timestamp_ns: int
    as_of_timestamp_ns: int
    tape_end_timestamp_ns: int
    snapshot_within_tape: bool
    orders: tuple[OrderEvidence, ...]


class ReconciliationJournal:
    """Venue-side journal only. The client receives a frozen, filtered response."""

    def __init__(self, plan: ReconciliationPlan) -> None:
        self.plan = plan
        self.reports: list[ExecutionReport] = []

    def record(self, reports: tuple[ExecutionReport, ...]) -> None:
        self.reports.extend(reports)

    def snapshot(
        self, request: ReconciliationRequest, arrival_ns: int, tape_end_ns: int
    ) -> ReconciliationResponse:
        as_of = min(max(0, arrival_ns - self.plan.snapshot_lag_ns), tape_end_ns)
        evidence = []
        for cid in request.client_order_ids:
            reports = [
                r
                for r in self.reports
                if r.client_order_id == cid
                and r.exchange_timestamp_ns <= as_of
                and r.reason != "unknown_order"
            ]
            state = reports[-1] if reports and self.plan.include_order_state else None
            fills = tuple(r.fill for r in reports if r.fill is not None)
            selected = fills[: self.plan.fill_limit_per_order]
            evidence.append(
                OrderEvidence(
                    cid,
                    state.exchange_timestamp_ns if state else None,
                    state.order_status if state else None,
                    state.remaining_quantity if state else None,
                    selected,
                    bool(reports) and len(selected) == len(fills),
                )
            )
        return ReconciliationResponse(
            request.query_id,
            request.request_timestamp_ns,
            arrival_ns,
            as_of,
            tape_end_ns,
            arrival_ns <= tape_end_ns,
            tuple(evidence),
        )
