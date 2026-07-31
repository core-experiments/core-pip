from __future__ import annotations

from benchmarks.lockfile_serialization import (
    LockfileSerialization,
    create_lock_workload,
)


def test_lockfile_serialization_benchmark_smoke() -> None:
    benchmark = LockfileSerialization()
    state = {"10": create_lock_workload(10)}
    benchmark.setup(state, "10")
    benchmark.time_render(state, "10")
    benchmark.time_render_fast(state, "10")

    assert len(benchmark.packages) == 10
