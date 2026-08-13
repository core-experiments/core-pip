"""Real-world-scale reproductions of documented catastrophic resolver cases.

``test_benchmark_resolution.py::test_uv_wrong_package_backtracking_families``
already models these five incidents (uv issue
https://github.com/astral-sh/uv/issues/8157: wrong-package backtracking
where the resolver locks a recent release of one package, then rejects
hundreds of releases of a related package before finding the one whose
bound actually matches) at a fixed, fast-CI-friendly 64 versions per
package.

These benchmarks model the same five families at the scale that made the
real incidents notable in the first place -- ``catastrophic_wheelhouses``
in conftest.py sizes each one from its packages' live PyPI release counts
(recorded 2026-08-12): boto3 has 2093 releases, botocore 2491; the
"58 minutes of Poetry resolving" and "pip stuck for 5+ minutes" reports
tied to this exact urllib3/boto3/botocore pattern
(https://github.com/aio-libs/aiobotocore/issues/840,
https://github.com/python-poetry/poetry/pull/7950) are from this
population, not a toy one.

Each case still resolves to the same 4 candidates as the fast-CI version;
only the amount of backtracking work needed to get there changes.
"""

from __future__ import annotations

from pathlib import Path

from benchmark_support import reset_caches
from cpip.index.provider import CandidateProvider
from cpip.resolution.api import ResolutionEngine
from pytest_codspeed import BenchmarkFixture


def resolve(wheelhouse: Path, requirements: list[str]) -> int:
    reset_caches()
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    return len(resolver.resolve(requirements).candidates)


def test_catastrophic_boto3_urllib3(
    benchmark: BenchmarkFixture,
    catastrophic_wheelhouses: dict[str, Path],
) -> None:
    """boto3 (2093 releases) / botocore (2491) vs. urllib3's upper bound."""
    wheelhouse = catastrophic_wheelhouses["boto3-urllib3"]

    def resolve_case() -> int:
        return resolve(wheelhouse, ["boto3-urllib3-root"])

    assert benchmark(resolve_case) == 4


def test_catastrophic_numpy_numba(
    benchmark: BenchmarkFixture,
    catastrophic_wheelhouses: dict[str, Path],
) -> None:
    """numpy (150) backtracking numba (136) into ancient llvmlite (73)."""
    wheelhouse = catastrophic_wheelhouses["numpy-numba"]

    def resolve_case() -> int:
        return resolve(wheelhouse, ["numpy-numba-root"])

    assert benchmark(resolve_case) == 4


def test_catastrophic_sentry_rapidjson(
    benchmark: BenchmarkFixture,
    catastrophic_wheelhouses: dict[str, Path],
) -> None:
    """python-rapidjson (66) vs. sentry-kafka-schemas' pin (246 releases)."""
    wheelhouse = catastrophic_wheelhouses["sentry-rapidjson"]

    def resolve_case() -> int:
        return resolve(wheelhouse, ["sentry-rapidjson-root"])

    assert benchmark(resolve_case) == 4


def test_catastrophic_starlette_fastapi(
    benchmark: BenchmarkFixture,
    catastrophic_wheelhouses: dict[str, Path],
) -> None:
    """starlette (202) backtracked past by fastapi's bound (317 releases)."""
    wheelhouse = catastrophic_wheelhouses["starlette-fastapi"]

    def resolve_case() -> int:
        return resolve(wheelhouse, ["starlette-fastapi-root"])

    assert benchmark(resolve_case) == 4


def test_catastrophic_apache_beam_dill(
    benchmark: BenchmarkFixture,
    catastrophic_wheelhouses: dict[str, Path],
) -> None:
    """apache-beam (108 releases) vs. dill's narrow pin (30 releases)."""
    wheelhouse = catastrophic_wheelhouses["apache-beam-dill"]

    def resolve_case() -> int:
        return resolve(wheelhouse, ["apache-beam-dill-root"])

    assert benchmark(resolve_case) == 4
