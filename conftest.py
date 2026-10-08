from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--latency-study-output",
        help="Save latency-stress-v1 tapes, runs, event snapshots and audit here.",
    )
