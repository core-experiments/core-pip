"""Benchmarks for requirements parsing and offline dependency resolution.

The resolver runs entirely against a generated local wheelhouse with
``--no-index`` semantics, so the measurements stay deterministic and never
touch the network.
"""

from __future__ import annotations

from pathlib import Path
import os

from benchmark_support import reset_caches
from pytest_codspeed import BenchmarkFixture
from cpip.index.provider import CandidateProvider
from cpip.resolution.req_file import parse_requirements
from cpip.resolution.resolver import Resolver
from cpip.core.errors import ResolutionError


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


def test_uv_wrong_package_backtracking_families(
    benchmark: BenchmarkFixture, wrong_package_wheelhouses: dict[str, Path]
) -> None:
    """Exercise the uv issue corpus' large wrong-package candidate shapes."""
    def resolve_cases() -> int:
        total = 0
        for name, wheelhouse in wrong_package_wheelhouses.items():
            total += resolve(wheelhouse, [f"{name}-root"])
        return total

    assert benchmark(resolve_cases) == 20


def test_top88_requirements_stress(
    benchmark: BenchmarkFixture, stress_wheelhouse: Path
) -> None:
    requirements = [f"stress-{index}" for index in range(88)]

    def resolve_stress() -> int:
        return resolve(stress_wheelhouse, requirements)

    assert benchmark(resolve_stress) == 176


def test_resolver_metrics(benchmark: BenchmarkFixture, graph_wheelhouse: Path) -> None:
    os.environ["CPIP_RESOLVER_METRICS"] = "1"

    def resolve_with_metrics() -> int:
        reset_caches()
        resolver = Resolver(
            provider=CandidateProvider.from_options(
                find_links=[str(graph_wheelhouse)], no_index=True
            ),
            ignore_installed=True,
        )
        resolver.resolve(["application"])
        metrics = resolver.metrics_snapshot()
        return metrics["candidates_considered"] + metrics["propagations"]

    assert benchmark(resolve_with_metrics) > 10


def test_candidate_scan_scaling(
    benchmark: BenchmarkFixture, candidate_scan_wheelhouse: Path
) -> None:
    """Scan many releases while rejecting a Requires-Python-heavy tail."""

    def resolve_candidate_scan() -> int:
        return resolve(candidate_scan_wheelhouse, ["candidate-scan"])

    assert benchmark(resolve_candidate_scan) == 1


def test_resolvelib_backjump_pattern(
    benchmark: BenchmarkFixture, backjump_wheelhouse: Path
) -> None:
    def resolve_conflict() -> int:
        reset_caches()
        try:
            resolve(backjump_wheelhouse, [
                "python>=3.12",
                "lz4==4.3.3",
                "clickhouse-driver>=0.2.9",
            ])
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("backjump workload unexpectedly resolved")

    assert benchmark(resolve_conflict) > 0


def test_unsatisfiable_error_reporting(
    benchmark: BenchmarkFixture, unsatisfiable_wheelhouse: Path
) -> None:
    requirements = ["unsatisfiable-root"]

    def resolve_and_format_error() -> int:
        reset_caches()
        try:
            resolve(unsatisfiable_wheelhouse, requirements)
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("unsatisfiable workload unexpectedly resolved")

    assert benchmark(resolve_and_format_error) > 100


def test_constraints_and_pins(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    requirements = [f"middle-{index}>=2.0.0" for index in range(10)]
    constraints = [f"middle-{index}==2.2.0" for index in range(10)]

    def resolve_constrained() -> int:
        reset_caches()
        resolver = Resolver(
            provider=CandidateProvider.from_options(
                find_links=[str(graph_wheelhouse)], no_index=True
            ),
            ignore_installed=True,
            constraints=constraints,
        )
        return len(resolver.resolve(requirements).candidates)

    assert benchmark(resolve_constrained) > 10


def test_conflicting_direct_requirements(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    requirements = ["middle-0==2.1.0", "middle-0==2.2.0"]

    def resolve_conflicting_direct() -> int:
        reset_caches()
        try:
            resolve(graph_wheelhouse, requirements)
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("conflicting direct requirements unexpectedly resolved")

    assert benchmark(resolve_conflicting_direct) > 0
