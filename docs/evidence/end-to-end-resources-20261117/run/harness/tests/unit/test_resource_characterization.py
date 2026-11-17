"""Pure monitor and retained-artifact checks; no application workers run here."""

from __future__ import annotations

import copy
import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def resource_tool() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "tools/characterize_end_to_end_resources.py"
    )
    spec = importlib.util.spec_from_file_location(
        "resource_characterization_unit", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _boundaries() -> list[dict[str, int | str]]:
    names = (
        "worker_entry",
        "imports_config_complete",
        "input_complete",
        "scheduler_enter",
        "scheduler_return",
        "outputs_enter",
        "outputs_return",
        "application_complete",
    )
    return [
        {"name": name, "monotonic_ns": 100 + 10 * index}
        for index, name in enumerate(names)
    ]


def test_proc_status_keeps_linux_raw_units_and_converts_kibibytes(
    resource_tool: ModuleType,
) -> None:
    result = resource_tool.parse_proc_status(
        "Name:\tpython\nState:\tR (running)\n"
        "VmHWM:\t  456 kB\nVmRSS:\t123 kB\nThreads:\t7\n"
    )
    assert result["proc_state"] == "R"
    assert result["unit"] == "kB"
    assert result["vmrss_raw"] == 123
    assert result["vmhwm_raw"] == 456
    assert result["vmrss_bytes"] == 125_952
    assert result["vmhwm_bytes"] == 466_944


def test_missing_proc_memory_is_unavailable_and_never_zero(
    resource_tool: ModuleType,
) -> None:
    result = resource_tool.parse_proc_status("Name:\tpython\nState:\tZ (zombie)\n")
    assert result["proc_state"] == "Z"
    assert result["vmrss_raw"] is None
    assert result["vmhwm_raw"] is None
    assert result["vmrss_bytes"] is None
    assert result["vmhwm_bytes"] is None


@pytest.mark.parametrize(
    "line",
    ("VmRSS: 12 MB", "VmRSS: -1 kB", "VmHWM: unknown kB"),
)
def test_proc_status_rejects_present_malformed_counters(
    resource_tool: ModuleType, line: str
) -> None:
    with pytest.raises(ValueError):
        resource_tool.parse_proc_status("State: R (running)\n" + line + "\n")


def test_sampler_assigns_boundaries_to_following_half_open_interval(
    resource_tool: ModuleType,
) -> None:
    boundaries = _boundaries()
    stages = (
        "bootstrap",
        "input",
        "engine_setup",
        "simulation",
        "postprocessing",
        "persistence",
        "reporting",
    )
    assert resource_tool.assign_stage(99, boundaries) == "startup"
    for index, stage in enumerate(stages):
        assert resource_tool.assign_stage(100 + 10 * index, boundaries) == stage
        assert resource_tool.assign_stage(109 + 10 * index, boundaries) == stage
    assert resource_tool.assign_stage(170, boundaries) == "after_application_complete"
    assert resource_tool.assign_stage(171, boundaries) == "after_application_complete"


@pytest.mark.parametrize("problem", ("missing", "reordered", "same_time"))
def test_stage_assignment_refuses_incomplete_or_noncausal_boundaries(
    resource_tool: ModuleType, problem: str
) -> None:
    boundaries = _boundaries()
    if problem == "missing":
        boundaries.pop()
    elif problem == "reordered":
        boundaries[2], boundaries[3] = boundaries[3], boundaries[2]
    else:
        boundaries[3]["monotonic_ns"] = boundaries[2]["monotonic_ns"]
    with pytest.raises(ValueError):
        resource_tool.assign_stage(145, boundaries)


@pytest.mark.parametrize(
    ("relative", "expected"),
    (
        ("input/events.parquet", "input_snapshot"),
        ("engine/resource-study/pnl.parquet", "engine_parquet"),
        ("engine/resource-study/run_config.yaml", "engine_json_yaml"),
        ("engine/resource-study/metrics.json", "engine_json_yaml"),
        ("engine/resource-study/drawdown.png", "plot_png"),
        ("engine/resource-study/report_summary.txt", "report_summary"),
        ("cache/fontlist-v390.json", "cache"),
        ("source/src/lobmm/portfolio.py", "bookkeeping"),
        ("samples.jsonl", "bookkeeping"),
    ),
)
def test_product_accounting_keeps_cache_and_bookkeeping_separate(
    resource_tool: ModuleType, relative: str, expected: str
) -> None:
    assert resource_tool.classify_file(relative) == expected


def test_diagnostics_normalization_removes_only_three_declared_root_fields(
    resource_tool: ModuleType,
) -> None:
    document = {
        "wall_clock_seconds": 1.0,
        "events_per_second": 2000.0,
        "peak_memory_bytes": 50,
        "end_inventory": 3,
        "event_stream_sha256": "unchanged",
        "nested": {"peak_memory_bytes": 90},
    }
    original = copy.deepcopy(document)
    normalized = resource_tool.normalize_document("diagnostics.json", document)
    assert normalized == {
        "end_inventory": 3,
        "event_stream_sha256": "unchanged",
        "nested": {"peak_memory_bytes": 90},
    }
    normalized["nested"]["peak_memory_bytes"] = 1
    assert document == original


def test_metrics_normalization_is_limited_to_engineering_and_deep_copies(
    resource_tool: ModuleType,
) -> None:
    document = {
        "wall_clock_seconds": "keep root field",
        "engineering": {
            "wall_clock_seconds": 0.5,
            "events_per_second": 4000.0,
            "peak_memory_bytes": 200,
            "events_processed": 2000,
            "other": {"events_per_second": "keep nested field"},
        },
        "pnl": {"net_pnl": -0.25, "peak_memory_bytes": "keep nonengineering field"},
    }
    original = copy.deepcopy(document)
    normalized = resource_tool.normalize_document("metrics.json", document)
    assert normalized == {
        "wall_clock_seconds": "keep root field",
        "engineering": {
            "events_processed": 2000,
            "other": {"events_per_second": "keep nested field"},
        },
        "pnl": {"net_pnl": -0.25, "peak_memory_bytes": "keep nonengineering field"},
    }
    normalized["engineering"]["other"]["events_per_second"] = "changed copy"
    assert document == original


@pytest.mark.parametrize("name", ("summary.json", "run_config.yaml"))
def test_behavior_documents_have_no_resource_field_exclusions(
    resource_tool: ModuleType, name: str
) -> None:
    document = {"peak_memory_bytes": "preserve", "nested": {"seed": 7}}
    result = resource_tool.normalize_document(name, document)
    assert result == document
    result["nested"]["seed"] = 8
    assert document["nested"]["seed"] == 7


def test_caps_are_strictly_greater_and_use_binary_byte_units(
    resource_tool: ModuleType,
) -> None:
    limits = (1_073_741_824, 536_870_912, 120_000_000_000, 600_000_000_000)
    assert resource_tool.check_caps(*limits) is None
    assert resource_tool.check_caps(0, 0, 0, 0) is None
    for index, expected in enumerate(
        ("worker_rss_cap", "output_logical_cap", "worker_time_cap", "total_time_cap")
    ):
        observed = list(limits)
        observed[index] += 1
        assert resource_tool.check_caps(*observed) == expected


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    *,
    color_type: int = 6,
    width: int = 2,
    height: int = 1,
    scanlines: bytes | None = None,
    compressed: bytes | None = None,
) -> bytes:
    channels = 4 if color_type == 6 else 3
    if scanlines is None:
        scanlines = b"\x00" + bytes(range(width * channels))
    if compressed is None:
        compressed = zlib.compress(scanlines)
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("color_type", (2, 6))
def test_png_audit_decodes_small_rgb_and_rgba_artifacts(
    resource_tool: ModuleType, tmp_path: Path, color_type: int
) -> None:
    path = tmp_path / "plot.png"
    path.write_bytes(_png(color_type=color_type))
    result = resource_tool.verify_png(path)
    assert result["valid"] is True
    assert result["width"] == 2
    assert result["height"] == 1


@pytest.mark.parametrize(
    "problem", ("signature", "crc", "zlib", "filter", "row_length", "dimensions")
)
def test_png_audit_rejects_corruption_beyond_filename_or_header_checks(
    resource_tool: ModuleType, tmp_path: Path, problem: str
) -> None:
    if problem == "signature":
        contents = b"not PNG" + _png()[8:]
    elif problem == "crc":
        contents = _png()[:-1] + bytes([_png()[-1] ^ 1])
    elif problem == "zlib":
        contents = _png(compressed=b"invalid zlib stream")
    elif problem == "filter":
        contents = _png(scanlines=b"\x05" + bytes(8))
    elif problem == "row_length":
        contents = _png(scanlines=b"\x00" + bytes(7))
    else:
        contents = _png(width=0, scanlines=b"\x00")
    path = tmp_path / "plot.png"
    path.write_bytes(contents)
    with pytest.raises(ValueError):
        resource_tool.verify_png(path)
