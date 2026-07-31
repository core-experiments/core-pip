from __future__ import annotations

from pathlib import Path

from benchmarks.metadata_cache import MetadataCacheScaling
from benchmarks.uv_scenarios import make_metadata_wheel


def test_metadata_cache_benchmark_smoke(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_metadata_wheel(wheelhouse, "demo", "1.0")
    state: dict[str, object] = {"wheelhouses": {"1": str(wheelhouse)}}

    benchmark = MetadataCacheScaling()
    benchmark.setup(state, "1", "invalidate")
    benchmark.time_metadata(state, "1", "invalidate")

    assert len(benchmark.cache) == 1
