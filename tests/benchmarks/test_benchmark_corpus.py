"""Offline benchmarks over a checked-in PyPI metadata snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from cpip.core.packaging import parse_requirement
from cpip.index.page_parsing import IndexPageParser
from pytest_codspeed import BenchmarkFixture

from benchmark_support import reset_caches


SNAPSHOT = Path(__file__).with_name("corpus") / "pypi_snapshot.json"


def load_snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_frozen_pypi_metadata_resolution_shape(benchmark: BenchmarkFixture) -> None:
    snapshot = load_snapshot()
    projects = snapshot["projects"]
    scenarios = snapshot["scenarios"]

    def parse_metadata_graph() -> int:
        reset_caches()
        total = 0
        for project in projects:  # type: ignore[union-attr]
            total += len(parse_requirement(project["name"]).name)  # type: ignore[index]
            total += sum(
                len(parse_requirement(requirement).name)  # type: ignore[arg-type]
                for requirement in project["requires_dist"]  # type: ignore[index]
            )
        for requirements in scenarios.values():  # type: ignore[union-attr]
            total += sum(
                len(parse_requirement(requirement).name)  # type: ignore[arg-type]
                for requirement in requirements  # type: ignore[union-attr]
            )
        return total

    assert benchmark(parse_metadata_graph) > 500


def test_frozen_pypi_simple_api_candidates(benchmark: BenchmarkFixture) -> None:
    snapshot = load_snapshot()
    files = []
    for project in snapshot["projects"]:  # type: ignore[union-attr]
        filename = f"{project['name']}-{project['version']}-py3-none-any.whl"  # type: ignore[index]
        files.append(
            {
                "filename": filename,
                "url": f"https://files.pythonhosted.org/packages/{filename}",
                "requires-python": ">=3.9",
                "core-metadata": {"sha256": "b" * 64},
            }
        )
    body = json.dumps({"meta": {"api-version": "1.1"}, "files": files})
    parser = IndexPageParser()

    def parse_snapshot_index() -> int:
        reset_caches()
        return len(parser.links_from_json(body, "https://pypi.org/simple/snapshot/"))

    assert benchmark(parse_snapshot_index) == len(files)
