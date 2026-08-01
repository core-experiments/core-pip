"""Opt-in live PyPI benchmarks.

Run with ``CPIP_RUN_LIVE_BENCHMARKS=1``.  These tests are intentionally not
part of the default benchmark run because network latency and PyPI state are
external inputs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cpip.core.packaging import parse_requirement
from cpip.index.provider import CandidateProvider
from cpip.network.http import NetworkSession
from pytest_codspeed import BenchmarkFixture

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("CPIP_RUN_LIVE_BENCHMARKS") != "1",
        reason="set CPIP_RUN_LIVE_BENCHMARKS=1 to enable live PyPI benchmarks",
    ),
]

INDEX_URL = "https://pypi.org/simple"
PACKAGES = ("numpy", "numba", "starlette", "fastapi", "boto3", "sphinx")


def collect_packages(session: NetworkSession) -> int:
    provider = CandidateProvider.from_options(
        index_url=INDEX_URL,
        session=session,
    )
    try:
        return sum(
            len(provider.collect_links(parse_requirement(package)))
            for package in PACKAGES
        )
    finally:
        provider.close()


def test_live_index_cold_cache(benchmark: BenchmarkFixture) -> None:
    def collect_cold() -> int:
        return collect_packages(NetworkSession(retries=0))

    assert benchmark(collect_cold) > 0


def test_live_index_warm_cache(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    session = NetworkSession(retries=0, cache=str(tmp_path / "http-cache"))
    collect_packages(session)

    def collect_warm() -> int:
        return collect_packages(session)

    assert benchmark(collect_warm) > 0


def test_live_index_failure_path(benchmark: BenchmarkFixture) -> None:
    session = NetworkSession(retries=0)

    def missing_project() -> int:
        provider = CandidateProvider.from_options(
            index_url=INDEX_URL,
            session=session,
        )
        try:
            return len(
                provider.collect_links(
                    parse_requirement("cpip-benchmark-does-not-exist"),
                ),
            )
        finally:
            provider.close()

    assert benchmark(missing_project) == 0


def test_live_artifact_head(benchmark: BenchmarkFixture) -> None:
    session = NetworkSession(retries=0)
    provider = CandidateProvider.from_options(index_url=INDEX_URL, session=session)
    links = provider.collect_links(parse_requirement("requests"))
    provider.close()
    url = next(link.url for link in links if link.url.endswith(".whl"))

    def request_artifact() -> int:
        try:
            response = session.head(url)
        except Exception as error:  # network outcome is the measured path
            return len(type(error).__name__)
        try:
            return response.status_code
        finally:
            response.close()

    assert benchmark(request_artifact) > 0
