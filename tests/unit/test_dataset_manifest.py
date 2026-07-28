from __future__ import annotations

import json

import pytest

from lobmm.cli import _configured_events
from lobmm.config import AppConfig, DatasetProvenanceConfig, DatasetSourceType
from lobmm.data.manifest import sha256_file, write_dataset_manifest


def _historical_provenance(checksum: str) -> DatasetProvenanceConfig:
    return DatasetProvenanceConfig(
        source_type=DatasetSourceType.HISTORICAL,
        provider="Fixture Provider",
        venue="TEST",
        symbol="ABC",
        dataset_id="fixture-session",
        session_start="2024-01-02",
        session_end="2024-01-02",
        checksum_sha256=checksum,
        license_notes="Repository-owned fictional fixture.",
    )


def test_manifest_hashes_canonical_and_source_files(tmp_path) -> None:
    canonical = tmp_path / "canonical.parquet"
    source = tmp_path / "source.csv"
    canonical.write_bytes(b"canonical-fixture")
    source.write_text("source-fixture\n", encoding="utf-8")
    checksum = sha256_file(canonical, chunk_size=3)

    result = write_dataset_manifest(
        canonical,
        provenance=_historical_provenance(checksum),
        adapter_name="fixture",
        adapter_parameters={"tick_size": "0.01"},
        source_paths=(source,),
        event_count=3,
        adapter_diagnostics={"rows": 3},
        validation_diagnostics={},
    )

    document = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.canonical_sha256 == checksum
    assert document["canonical_output"]["sha256"] == checksum
    assert document["ingestion"]["source_files"][0]["filename"] == "source.csv"
    assert document["provenance"]["source_type"] == "historical"


def test_manifest_rejects_checksum_mismatch(tmp_path) -> None:
    canonical = tmp_path / "canonical.parquet"
    source = tmp_path / "source.csv"
    canonical.write_bytes(b"canonical-fixture")
    source.write_bytes(b"source-fixture")

    with pytest.raises(ValueError, match="must match"):
        write_dataset_manifest(
            canonical,
            provenance=_historical_provenance("0" * 64),
            adapter_name="fixture",
            adapter_parameters={},
            source_paths=(source,),
            event_count=1,
            adapter_diagnostics={},
            validation_diagnostics={},
        )


def test_configured_input_is_verified_before_decoding(tmp_path) -> None:
    dataset = tmp_path / "tampered.parquet"
    dataset.write_bytes(b"not-the-declared-data")
    config = AppConfig.model_validate(
        {
            "instrument": {"symbol": "ABC"},
            "data": {
                "input_path": str(dataset),
                "provenance": {
                    "source_type": "historical",
                    "provider": "Fixture Provider",
                    "venue": "TEST",
                    "symbol": "ABC",
                    "dataset_id": "fixture",
                    "session_start": "2024-01-02",
                    "session_end": "2024-01-02",
                    "checksum_sha256": "0" * 64,
                    "license_notes": "Repository-owned fictional fixture.",
                },
            },
        }
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        _configured_events(config)
