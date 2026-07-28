"""Cryptographic provenance sidecars for converted historical datasets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lobmm.config import DatasetProvenanceConfig


@dataclass(frozen=True, slots=True)
class SourceFileRecord:
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DatasetManifestResult:
    manifest_path: Path
    canonical_sha256: str
    source_files: tuple[SourceFileRecord, ...]


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash one file without loading it into memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_dataset_manifest(
    canonical_path: str | Path,
    *,
    provenance: DatasetProvenanceConfig,
    adapter_name: str,
    adapter_parameters: Mapping[str, Any],
    source_paths: Sequence[str | Path],
    event_count: int,
    adapter_diagnostics: Mapping[str, int],
    validation_diagnostics: Mapping[str, int],
    manifest_path: str | Path | None = None,
) -> DatasetManifestResult:
    """Write an auditable sidecar after a successful canonical conversion."""

    output = Path(canonical_path)
    if not output.is_file():
        raise FileNotFoundError(f"canonical dataset does not exist: {output}")
    if provenance.synthetic_demonstration:
        raise ValueError(
            "historical ingestion manifest cannot use synthetic provenance"
        )
    if not adapter_name.strip():
        raise ValueError("adapter_name cannot be blank")
    if event_count <= 0:
        raise ValueError("event_count must be positive")
    if not source_paths:
        raise ValueError("at least one source file is required")

    source_records = tuple(_source_record(Path(path)) for path in source_paths)
    canonical_sha256 = sha256_file(output)
    if provenance.checksum_sha256 != canonical_sha256:
        raise ValueError(
            "provenance checksum_sha256 must match the canonical output file"
        )
    destination = (
        Path(manifest_path)
        if manifest_path is not None
        else output.with_suffix(".manifest.json")
    )
    document = {
        "schema_version": 1,
        "provenance": provenance.artifact(),
        "canonical_output": {
            "filename": output.name,
            "sha256": canonical_sha256,
            "size_bytes": output.stat().st_size,
            "event_count": event_count,
        },
        "ingestion": {
            "adapter": adapter_name,
            "parameters": dict(adapter_parameters),
            "source_files": [
                {
                    "filename": record.filename,
                    "sha256": record.sha256,
                    "size_bytes": record.size_bytes,
                }
                for record in source_records
            ],
            "adapter_diagnostics": dict(sorted(adapter_diagnostics.items())),
            "validation_diagnostics": dict(sorted(validation_diagnostics.items())),
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return DatasetManifestResult(
        manifest_path=destination,
        canonical_sha256=canonical_sha256,
        source_files=source_records,
    )


def _source_record(path: Path) -> SourceFileRecord:
    if not path.is_file():
        raise FileNotFoundError(f"source data file does not exist: {path}")
    return SourceFileRecord(
        filename=path.name,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
    )
