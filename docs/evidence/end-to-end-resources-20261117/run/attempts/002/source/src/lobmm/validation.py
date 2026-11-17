"""Ordered-stream validation with optional full book reconstruction."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import polars as pl

from lobmm.book import BookError, L2Book
from lobmm.data.fingerprint import canonical_event_bytes
from lobmm.data.loaders import load_events
from lobmm.data.schema import DataSchemaError, frame_to_events
from lobmm.enums import ValidationMode
from lobmm.events import BookView, MarketEvent


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One precisely located stream problem or lenient repair."""

    event_index: int | None
    code: str
    message: str


class StreamValidationError(ValueError):
    """Raised when strict stream validation encounters its first issue."""

    def __init__(self, issue: ValidationIssue) -> None:
        self.issue = issue
        location = (
            "stream"
            if issue.event_index is None
            else f"event index {issue.event_index}"
        )
        super().__init__(f"{location} [{issue.code}]: {issue.message}")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Audit-friendly result of validating a complete event stream."""

    event_count: int
    issues: tuple[ValidationIssue, ...]
    diagnostics: Mapping[str, int]
    final_book: BookView | None
    event_stream_sha256: str
    mode: ValidationMode
    reconstructed_book: bool
    required_nonempty: bool

    @property
    def valid(self) -> bool:
        """Whether no issues or repairs were observed."""

        return not self.issues

    @property
    def issue_count(self) -> int:
        return len(self.issues)


def _record_or_raise(
    issue: ValidationIssue,
    *,
    mode: ValidationMode,
    issues: list[ValidationIssue],
    counters: Counter[str],
) -> None:
    counters[issue.code] += 1
    if mode is ValidationMode.STRICT:
        raise StreamValidationError(issue)
    issues.append(issue)


def validate_event_stream(
    events: Iterable[MarketEvent],
    *,
    mode: ValidationMode = ValidationMode.STRICT,
    reconstruct_book: bool = True,
    require_nonempty: bool = True,
) -> ValidationResult:
    """Validate ordering and, by default, replay every event through an L2 book.

    Timestamps must be nondecreasing and sequence numbers must be strictly
    increasing. The sequence rule provides a deterministic total order for
    events sharing one timestamp. In lenient mode, ordering violations are
    reported but the supplied order is retained; malformed depth reductions
    are safely clamped by :class:`~lobmm.book.L2Book`.
    """

    if not isinstance(mode, ValidationMode):
        raise TypeError("mode must be a ValidationMode")

    book = L2Book(mode) if reconstruct_book else None
    issues: list[ValidationIssue] = []
    counters: Counter[str] = Counter()
    digest = hashlib.sha256()
    previous_timestamp: int | None = None
    previous_sequence: int | None = None
    last_event: MarketEvent | None = None
    event_count = 0

    for index, event in enumerate(events):
        digest.update(canonical_event_bytes(event))
        if previous_timestamp is not None and event.timestamp_ns < previous_timestamp:
            _record_or_raise(
                ValidationIssue(
                    index,
                    "timestamp_regression",
                    f"{event.timestamp_ns} follows {previous_timestamp}",
                ),
                mode=mode,
                issues=issues,
                counters=counters,
            )
        if previous_sequence is not None and event.sequence_number <= previous_sequence:
            _record_or_raise(
                ValidationIssue(
                    index,
                    "sequence_not_strictly_increasing",
                    f"{event.sequence_number} follows {previous_sequence}",
                ),
                mode=mode,
                issues=issues,
                counters=counters,
            )

        if book is not None:
            try:
                delta = book.apply(event)
                if delta.clamped:
                    _record_or_raise(
                        ValidationIssue(
                            index,
                            "book_event_clamped",
                            (
                                f"{event.event_type.value} requested "
                                f"{event.quantity}, applied {delta.quantity_applied} "
                                f"at {event.side!s} {event.price_ticks}"
                            ),
                        ),
                        mode=mode,
                        issues=issues,
                        counters=counters,
                    )
                book.assert_valid()
            except BookError as exc:
                _record_or_raise(
                    ValidationIssue(index, "invalid_book_transition", str(exc)),
                    mode=mode,
                    issues=issues,
                    counters=counters,
                )

        previous_timestamp = event.timestamp_ns
        previous_sequence = event.sequence_number
        last_event = event
        event_count += 1

    if event_count == 0 and require_nonempty:
        _record_or_raise(
            ValidationIssue(None, "empty_stream", "market event stream is empty"),
            mode=mode,
            issues=issues,
            counters=counters,
        )

    final_book = (
        book.view(
            timestamp_ns=last_event.timestamp_ns,
            sequence_number=last_event.sequence_number,
        )
        if book is not None and last_event is not None
        else None
    )
    if book is not None:
        counters.update(book.diagnostics)
    return ValidationResult(
        event_count=event_count,
        issues=tuple(issues),
        diagnostics=MappingProxyType(dict(counters)),
        final_book=final_book,
        event_stream_sha256=digest.hexdigest(),
        mode=mode,
        reconstructed_book=reconstruct_book,
        required_nonempty=require_nonempty,
    )


def validate_frame(
    frame: pl.DataFrame,
    *,
    mode: ValidationMode = ValidationMode.STRICT,
    reconstruct_book: bool = True,
    require_nonempty: bool = True,
    allow_extra_columns: bool = False,
) -> ValidationResult:
    """Decode and validate one in-memory canonical frame."""

    try:
        events = frame_to_events(frame, allow_extra_columns=allow_extra_columns)
    except DataSchemaError as exc:
        raise StreamValidationError(
            ValidationIssue(None, "invalid_schema", str(exc))
        ) from exc
    return validate_event_stream(
        events,
        mode=mode,
        reconstruct_book=reconstruct_book,
        require_nonempty=require_nonempty,
    )


def validate_file(
    path: str | Path,
    *,
    mode: ValidationMode = ValidationMode.STRICT,
    reconstruct_book: bool = True,
) -> ValidationResult:
    """Load a canonical CSV/Parquet file and validate its full event stream."""

    return validate_event_stream(
        load_events(path),
        mode=mode,
        reconstruct_book=reconstruct_book,
    )


# Concise compatibility spelling for callers and the CLI.
validate_events = validate_event_stream
