from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
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
