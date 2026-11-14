"""Canonical market-data schema, I/O, and adapter interfaces."""

from lobmm.data._adapter_utils import MarketDataAdapterError, TableSource
from lobmm.data.adapters import BaseL2DataAdapter, DataAdapter, L2DataAdapter
from lobmm.data.fingerprint import event_stream_sha256
from lobmm.data.loaders import (
    DataLoadError,
    load_csv,
    load_events,
    load_frame,
    load_parquet,
    read_csv,
    read_parquet,
    write_csv,
    write_parquet,
)
from lobmm.data.lobster import (
    LOBSTERCSVAdapter,
    LobsterCSVAdapter,
    LOBSTERData,
    LobsterData,
)
from lobmm.data.manifest import (
    DatasetManifestResult,
    SourceFileRecord,
    sha256_file,
    write_dataset_manifest,
)
from lobmm.data.schema import (
    CANONICAL_COLUMNS,
    CANONICAL_SCHEMA,
    DataSchemaError,
    coerce_canonical_frame,
    empty_event_frame,
    event_from_mapping,
    events_to_frame,
    frame_to_events,
)
from lobmm.data.tardis import TardisCSVAdapter, TardisData, TardisTimestampSource

__all__ = [
    "CANONICAL_COLUMNS",
    "CANONICAL_SCHEMA",
    "BaseL2DataAdapter",
    "DataAdapter",
    "DataLoadError",
    "DataSchemaError",
    "DatasetManifestResult",
    "L2DataAdapter",
    "LOBSTERCSVAdapter",
    "LOBSTERData",
    "LobsterCSVAdapter",
    "LobsterData",
    "MarketDataAdapterError",
    "SourceFileRecord",
    "TableSource",
    "TardisCSVAdapter",
    "TardisData",
    "TardisTimestampSource",
    "coerce_canonical_frame",
    "empty_event_frame",
    "event_from_mapping",
    "event_stream_sha256",
    "events_to_frame",
    "frame_to_events",
    "load_csv",
    "load_events",
    "load_frame",
    "load_parquet",
    "read_csv",
    "read_parquet",
    "sha256_file",
    "write_csv",
    "write_dataset_manifest",
    "write_parquet",
]
