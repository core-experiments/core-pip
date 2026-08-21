"""Benchmarks for requirement, version and marker handling.

These helpers run for every requirement pip reads, for every candidate it
considers and for every dependency edge it walks, so they dominate the CPU
profile of resolution.
"""

from __future__ import annotations

from benchmark_support import (
    requirement_lines,
    reset_caches,
    version_strings,
)
from cpip.core.packaging import (
    SpecifierSet,
    Version,
    canonicalize_name,
    canonicalize_requirement,
    marker_applies,
    parse_requirement,
)
from pytest_codspeed import BenchmarkFixture

REQUIREMENTS = requirement_lines()
VERSION_STRINGS = version_strings()
PARSED_VERSIONS = [Version(value) for value in VERSION_STRINGS]
PROJECT_NAMES = [f"Some_Project.Name-{index}" for index in range(len(VERSION_STRINGS))]
MARKERS = [
    'python_version >= "3.9"',
    'sys_platform == "linux" and platform_machine != "ppc64le"',
    'os_name == "posix" and python_full_version < "3.13.0"',
    'extra == "socks" or extra == "security"',
    '(python_version < "3.11" and sys_platform != "win32") or extra == "tests"',
]


def test_parse_requirements(benchmark: BenchmarkFixture) -> None:
    def parse_all() -> int:
        reset_caches()
        return sum(len(parse_requirement(line).name) for line in REQUIREMENTS)

    assert benchmark(parse_all) > 0


def test_canonicalize_requirements(benchmark: BenchmarkFixture) -> None:
    def canonicalize_all() -> int:
        reset_caches()
        return sum(len(canonicalize_requirement(line)) for line in REQUIREMENTS)

    assert benchmark(canonicalize_all) > 0


def test_canonicalize_names(benchmark: BenchmarkFixture) -> None:
    def canonicalize_all() -> int:
        reset_caches()
        return sum(len(canonicalize_name(name)) for name in PROJECT_NAMES)

    assert benchmark(canonicalize_all) > 0


def test_parse_versions(benchmark: BenchmarkFixture) -> None:
    def parse_all() -> int:
        reset_caches()
        return len([Version(value) for value in VERSION_STRINGS])

    assert benchmark(parse_all) == len(VERSION_STRINGS)


def test_sort_versions(benchmark: BenchmarkFixture) -> None:
    def sort_versions() -> Version:
        return sorted(PARSED_VERSIONS)[-1]

    assert isinstance(benchmark(sort_versions), Version)


def test_specifier_set_contains(benchmark: BenchmarkFixture) -> None:
    specifier = SpecifierSet(">=1.0,!=2.5.0,!=3.7.*,<9.0")

    def check_all() -> int:
        return sum(
            1
            for version in PARSED_VERSIONS
            if specifier.contains(version, allow_prereleases=True)
        )

    assert benchmark(check_all) > 0


def test_parse_specifier_sets(benchmark: BenchmarkFixture) -> None:
    raw = [
        ">=3.9",
        ">=3.9,<4",
        ">=2.7,!=3.0.*,!=3.1.*,!=3.2.*,<4",
        "~=1.4.2",
        "==1.2.3",
    ]

    def parse_all() -> int:
        reset_caches()
        return sum(len(SpecifierSet(value).specifiers) for value in raw * 60)

    assert benchmark(parse_all) > 0


def test_evaluate_markers(benchmark: BenchmarkFixture) -> None:
    def evaluate_all() -> int:
        matched = 0
        for _ in range(20):
            for marker in MARKERS:
                matched += marker_applies(marker, extras=("socks", "tests"))
        return matched

    assert benchmark(evaluate_all) > 0


def test_restore_versions_from_state(benchmark: BenchmarkFixture) -> None:
    """The cold path by which cached catalog summaries hand back Versions
    without reparsing: one record per version, restored in bulk."""
    states = [version.cache_state_internal() for version in PARSED_VERSIONS]

    def restore_all() -> int:
        reset_caches()
        return len([Version.from_cache_state(state) for state in states])

    assert benchmark(restore_all) == len(PARSED_VERSIONS)


def test_intern_versions_warm(benchmark: BenchmarkFixture) -> None:
    """Constructing a Version from text the process has already seen: the
    steady state of a resolve, where the same few hundred texts recur on
    every candidate."""

    def construct_all() -> int:
        return len([Version(value) for value in VERSION_STRINGS])

    reset_caches()
    construct_all()
    assert benchmark(construct_all) == len(VERSION_STRINGS)
