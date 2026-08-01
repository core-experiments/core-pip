"""CodSpeed coverage for the remaining benchmark workloads."""

from __future__ import annotations

import hashlib
import itertools
import os
from pathlib import Path

import pytest
from benchmark_support import make_wheel, reset_caches
from pytest_codspeed import BenchmarkFixture
from cpip.cli.commands.fast_lock import render_lock as render_fast_lock
from cpip.cli.commands.lock import render_lock
from cpip.core.packaging import SpecifierSet, parse_requirement
from cpip.core.wheel import read_wheel_metadata
from cpip.index.directory_index import DirectoryIndex
from cpip.install.unpacking import unzip_file
from cpip.index.provider import CandidateProvider
from cpip.resolution.fast_wheelhouse.cache import candidate_cache
from cpip.resolution.fast_wheelhouse.metadata import load_candidate, wheel_name
from cpip.resolution.req_file import parse_requirements
from cpip.resolution.resolver import Resolver


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
    wheel = make_wheel(wheelhouse, "large-payload", "1.0.0", payload_files=10_000)
    counter = itertools.count()

    def install() -> None:
        destination = tmp_path / f"target-{next(counter)}"
        unzip_file(str(wheel), str(destination), flatten=False)

    benchmark(install)


@pytest.mark.parametrize("file_count", [100, 1_000, 10_000])
def test_directory_index_scaling(
    benchmark: BenchmarkFixture, tmp_path: Path, file_count: int
) -> None:
    for index in range(file_count):
        (tmp_path / f"index_bench_{index:05}-1.0-py3-none-any.whl").touch()
    (tmp_path / "index-bench-archive-1.0.tar.gz").touch()
    (tmp_path / "simple.html").write_text("<html></html>", encoding="utf-8")

    def scan() -> int:
        index = DirectoryIndex(str(tmp_path))
        index.scan()
        return sum(len(urls) for urls in index.project_name_to_urls.values())

    assert benchmark(scan) == file_count + 1


@pytest.mark.parametrize("package_count", [10, 100, 1_000, 10_000])
def test_lockfile_serialization(
    benchmark: BenchmarkFixture, package_count: int
) -> None:
    packages = []
    fast_packages = []
    for index in range(package_count):
        name = f"lock-bench-{index:05}"
        digest = f"{index:064x}"[-64:]
        wheel = f"{name}-1.0-py3-none-any.whl"
        url = f"https://files.example.test/{wheel}"
        packages.append(
            {
                "name": name,
                "version": "1.0",
                "wheels": [{"name": wheel, "url": url, "hashes": {"sha256": digest}}],
            }
        )
        fast_packages.append((name, "1.0", wheel, url, digest))

    def render() -> int:
        return len(render_lock(packages)) + len(render_fast_lock(fast_packages))

    assert benchmark(render) > package_count


@pytest.mark.parametrize("cache_state", ["cold", "warm", "invalidate"])
def test_metadata_cache_scaling(
    benchmark: BenchmarkFixture, tmp_path: Path, cache_state: str
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    entries = [
        make_wheel(wheelhouse, f"metadata-bench-{index:04}", "1.0.0")
        for index in range(1_000)
    ]
    metadata_cache: dict[str, object] = {}

    def load_all() -> int:
        return sum(
            len(
                load_candidate(
                    str(path), metadata_cache, wheel_name(str(path))
                ).dependencies
            )
            for path in entries
        )

    if cache_state in {"warm", "invalidate"}:
        load_all()
    invalidated = entries[len(entries) // 2]

    def read_metadata() -> int:
        candidate_cache.clear()
        if cache_state == "cold":
            metadata_cache.clear()
        elif cache_state == "invalidate":
            stat = invalidated.stat()
            os.utime(invalidated, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
        return load_all()

    assert benchmark(read_metadata) == 0


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


@pytest.mark.parametrize("unsatisfiable", [False, True])
def test_real_world_resolution_shape(
    benchmark: BenchmarkFixture, tmp_path: Path, unsatisfiable: bool
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for index in range(1, 129):
        version = f"{index}.0.0"
        make_wheel(wheelhouse, "shared", version)
        required_version = "999" if unsatisfiable else str(index)
        make_wheel(
            wheelhouse,
            "application",
            version,
            requires=[f"shared == {required_version}.0.0"],
        )
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)], no_index=True
    )

    def resolve() -> int:
        reset_caches()
        try:
            return len(
                Resolver(provider=provider, ignore_installed=True)
                .resolve(["application"])
                .candidates
            )
        except Exception:
            return 0

    assert benchmark(resolve) == (0 if unsatisfiable else 2)


def test_requirements_file_scaling(
    benchmark: BenchmarkFixture, requirements_file: Path
) -> None:
    def parse_file() -> int:
        reset_caches()
        return len(parse_requirements(str(requirements_file), session=None))

    assert benchmark(parse_file) > 0
