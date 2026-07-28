"""Extension points for mapping external Level 2 data to the canonical schema."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

import polars as pl

from lobmm.data.schema import coerce_canonical_frame


@runtime_checkable
class L2DataAdapter[SourceT](Protocol):
    """Structural interface implemented by external dataset adapters."""

    @property
    def name(self) -> str:
        """Stable adapter name used in diagnostics and run metadata."""

    def to_canonical(self, source: SourceT) -> pl.DataFrame:
        """Map a source object into the canonical Level 2 schema."""


class BaseL2DataAdapter[SourceT](ABC):
    """Nominal base class for adapters that want shared schema checking."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable adapter name used in diagnostics and run metadata."""

    @abstractmethod
    def to_canonical(self, source: SourceT) -> pl.DataFrame:
        """Map a source object into the canonical Level 2 schema."""

    def checked(self, source: SourceT) -> pl.DataFrame:
        """Map ``source`` and verify the exact canonical column contract."""

        return coerce_canonical_frame(self.to_canonical(source))


# Shorter spelling for callers that do not care whether an adapter is nominal.
DataAdapter = L2DataAdapter
