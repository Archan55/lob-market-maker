"""Stable fingerprints for canonical event streams."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable

from lobmm.enums import EventType
from lobmm.events import MarketEvent

_EVENT_STRUCT = struct.Struct(">qqbbqq")
_EVENT_CODES = {
    EventType.ADD: 1,
    EventType.CANCEL: 2,
    EventType.TRADE: 3,
    EventType.RESET: 4,
    EventType.SNAPSHOT: 5,
}


def canonical_event_bytes(event: MarketEvent) -> bytes:
    """Encode one canonical event for stable, cross-process fingerprinting."""

    return _EVENT_STRUCT.pack(
        event.timestamp_ns,
        event.sequence_number,
        _EVENT_CODES[event.event_type],
        int(event.side) if event.side is not None else 0,
        event.price_ticks,
        event.quantity,
    )


def event_stream_sha256(events: Iterable[MarketEvent]) -> str:
    """Return a stable SHA-256 fingerprint of exact canonical event content."""

    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_event_bytes(event))
    return digest.hexdigest()
