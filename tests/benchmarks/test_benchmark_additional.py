"""CodSpeed coverage for workloads added by the original ASV suite."""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

from benchmark_support import make_wheel, reset_caches
from pytest_codspeed import BenchmarkFixture
from pip.core.packaging import SpecifierSet, parse_requirement
from pip.core.wheel import read_wheel_metadata
from pip.install.unpacking import unzip_file
from pip.index.provider import CandidateProvider
from pip.resolution.req_file import parse_requirements
from pip.resolution.resolver import Resolver


def test_hash_throughput(benchmark: BenchmarkFixture) -> None:
    payloads = tuple(b"x" * size for size in (1 << 10, 1 << 20, 16 << 20))

    def hash_payloads() -> int:
        return sum(len(hashlib.sha256(payload).digest()) for payload in payloads)

    assert benchmark(hash_payloads) == 96


def test_pep440_specifier_parsing(benchmark: BenchmarkFixture) -> None:
    values = (
        ">=3.8",
        ">=3.8,<4",
        ">=2.5,!=3.0.*,!=3.1.*,!=3.2.*,<4",
        "~=2.1",
        "!=2.0rc1,>=2.0",
    )

    def parse_all() -> int:
        return sum(len(SpecifierSet(value).specifiers) for value in values * 100)

    assert benchmark(parse_all) > 0


def test_requirement_primitive_parsing(benchmark: BenchmarkFixture) -> None:
    values = (
        "demo>=1.2,<3",
        "Demo_Pkg[PDF,SSL]>=1.0; python_version >= '3.11'",
        "demo!=1.0.*,>=0.5,<3; sys_platform == 'darwin'",
        "demo @ https://example.invalid/packages/demo-1.2-py3-none-any.whl",
    )

    def parse_all() -> int:
        reset_caches()
        return sum(len(parse_requirement(value).name) for value in values * 100)

    assert benchmark(parse_all) > 0


def test_metadata_scaling(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    def read_metadata() -> object:
        reset_caches()
        return read_wheel_metadata(payload_wheel)

    assert benchmark(read_metadata) is not None


def test_large_archive_installation(
    benchmark: BenchmarkFixture, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "large-payload", "1.0.0", payload_files=1_000)
    counter = itertools.count()

    def install() -> None:
        destination = tmp_path / f"target-{next(counter)}"
        unzip_file(str(wheel), str(destination), flatten=False)

    benchmark(install)


def test_marker_heavy_resolution(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for index in range(64):
        requirements = ()
        if index:
            requirements = (
                f"marker-provider-{index - 1:03}>=1; python_version >= '3.9'",
            )
        make_wheel(
            wheelhouse,
            f"marker-provider-{index:03}",
            "1.0.0",
            requires=list(requirements),
        )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)], no_index=True
    )

    def resolve() -> int:
        reset_caches()
        return len(
            Resolver(provider=provider, ignore_installed=True)
            .resolve(["marker-provider-063"])
            .candidates
        )

    assert benchmark(resolve) == 64


def test_requirements_file_scaling(
    benchmark: BenchmarkFixture, requirements_file: Path
) -> None:
    def parse_file() -> int:
        reset_caches()
        return len(parse_requirements(str(requirements_file), session=None))

    assert benchmark(parse_file) > 0
