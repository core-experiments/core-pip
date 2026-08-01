"""CodSpeed coverage for workloads added by the original ASV suite."""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

from benchmark_support import REAL_WORLD_CORPORA, make_wheel, reset_caches
from pytest_codspeed import BenchmarkFixture
from cpip.core.packaging import SpecifierSet, parse_requirement
from cpip.core.wheel import read_wheel_metadata
from cpip.install.unpacking import unzip_file
from cpip.install.target import InstallTarget
from cpip.install.uninstall import DistributionUninstaller
from cpip.install.wheel_transaction import WheelInstaller
from cpip.index.provider import CandidateProvider
from cpip.resolution.req_file import parse_requirements
from cpip.core.errors import BuildError
from cpip.index.candidates import prepare_project_metadata
from cpip.index.candidate_materialization import CandidateMaterializer
from cpip.index.candidate_materialization import validate_build_requirements
from cpip.index.candidates import InstallationCandidate
from cpip.index.links import Link
from cpip.resolution.resolver import Resolver
from cpip.build.build_backend import ProjectBuilder
from cpip.build.metadata import InstalledDistributionStore


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


def test_metadata_variation(
    benchmark: BenchmarkFixture, metadata_variation_wheels: list[Path]
) -> None:
    def read_all_metadata() -> int:
        reset_caches()
        return sum(
            len(read_wheel_metadata(wheel).get_all("Requires-Dist", ()))
            for wheel in metadata_variation_wheels
        )

    assert benchmark(read_all_metadata) > 100


def test_sdist_metadata_build(benchmark: BenchmarkFixture, source_tree: Path) -> None:
    def prepare_metadata() -> str:
        return prepare_project_metadata(source_tree, build_isolation=False).version

    assert benchmark(prepare_metadata) == "1.0.0"


def test_metadata_cache_miss(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    requirement = parse_requirement("payload-pkg")

    def load_uncached() -> str:
        candidate = InstallationCandidate.from_link(
            Link.from_path(payload_wheel, source_url=None)
        )
        materializer = CandidateMaterializer(build_isolation=False)
        return materializer.metadata_loader(candidate, requirement).load().name

    assert benchmark(load_uncached) == "payload-pkg"


def test_metadata_cache_hit(benchmark: BenchmarkFixture, payload_wheel: Path) -> None:
    requirement = parse_requirement("payload-pkg")
    candidate = InstallationCandidate.from_link(
        Link.from_path(payload_wheel, source_url=None)
    )
    materializer = CandidateMaterializer(build_isolation=False)
    materializer.metadata_loader(candidate, requirement).load()
    cached_loader = materializer.metadata_loader(candidate, requirement)

    assert benchmark(cached_loader.load).name == "payload-pkg"


def test_sdist_metadata_build_isolated(
    benchmark: BenchmarkFixture, isolated_source_tree: Path
) -> None:
    def prepare_metadata() -> str:
        return prepare_project_metadata(isolated_source_tree).version

    assert benchmark(prepare_metadata) == "1.0.0"


def test_editable_build(benchmark: BenchmarkFixture, source_tree: Path, tmp_path: Path) -> None:
    wheel_directory = tmp_path / "editable-wheel"
    wheel_directory.mkdir()

    def build_editable() -> str:
        return ProjectBuilder(source_tree).build_editable(wheel_directory)

    assert benchmark(build_editable).endswith(".whl")


def test_build_isolation_requirements_validation(
    benchmark: BenchmarkFixture, tmp_path: Path
) -> None:
    project = tmp_path / "build-requirements"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=65\", \"wheel>=0.40\", "
        "\"packaging>=23\"]\nbuild-backend = \"setuptools.build_meta\"\n",
        encoding="utf-8",
    )

    def validate() -> None:
        validate_build_requirements(project)

    benchmark(validate)


def test_sdist_metadata_build_failure(
    benchmark: BenchmarkFixture, failing_source_tree: Path
) -> None:
    def format_failure() -> int:
        try:
            prepare_project_metadata(failing_source_tree, build_isolation=False)
        except BuildError as error:
            return len(str(error))
        raise AssertionError("failing source tree unexpectedly built")

    assert benchmark(format_failure) > 20


def test_extras_marker_combinatorics(
    benchmark: BenchmarkFixture, extras_marker_wheelhouse: Path
) -> None:
    def resolve_extras() -> int:
        reset_caches()
        return len(
            Resolver(
                provider=CandidateProvider.from_options(
                    find_links=[str(extras_marker_wheelhouse)], no_index=True
                ),
                ignore_installed=True,
            )
            .resolve(["extras-root[all,dev]"])
            .candidates
        )

    assert benchmark(resolve_extras) > 40


def test_incremental_target_install(
    benchmark: BenchmarkFixture, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "incremental-wheelhouse"
    wheelhouse.mkdir()
    old = make_wheel(wheelhouse, "incremental-pkg", "1.0.0", payload_files=8)
    new = make_wheel(wheelhouse, "incremental-pkg", "2.0.0", payload_files=12)
    addon = make_wheel(wheelhouse, "incremental-addon", "1.0.0", payload_files=4)
    target_path = tmp_path / "incremental-target"
    target = InstallTarget.from_options("incremental-pkg", target=str(target_path))
    WheelInstaller(target, pycompile=False).install(old)
    installer = WheelInstaller(target, pycompile=False)
    uninstaller = DistributionUninstaller(paths=[str(target_path)])

    def update_target() -> int:
        installer.install(new)
        installer.install(addon)
        uninstaller.uninstall("incremental-addon")
        return len(tuple(target_path.rglob("*")))

    assert benchmark(update_target) > 10


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


def test_direct_url_and_constraint_parsing(benchmark: BenchmarkFixture) -> None:
    values = (
        "demo @ https://example.invalid/demo-1.0-py3-none-any.whl",
        "demo @ file:///tmp/demo-1.0.tar.gz",
        "git+https://example.invalid/demo.git@main#egg=demo",
        "demo[security,tests]>=1.0; python_version >= '3.9' and sys_platform != 'win32'",
    )

    def parse_mixed() -> int:
        reset_caches()
        return sum(len(parse_requirement(value).name) for value in values * 100)

    assert benchmark(parse_mixed) > 0


def test_frozen_real_world_corpus_parsing(benchmark: BenchmarkFixture) -> None:
    values = tuple(value for corpus in REAL_WORLD_CORPORA.values() for value in corpus)

    def parse_corpus() -> int:
        reset_caches()
        return sum(len(parse_requirement(value).name) for value in values)

    assert benchmark(parse_corpus) > 100


def test_installed_state_scan(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    wheelhouse = tmp_path / "installed-wheelhouse"
    wheelhouse.mkdir()
    target_path = tmp_path / "installed-target"
    target = InstallTarget.from_options("installed-0", target=str(target_path))
    installer = WheelInstaller(target, pycompile=False)
    wheels = [
        make_wheel(wheelhouse, f"installed-{index}", "1.0.0", payload_files=2)
        for index in range(48)
    ]
    for wheel in wheels:
        installer.install(wheel)

    def scan_installed() -> int:
        return len(InstalledDistributionStore(paths=[str(target_path)]).iter())

    assert benchmark(scan_installed) == 48


def test_cold_and_warm_resolution_cache(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    def cold_resolution() -> int:
        reset_caches()
        resolver = Resolver(
            provider=CandidateProvider.from_options(
                find_links=[str(graph_wheelhouse)], no_index=True
            ),
            ignore_installed=True,
        )
        return len(resolver.resolve(["application"]).candidates)

    assert benchmark(cold_resolution) > 10


def test_warm_resolution_cache(
    benchmark: BenchmarkFixture, graph_wheelhouse: Path
) -> None:
    resolver = Resolver(
        provider=CandidateProvider.from_options(
            find_links=[str(graph_wheelhouse)], no_index=True
        ),
        ignore_installed=True,
    )
    resolver.resolve(["application"])

    def warm_resolution() -> int:
        return len(resolver.resolve(["application"]).candidates)

    assert benchmark(warm_resolution) > 10
