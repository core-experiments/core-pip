"""Benchmarks for requirements parsing and offline dependency resolution.

The resolver runs entirely against a generated local wheelhouse with
``--no-index`` semantics, so the measurements stay deterministic and never
touch the network.
"""

from __future__ import annotations

from pathlib import Path

from benchmark_support import reset_caches
from pytest_codspeed import BenchmarkFixture
from cpip.index.provider import CandidateProvider
from cpip.resolution.req_file import parse_requirements
from cpip.resolution.resolver import Resolver


def resolve(wheelhouse: Path, requirements: list[str]) -> int:
    reset_caches()
    resolver = Resolver(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)], no_index=True
        ),
        ignore_installed=True,
    )
    return len(resolver.resolve(requirements).candidates)


def test_parse_requirements_file(
    benchmark: BenchmarkFixture, requirements_file: Path
) -> None:
    def parse_file() -> int:
        reset_caches()
        return len(parse_requirements(str(requirements_file), session=None))

    assert benchmark(parse_file) > 0


def test_resolve_single_project(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    def resolve_leaf() -> int:
        return resolve(graph_wheelhouse, ["leaf-0"])

    assert benchmark(resolve_leaf) == 1


def test_resolve_dependency_graph(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    def resolve_application() -> int:
        return resolve(graph_wheelhouse, ["application"])

    assert benchmark(resolve_application) > 10


def test_resolve_pinned_dependency_graph(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    requirements = [f"middle-{index}==2.2.0" for index in range(10)]

    def resolve_pinned() -> int:
        return resolve(graph_wheelhouse, requirements)

    assert benchmark(resolve_pinned) > 10


def test_resolve_with_backtracking(
    benchmark: BenchmarkFixture, backtracking_wheelhouse: Path
) -> None:
    def resolve_conflicting() -> int:
        return resolve(backtracking_wheelhouse, ["conflicting"])

    assert benchmark(resolve_conflicting) > 0
