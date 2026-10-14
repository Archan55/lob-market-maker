from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--durable-study-output",
        help="Save durable recovery inboxes, prefix witnesses and real process-death evidence here.",
    )
    parser.addoption(
        "--reordering-study-output",
        help="Save reordering-stress-v1 replay inputs, trajectories and audit here.",
    )
    parser.addoption(
        "--reconciliation-study-output",
        help="Save offline reconciliation inputs, frozen responses and audit here.",
    )
    parser.addoption(
        "--latency-study-output",
        help="Save latency-stress-v1 tapes, runs, event snapshots and audit here.",
    )
    parser.addoption(
        "--exposure-study-output",
        help="Save exposure-session-stress-v1 inputs, replay tables and audit here.",
    )
    parser.addoption(
        "--client-stop-study-output",
        help="Save client-stop-v1 tapes, runs, snapshots and independent audit here.",
    )
    parser.addoption(
        "--transport-study-output",
        help="Save offline transport fault tapes, plans, snapshots and audit here.",
    )
