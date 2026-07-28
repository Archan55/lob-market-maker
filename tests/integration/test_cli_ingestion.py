from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from lobmm.cli import app
from lobmm.data import sha256_file

FIXTURES = Path(__file__).parents[1] / "fixtures" / "adapters"


def test_tardis_cli_writes_canonical_data_and_provenance_manifest(tmp_path) -> None:
    output = tmp_path / "tardis.parquet"
    result = CliRunner().invoke(
        app,
        [
            "ingest-tardis",
            "--book",
            str(FIXTURES / "tardis_incremental_book_L2.csv"),
            "--trades",
            str(FIXTURES / "tardis_trades.csv"),
            "--output",
            str(output),
            "--tick-size",
            "0.01",
            "--venue",
            "fixture",
            "--symbol",
            "ABC-USD",
            "--dataset-id",
            "tardis-fixture",
            "--session-start",
            "2024-01-02",
            "--license-notes",
            "Repository-owned fictional fixture.",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = output.with_suffix(".manifest.json")
    assert output.is_file()
    assert manifest.is_file()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["canonical_output"]["sha256"] == sha256_file(output)
    assert document["provenance"]["symbol"] == "ABC-USD"
    assert document["ingestion"]["adapter"] == "tardis_incremental_book_l2"


def test_lobster_cli_writes_canonical_data_and_provenance_manifest(tmp_path) -> None:
    output = tmp_path / "lobster.parquet"
    result = CliRunner().invoke(
        app,
        [
            "ingest-lobster",
            "--messages",
            str(FIXTURES / "lobster_message.csv"),
            "--orderbook",
            str(FIXTURES / "lobster_orderbook.csv"),
            "--output",
            str(output),
            "--tick-size",
            "0.01",
            "--symbol",
            "ABC",
            "--dataset-id",
            "lobster-fixture",
            "--trading-date",
            "2024-01-02",
            "--levels",
            "2",
            "--timezone",
            "UTC",
            "--license-notes",
            "Repository-owned fictional fixture.",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = output.with_suffix(".manifest.json")
    assert output.is_file()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["canonical_output"]["event_count"] > 0
    assert document["provenance"]["venue"] == "NASDAQ"
    assert document["ingestion"]["adapter"] == "lobster_message_orderbook"
