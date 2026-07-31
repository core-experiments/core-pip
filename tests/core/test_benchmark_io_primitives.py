from __future__ import annotations

from pathlib import Path

from benchmarks.io_primitives import (
    DirectoryIndexScaling,
    RequirementsFileParsing,
    create_index_workload,
    create_requirements_workload,
)


def test_directory_index_benchmark_smoke(tmp_path: Path) -> None:
    paths = create_index_workload(tmp_path)
    benchmark = DirectoryIndexScaling()
    state: dict[str, object] = {"paths": paths}
    benchmark.setup(state, "10", "mixed")
    benchmark.time_scan(state, "10", "mixed")


def test_requirements_file_benchmark_smoke(tmp_path: Path) -> None:
    paths = create_requirements_workload(tmp_path)
    benchmark = RequirementsFileParsing()
    state: dict[str, object] = {"paths": paths}
    benchmark.setup(state, "10", "nested")
    benchmark.time_parse(state, "10", "nested")
