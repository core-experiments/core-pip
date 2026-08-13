from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest
from cpip.cli.main import main
from cpip.core.packaging import Requirement, Version, parse_requirement
from cpip.core.wheel import TargetContext
from cpip.index.cache import origin_hashes
from cpip.index.catalog_cache import save_links
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.candidate_materialization import CandidateMaterializer
from cpip.index.candidates import InstallationCandidate
from cpip.index.directory_index import (
    local_source_snapshot,
    project_version_from_filename,
)
from cpip.index.links import Link
from cpip.index.provider import CandidateProvider
from cpip.index.source_locations import FindLinksSource, SimpleIndexSource
from cpip.index.source_models import (
    ArtifactKind,
    CandidateMetadata,
    CandidateRecord,
    CandidateSelection,
    MetadataFile,
    RejectionReason,
)
from cpip.index.vcs import is_immutable_vcs_link, vcs_reference
from cpip.network.http import HttpResponse
from cpip.network.cache import SafeFileCache

from ..wheel_helpers import make_sdist, make_wheel


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/path/page%231.html", "page#1.html"),
        ("https://example.com/a%252Fb.whl", "a%2Fb.whl"),
        (
            "https://example.com/myproject-1.0%2Bfoobar.0-py2.py3-none-any.whl",
            "myproject-1.0+foobar.0-py2.py3-none-any.whl",
        ),
        ("https://example.com/path/", "path"),
        ("https://example.com/foo/%2e%2e", "example.com"),
    ],
)
def test_link_filename_oracle(url: str, expected: str) -> None:
    provider = CandidateProvider.from_options(no_index=True)
    links = provider.collect_links(parse_requirement(f"demo @ {url}"))

    assert [link.filename for link in links] == [expected]


def test_local_source_snapshot_uses_directory_entry_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0.tar.gz"
    artifact.touch()
    (tmp_path / "project").mkdir()

    def fail_iterdir(path: Path):
        raise AssertionError(f"created Path entries before filtering: {path}")

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    snapshot = local_source_snapshot(os.fspath(tmp_path))
    assert snapshot is not None
    assert tuple(entry.path for entry in snapshot.entries) == (os.fspath(artifact),)


def test_source_archive_filename_normalizes_project_name() -> None:
    assert project_version_from_filename("PyHive-0.7.0.tar.gz") == (
        "pyhive",
        Version("0.7.0"),
    )
    assert project_version_from_filename("Demo_Pkg-1.0.zip") == (
        "demo-pkg",
        Version("1.0"),
    )


def test_find_links_reuses_local_artifact_identity_until_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0.tar.gz"
    artifact.write_bytes(b"artifact")
    source = FindLinksSource((str(tmp_path),))
    import cpip.index.directory_index as directory_index

    scan = directory_index.os.scandir
    calls = 0

    def counting_scan(path: str):
        nonlocal calls
        calls += 1
        return scan(path)

    monkeypatch.setattr(directory_index.os, "scandir", counting_scan)
    first = source.links_from_local_path(tmp_path)
    second = source.links_from_local_path(tmp_path)

    assert first[0].local_identity_internal == second[0].local_identity_internal
    assert calls == 1

    source.refresh_local_sources(str(tmp_path))
    source.links_from_local_path(tmp_path)
    assert calls == 2


def test_find_links_caches_local_file_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0.tar.gz"
    artifact.write_bytes(b"artifact")
    source = FindLinksSource((str(artifact),))
    import cpip.index.source_locations as source_locations

    stat = source_locations.os.stat
    calls = 0

    def counting_stat(path: str):
        nonlocal calls
        calls += 1
        return stat(path)

    monkeypatch.setattr(source_locations.os, "stat", counting_stat)
    assert source.links_from_local_path(artifact)
    assert source.links_from_local_path(artifact)
    assert calls == 0

    source.refresh_local_sources(str(artifact))
    source.links_from_local_path(artifact)
    assert calls == 0


@pytest.mark.parametrize(
    "url, repo_url, requested_revision",
    [
        (
            "git+file:///tmp/demo-pkg@master#egg=demo-pkg",
            "file:///tmp/demo-pkg",
            "master",
        ),
        (
            "git+file:///tmp/demo-pkg@refs/foo/bar#egg=demo-pkg",
            "file:///tmp/demo-pkg",
            "refs/foo/bar",
        ),
        (
            "git+https://example.com/demo/pkg.git@v1.0#egg=demo-pkg",
            "https://example.com/demo/pkg.git",
            "v1.0",
        ),
        (
            "git+ssh://git@example.com/demo/pkg.git@feature%40one#egg=demo-pkg",
            "ssh://git@example.com/demo/pkg.git",
            "feature@one",
        ),
    ],
)
def test_vcs_reference_splits_repo_url_and_revision(
    url: str,
    repo_url: str,
    requested_revision: str,
) -> None:
    reference = vcs_reference(url)

    assert reference.vcs == "git"
    assert reference.repo_url == repo_url
    assert reference.requested_revision == requested_revision


def test_immutable_vcs_link_requires_full_git_sha() -> None:
    assert is_immutable_vcs_link(
        "git+https://example.com/demo/pkg.git@"
        "0123456789abcdef0123456789abcdef01234567#egg=demo-pkg",
    )
    assert not is_immutable_vcs_link(
        "git+https://example.com/demo/pkg.git@master#egg=demo-pkg",
    )
    assert not is_immutable_vcs_link(
        "git+https://example.com/demo/pkg.git@0123456#egg=demo-pkg",
    )


def test_candidate_provider_reads_pep503_file_index(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    older = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    newer = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0")
    write_simple_project_index(index, "demo-pkg", [older, newer])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("Demo_Pkg>=1"))

    assert [str(candidate.version) for candidate in candidates] == ["2.0", "1.0"]


def test_candidate_provider_prunes_versions_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheels = [
        make_wheel(wheelhouse, "demo-pkg", "demo_pkg", version)
        for version in ("1.0", "2.0")
    ]
    write_simple_project_index(index, "demo-pkg", wheels)
    materialize = CandidateMaterializer.iter_materialize
    materialized: list[Version] = []

    def counting_materialize(
        materializer: CandidateMaterializer,
        requirement: Requirement,
        accepted: tuple[InstallationCandidate, ...],
    ) -> object:
        materialized.extend(candidate.version for candidate in accepted)
        yield from materialize(materializer, requirement, accepted)

    monkeypatch.setattr(CandidateMaterializer, "iter_materialize", counting_materialize)
    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(
        parse_requirement("demo-pkg"),
        allowed_versions=frozenset({Version("1.0")}),
    )

    assert [candidate.version for candidate in candidates] == [Version("1.0")]
    assert materialized == []
    assert Path(candidates[0].path).is_file()
    assert materialized == [Version("1.0")]


def test_warm_catalog_stream_constructs_only_consumed_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SafeFileCache(os.fspath(tmp_path / "cache"))
    page_url = "https://index.invalid/simple/demo/"
    links = [
        Link.from_url(
            f"https://files.invalid/demo-{version}-py3-none-any.whl",
            source_url=page_url,
        )
        for version in ("1.0", "2.0")
    ]
    save_links(cache, page_url, links)

    class Session:
        def __init__(self) -> None:
            self.cache = cache

        @staticmethod
        def has_fresh_cached_response(url: str) -> bool:
            del url
            return True

    requirement = parse_requirement("demo")
    first_provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )
    assert [
        candidate.version
        for candidate in first_provider.applicable_candidate_records(requirement)
    ] == [Version("2.0"), Version("1.0")]
    first_provider.close()

    provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )
    link_from_record = provider.link_from_catalog_record
    constructed: list[str] = []

    def counting_link_from_record(record: tuple[object, ...], source_url: str) -> Link:
        constructed.append(str(record[0]))
        return link_from_record(record, source_url)

    monkeypatch.setattr(provider, "link_from_catalog_record", counting_link_from_record)
    records = provider.lazy_catalog_records(requirement)

    assert records is not None
    assert next(records).version == Version("2.0")
    assert constructed == ["https://files.invalid/demo-2.0-py3-none-any.whl"]
    provider.close()


def test_candidate_provider_scans_find_links_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "first", "first", "1.0")
    make_wheel(wheelhouse, "second", "second", "1.0")
    collect_links = FindLinksSource.collect_links
    calls = 0

    def counting_collect_links(
        source: FindLinksSource,
        requirement: Requirement,
    ) -> list[Link]:
        nonlocal calls
        calls += 1
        return collect_links(source, requirement)

    monkeypatch.setattr(FindLinksSource, "collect_links", counting_collect_links)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    assert len(provider.collect_links(parse_requirement("first"))) == 2
    assert len(provider.collect_links(parse_requirement("second"))) == 2
    assert calls == 1


def test_local_find_links_do_not_start_catalog_prefetcher(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
        session=object(),
    )

    provider.prefetch_available_versions(
        (parse_requirement("first"), parse_requirement("second")),
    )

    assert provider.prefetcher is None


def test_warm_remote_indexes_do_not_start_catalog_prefetcher() -> None:
    checks = 0

    class Session:
        @staticmethod
        def has_fresh_cached_response(url: str) -> bool:
            nonlocal checks
            del url
            checks += 1
            return True

    provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )

    provider.prefetch_available_versions(
        (parse_requirement("first"), parse_requirement("second")),
    )
    provider.prefetch_available_versions(
        (parse_requirement("first"), parse_requirement("second")),
    )

    assert provider.prefetcher is None
    assert checks == 2


def test_exact_catalog_prefetch_starts_wheel_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Session:
        def get(self, url: str) -> object:
            calls.append(url)
            return object()

    link = Link.from_url(
        "https://packages.invalid/demo-1.0-py3-none-any.whl",
        source_url=None,
        metadata_file=MetadataFile(None),
    )
    record = CandidateRecord("demo", Version("1.0"), link)
    provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )
    monkeypatch.setattr(
        provider,
        "load_available_versions",
        lambda requirement, cache_key: (),
    )
    monkeypatch.setattr(
        provider,
        "evaluate_links",
        lambda requirement: CandidateSelection((record,), ()),
    )

    try:
        provider.load_prefetched_versions(
            (parse_requirement("demo==1.0"), ("demo", True, True)),
        )
    finally:
        provider.close()

    metadata_link = link.metadata_link()
    assert metadata_link is not None
    assert calls == [metadata_link.url]


def test_remote_index_fanout_preserves_order_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def collect(source: SimpleIndexSource, requirement: Requirement) -> list[Link]:
        del requirement
        return [
            Link.from_url(
                f"https://packages.invalid/{source.index_url}/demo-1.0.whl",
                source_url=None,
            ),
            Link.from_url("https://packages.invalid/shared-1.0.whl", source_url=None),
        ]

    monkeypatch.setattr(SimpleIndexSource, "collect_links", collect)
    provider = CandidateProvider.from_options(
        index_url="primary",
        extra_index_urls=["fallback"],
        session=object(),
    )
    try:
        links = provider.catalog_links(parse_requirement("demo"))
    finally:
        provider.close()

    assert [link.url for link in links] == [
        "https://packages.invalid/primary/demo-1.0.whl",
        "https://packages.invalid/shared-1.0.whl",
        "https://packages.invalid/fallback/demo-1.0.whl",
    ]


def test_candidate_provider_groups_find_links_catalog_by_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    first = make_wheel(wheelhouse, "first", "first", "1.0")
    make_wheel(wheelhouse, "second", "second", "1.0")
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    links = provider.catalog_links(parse_requirement("first"))

    assert [Path(link.file_path) for link in links] == [first]

    evaluate = CandidateEvaluator.evaluate_parsed_link
    evaluated: list[Path] = []

    def counting_evaluate(link: Link, *args: object, **kwargs: object) -> object:
        evaluated.append(Path(link.file_path))
        return evaluate(link, *args, **kwargs)

    monkeypatch.setattr(CandidateEvaluator, "evaluate_parsed_link", counting_evaluate)

    assert len(provider.find_candidates(parse_requirement("first"))) == 1
    assert evaluated == [first]


def test_candidate_provider_deduplicates_equivalent_index_artifacts(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    primary.mkdir()
    fallback.mkdir()
    make_wheel(primary, "demo-pkg", "demo_pkg", "1.0")
    make_wheel(fallback, "demo-pkg", "demo_pkg", "1.0")
    provider = CandidateProvider.from_options(
        find_links=[str(primary)],
        index_url=fallback.as_uri(),
    )

    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert len(candidates) == 1


def test_candidate_provider_reuses_candidate_materializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "first", "first", "1.0")
    make_wheel(wheelhouse, "second", "second", "1.0")
    initialize = CandidateMaterializer.__init__
    calls = 0

    def counting_initialize(
        materializer: CandidateMaterializer,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal calls
        calls += 1
        initialize(materializer, *args, **kwargs)

    monkeypatch.setattr(CandidateMaterializer, "__init__", counting_initialize)
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    assert len(provider.find_candidates(parse_requirement("first"))) == 1
    assert len(provider.find_candidates(parse_requirement("second"))) == 1
    assert calls == 1


def test_candidate_materializer_reuses_stable_wheel_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel_path = make_wheel(wheelhouse, "first", "first", "1.0")
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    materializer = provider.materializer_internal
    assert materializer is None

    original = CandidateMaterializer.iter_materialize
    materialized: list[Path] = []

    def counting_materialize(
        current: CandidateMaterializer,
        requirement: Requirement,
        accepted: tuple[InstallationCandidate, ...],
    ) -> object:
        materialized.extend(Path(candidate.link.file_path) for candidate in accepted)
        yield from original(current, requirement, accepted)

    monkeypatch.setattr(CandidateMaterializer, "iter_materialize", counting_materialize)
    first = provider.find_candidates(parse_requirement("first"))
    second = provider.find_candidates(parse_requirement("first>=1"))
    assert len(first) == 1
    assert len(second) == 1
    assert materialized == []

    assert first[0].path == os.fspath(wheel_path)
    assert second[0].path == os.fspath(wheel_path)
    assert materialized == [wheel_path, wheel_path]
    assert provider.materializer_internal is not None
    assert len(provider.materializer_internal.wheel_candidates) == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_pypi_metadata_precedes_sdist_build(
    dry_run: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.pythonhosted.org/packages/demo-1.0.tar.gz",
            source_url="https://pypi.org/simple/demo/",
        ),
    )
    materializer = CandidateMaterializer(dry_run=dry_run)
    expected = CandidateMetadata(
        name="demo",
        version=Version("1.0"),
        dependencies=(),
        provided_extras=frozenset(),
        requires_python=">=3.9",
    )
    monkeypatch.setattr(materializer, "pypi_metadata", lambda *args: expected)

    def fail_build(*args: object, **kwargs: object) -> None:
        pytest.fail("dry-run should not invoke the source build backend")

    monkeypatch.setattr("cpip.build.build_backend.prepare_project_metadata", fail_build)

    metadata = materializer.metadata_loader(candidate, parse_requirement("demo")).load()

    assert metadata is expected


def test_pypi_release_metadata_is_shared_by_artifacts() -> None:
    first = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.pythonhosted.org/packages/demo-1.0.tar.gz",
            source_url="https://pypi.org/simple/demo/",
        ),
    )
    second = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.pythonhosted.org/packages/demo-1.0.zip",
            source_url="https://pypi.org/simple/demo/",
        ),
    )

    class Session:
        calls = 0

        def get(self, url: str) -> HttpResponse:
            self.calls += 1
            assert url == "https://pypi.org/pypi/demo/1.0/json"
            return HttpResponse(
                status_code=200,
                reason="OK",
                url=url,
                headers={"Content-Type": "application/json"},
                raw=io.BytesIO(
                    b'{"info": {"name": "demo", "version": "1.0", '
                    b'"requires_dist": ["base", "extra; extra == \'feature\'"]}}',
                ),
            )

    session = Session()
    materializer = CandidateMaterializer(dry_run=True, session=session)
    first_metadata = materializer.pypi_metadata(first, frozenset())
    second_metadata = materializer.pypi_metadata(second, frozenset({"feature"}))

    assert session.calls == 1
    assert [item.name for item in first_metadata.dependencies] == ["base"]
    assert [item.name for item in second_metadata.dependencies] == ["base", "extra"]


def test_pypi_release_metadata_404_falls_back_to_artifact() -> None:
    candidate = CandidateRecord(
        name="legacy",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.pythonhosted.org/packages/legacy-1.0.tar.gz",
            source_url="https://pypi.org/simple/legacy/",
        ),
    )

    class Session:
        calls = 0

        def get(self, url: str) -> HttpResponse:
            self.calls += 1
            return HttpResponse(
                status_code=404,
                reason="Not Found",
                url=url,
                headers={},
                raw=io.BytesIO(),
            )

    session = Session()
    materializer = CandidateMaterializer(dry_run=True, session=session)

    assert materializer.pypi_metadata(candidate, frozenset()) is None
    assert materializer.pypi_metadata(candidate, frozenset()) is None
    assert session.calls == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_reads_detached_wheel_metadata_without_download(
    dry_run: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl",
            source_url="https://pypi.org/simple/demo/",
            metadata_file=MetadataFile(None),
        ),
    )

    class Session:
        def get(self, url: str) -> HttpResponse:
            assert url.endswith("demo-1.0-py3-none-any.whl.metadata")
            return HttpResponse(
                status_code=200,
                reason="OK",
                url=url,
                headers={"Content-Type": "text/plain"},
                raw=io.BytesIO(
                    b"Name: demo\nVersion: 1.0\nRequires-Dist: requests>=2\n",
                ),
            )

    materializer = CandidateMaterializer(dry_run=dry_run, session=Session())
    monkeypatch.setattr(
        materializer,
        "ensure_local_text",
        lambda *args, **kwargs: pytest.fail("wheel should not be downloaded"),
    )

    metadata = materializer.metadata_loader(candidate, parse_requirement("demo")).load()

    assert metadata.version == Version("1.0")
    assert metadata.dependencies[0].raw == "requests>=2"


def test_candidate_provider_parses_index_artifacts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheels = [
        make_wheel(wheelhouse, "demo-pkg", "demo_pkg", version)
        for version in ("1.0", "2.0")
    ]
    write_simple_project_index(index, "demo-pkg", wheels)
    original = InstallationCandidate.from_link
    calls = 0

    def counting_from_link(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(InstallationCandidate, "from_link", counting_from_link)
    provider = CandidateProvider.from_options(index_url=index.as_uri())

    assert len(provider.available_versions(parse_requirement("demo-pkg"))) == 2

    def fail_catalog(requirement: Requirement) -> list[Link]:
        raise AssertionError(f"reloaded indexed project catalog: {requirement}")

    monkeypatch.setattr(provider, "catalog_links", fail_catalog)
    evaluate_parsed_link = CandidateEvaluator.evaluate_parsed_link
    evaluations = 0

    def counting_evaluate(*args: object, **kwargs: object) -> object:
        nonlocal evaluations
        evaluations += 1
        return evaluate_parsed_link(*args, **kwargs)

    monkeypatch.setattr(CandidateEvaluator, "evaluate_parsed_link", counting_evaluate)
    assert len(provider.find_candidates(parse_requirement("demo-pkg==2"))) == 1
    assert evaluations == 1
    assert len(provider.find_candidates(parse_requirement("demo-pkg<2"))) == 1
    assert calls == 2


def test_candidate_provider_reuses_catalog_for_exact_version_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0")
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    requirement = parse_requirement("demo-pkg")

    assert len(provider.available_versions_for(requirement, Version("1.0"))) == 1

    def fail_catalog(requirement: Requirement) -> None:
        raise AssertionError(f"reloaded populated version catalog: {requirement}")

    monkeypatch.setattr(provider, "available_versions", fail_catalog)

    assert len(provider.available_versions_for(requirement, Version("2.0"))) == 1


def test_candidate_provider_skips_release_filter_for_stable_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )

    def fail_evaluator(*args: object, **kwargs: object) -> None:
        raise AssertionError("re-filtered an all-stable candidate set")

    monkeypatch.setattr(CandidateEvaluator, "create", fail_evaluator)

    candidates = provider.find_candidates(parse_requirement("demo-pkg>=1"))

    assert [candidate.path for candidate in candidates] == [os.fspath(wheel)]


def test_candidate_provider_filters_wheels_for_download_target(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    linux = wheelhouse / "demo_pkg-2.0-py3-none-linux_x86_64.whl"
    linux.write_bytes(
        make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0").read_bytes(),
    )
    any_wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    any_target = wheelhouse / "demo_pkg-1.0-py3-none-any.whl"
    if any_target != any_wheel:
        any_target.write_bytes(any_wheel.read_bytes())
    write_simple_project_index(index, "demo-pkg", [linux, any_target])

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        target=TargetContext(platforms=("linux_x86_64",)),
    )
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert [os.path.basename(candidate.path) for candidate in candidates] == [
        "demo_pkg-2.0-py3-none-linux_x86_64.whl",
        "demo_pkg-1.0-py3-none-any.whl",
    ]


def test_origin_hashes_with_invalid_json(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    origin_file = tmp_path / "origin.json"
    origin_file.write_text("{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="cpip.index.candidate_evaluators"):
        hashes = origin_hashes(origin_file)

    assert hashes is None
    assert any(
        "Ignoring invalid cache entry origin file" in record.message
        for record in caplog.records
    )


def test_evaluate_links_propagates_unexpected_source_tree_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.joinpath("src", "dir_pkg").mkdir(parents=True)
    project.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "dir-pkg"',
                'version = "1.0"',
                "",
            ],
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)
    monkeypatch.setattr(
        "cpip.build.build_backend.prepare_project_metadata",
        lambda *args_internal: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        provider.evaluate_links(parse_requirement(str(project)))


@pytest.mark.parametrize(
    "anchor_html, expected",
    [
        ('<a href="/pkg-1.0.tar.gz"></a>', None),
        ('<a href="/pkg-1.0.tar.gz" data-requires-python></a>', None),
        ('<a href="/pkg-1.0.tar.gz" data-requires-python="&gt;=3.6"></a>', ">=3.6"),
        (
            '<a href="/pkg-1.0.tar.gz" data-requires-python="&amp;gt;=3.6"></a>',
            "&gt;=3.6",
        ),
    ],
)
def test_html_requires_python_oracle(
    tmp_path: Path,
    anchor_html: str,
    expected: str | None,
) -> None:
    write_simple_project_html(tmp_path / "simple", "pkg", anchor_html)

    provider = CandidateProvider.from_options(index_url=(tmp_path / "simple").as_uri())
    links = provider.collect_links(parse_requirement("pkg"))

    assert [link.requires_python for link in links] == [expected]


@pytest.mark.parametrize(
    "anchor_html, expected",
    [
        ('<a href="/pkg1-1.0.tar.gz"></a>', None),
        ('<a href="/pkg2-1.0.tar.gz" data-yanked></a>', None),
        ('<a href="/pkg3-1.0.tar.gz" data-yanked=""></a>', ""),
        ('<a href="/pkg4-1.0.tar.gz" data-yanked="error"></a>', "error"),
        ('<a href="/pkg4-1.0.tar.gz" data-yanked="version &lt 1"></a>', "version < 1"),
        (
            '<a href="/pkg-1.0.tar.gz" data-yanked="curlyquote \u2018"></a>',
            "curlyquote \u2018",
        ),
        (
            '<a href="/pkg-1.0.tar.gz" data-yanked="version &amp;lt; 1"></a>',
            "version &lt; 1",
        ),
    ],
)
def test_html_yanked_reason_oracle(
    tmp_path: Path,
    anchor_html: str,
    expected: str | None,
) -> None:
    write_simple_project_html(tmp_path / "simple", "pkg", anchor_html)

    provider = CandidateProvider.from_options(index_url=(tmp_path / "simple").as_uri())
    links = provider.collect_links(parse_requirement("pkg"))

    assert [link.yanked_reason for link in links] == [expected]


def test_html_core_metadata_oracle(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    anchor = (
        '<a href="/pkg1-1.0.tar.gz#sha512=abc132409cb" '
        'data-core-metadata="sha256=aa113592bbe" '
        'data-dist-info-metadata="sha256=invalid_value"></a>'
    )
    write_simple_project_html(index, "pkg1", anchor)

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    links = provider.collect_links(parse_requirement("pkg1"))

    assert links[0].hashes == {"sha512": "abc132409cb"}
    assert links[0].metadata_file == MetadataFile({"sha256": "aa113592bbe"})


def test_json_simple_api_link_oracle(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    project = index / "holygrail"
    project.mkdir(parents=True)
    (project / "index.json").write_text(
        """
        {
          "meta": {"api-version": "1.0"},
          "name": "holygrail",
          "files": [
            {
              "filename": "holygrail-1.0.tar.gz",
              "url": "https://example.com/files/holygrail-1.0.tar.gz",
              "hashes": {"sha256": "sha256 hash", "blake2b": "blake2b hash"},
              "requires-python": ">=3.7",
              "yanked": "Had a vulnerability"
            },
            {
              "filename": "holygrail-1.0-py3-none-any.whl",
              "url": "/files/holygrail-1.0-py3-none-any.whl",
              "hashes": {"sha256": "sha256 hash", "blake2b": "blake2b hash"},
              "requires-python": ">=3.7",
              "core-metadata": {"sha512": "aabdd41"},
              "dist-info-metadata": {"sha512": "this_is_wrong"}
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    links = provider.collect_links(parse_requirement("holygrail"))

    assert links[0].url == "https://example.com/files/holygrail-1.0.tar.gz"
    assert links[0].hashes == {"sha256": "sha256 hash", "blake2b": "blake2b hash"}
    assert links[0].requires_python == ">=3.7"
    assert links[0].yanked_reason == "Had a vulnerability"
    assert links[1].url == "file:///files/holygrail-1.0-py3-none-any.whl"
    assert links[1].metadata_file == MetadataFile({"sha512": "aabdd41"})


def test_candidate_provider_normalizes_project_names_on_all_indexes(
    tmp_path: Path,
) -> None:
    first_index = tmp_path / "index1"
    second_index = tmp_path / "index2"
    wheelhouse = tmp_path / "packages"
    first_index.mkdir()
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "complex-name", "complex_name", "1.0")
    write_simple_project_index(second_index, "complex-name", [wheel])

    provider = CandidateProvider.from_options(
        index_url=first_index.as_uri(),
        extra_index_urls=[second_index.as_uri()],
    )
    candidates = provider.find_candidates(parse_requirement("Complex_Name"))

    assert [candidate.path for candidate in candidates] == [os.fspath(wheel)]


def test_candidate_provider_reads_direct_file_url(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True)
    candidates = provider.find_candidates(
        parse_requirement(f"demo-pkg @ {wheel.as_uri()}"),
    )

    assert [candidate.path for candidate in candidates] == [os.fspath(wheel)]


def test_candidate_provider_reads_direct_project_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.joinpath("src", "dir_pkg").mkdir(parents=True)
    project.joinpath("src", "dir_pkg", "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    project.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "dir-pkg"',
                'version = "1.0"',
                "",
            ],
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)
    selection = provider.evaluate_links(parse_requirement(str(project)))
    candidates = provider.find_candidates(parse_requirement(str(project)))

    assert [candidate.link.kind for candidate in selection.accepted] == [
        ArtifactKind.SOURCE_TREE,
    ]
    assert [candidate.name for candidate in candidates] == ["dir-pkg"]
    assert [str(candidate.version) for candidate in candidates] == ["1.0"]


def test_candidate_provider_rejects_invalid_source_tree_version(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.joinpath("src", "dir_pkg").mkdir(parents=True)
    project.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "dir-pkg"',
                'version = "not-a-version"',
                "",
            ],
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)

    selection = provider.evaluate_links(parse_requirement(str(project)))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.INVALID_VERSION,
    ]


def test_evaluate_links_rejects_incompatible_requires_python(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py3-none-any.whl" data-requires-python=">=99"></a>',
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    selection = provider.evaluate_links(parse_requirement("demo-pkg"))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.REQUIRES_PYTHON,
    ]


def test_evaluate_links_rejects_unsupported_wheel_tags(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py1-none-any.whl"></a>',
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    selection = provider.evaluate_links(parse_requirement("demo-pkg"))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.UNSUPPORTED_WHEEL,
    ]


def test_evaluate_links_yanked_policy_oracle(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py3-none-any.whl" data-yanked="bad release"></a>',
    )
    provider = CandidateProvider.from_options(index_url=index.as_uri())

    unpinned = provider.evaluate_links(parse_requirement("demo-pkg"))
    pinned = provider.evaluate_links(parse_requirement("demo-pkg==1.0"))

    assert [rejected.reason for rejected in unpinned.rejected] == [
        RejectionReason.YANKED,
    ]
    assert [str(candidate.version) for candidate in pinned.accepted] == ["1.0"]


def test_evaluate_links_collects_all_artifact_kinds(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    write_simple_project_html(
        index,
        "demo-pkg",
        "\n".join(
            [
                '<a href="demo_pkg-1.0-py3-none-any.whl"></a>',
                '<a href="demo-pkg-1.0.tar.gz"></a>',
                '<a href="demo-pkg-1.0.tar.lzma"></a>',
                '<a href="demo-pkg-1.0.tar.gz.metadata"></a>',
                '<a href="demo-pkg-1.0.tar.gz.attestation"></a>',
                '<a href="demo-pkg-1.0.unknown"></a>',
            ],
        ),
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    links = provider.collect_links(parse_requirement("demo-pkg"))
    selection = provider.evaluate_links(parse_requirement("demo-pkg"))

    assert [link.kind for link in links] == [
        ArtifactKind.WHEEL,
        ArtifactKind.SDIST,
        ArtifactKind.SDIST,
        ArtifactKind.METADATA,
        ArtifactKind.ATTESTATION,
        ArtifactKind.UNKNOWN,
    ]
    assert [candidate.link.kind for candidate in selection.accepted] == [
        ArtifactKind.WHEEL,
        ArtifactKind.SDIST,
        ArtifactKind.SDIST,
    ]
    assert {rejected.reason for rejected in selection.rejected} == {
        RejectionReason.UNSUPPORTED_ARTIFACT,
    }


def test_candidate_provider_builds_sdist_candidate(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(
        packages,
        "source-pkg",
        "source_pkg",
        "1.0",
        requires=["dep-pkg>=1"],
    )
    write_simple_project_archive_index(index, "source-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("source-pkg"))

    assert [candidate.name for candidate in candidates] == ["source-pkg"]
    assert [str(candidate.version) for candidate in candidates] == ["1.0"]
    assert os.path.basename(candidates[0].path) == "source_pkg-1.0-py3-none-any.whl"
    assert [dependency.raw for dependency in candidates[0].dependencies] == [
        "dep-pkg>=1",
    ]


def test_candidate_provider_defers_sdist_build_when_matching_wheel_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [wheel, sdist])

    def fail_build(*args_internal, **kwargs_internal):
        raise AssertionError("sdist build should be skipped when a wheel exists")

    monkeypatch.setattr("cpip.build.build.build_wheel_from_source", fail_build)

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert os.path.basename(candidates[0].path) == wheel.name


def test_candidate_provider_only_builds_highest_ranked_source_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    newest = make_sdist(packages, "demo-pkg", "demo_pkg", "3.0")
    older = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_archive_index(index, "demo-pkg", [newest, older, wheel])

    built: list[str] = []
    real_build = __import__(
        "cpip.build.build",
        fromlist=["build_wheel_from_source"],
    ).build_wheel_from_source

    def tracking_build(path, *args, **kwargs):
        built.append(Path(path).name)
        return real_build(path, *args, **kwargs)

    monkeypatch.setattr(
        "cpip.build.build.build_wheel_from_source",
        tracking_build,
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert built == []
    preferred = candidates[:2]

    assert Path(preferred[0].path).is_file()
    # nab keeps source candidates lazy until they are selected/materialized.
    assert built == []
    assert [str(candidate.version) for candidate in preferred] == ["3.0", "1.0"]


def test_candidate_provider_runs_project_build_backend(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(
        packages,
        "backend-pkg",
        "backend_pkg",
        "1.0",
        standalone_backend=True,
    )
    write_simple_project_archive_index(index, "backend-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("backend-pkg"))

    assert [os.path.basename(candidate.path) for candidate in candidates] == [
        "backend_pkg-1.0-py3-none-any.whl",
    ]


def test_candidate_provider_prefers_wheel_over_matching_sdist(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "priority-pkg", "priority_pkg", "1.0")
    sdist = make_sdist(packages, "priority-pkg", "priority_pkg", "1.0")
    write_simple_project_archive_index(index, "priority-pkg", [sdist, wheel])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("priority-pkg"))

    assert candidates[0].path == os.fspath(wheel)


def test_core_download_uses_index_and_extra_index_url(tmp_path: Path, capsys) -> None:
    primary_index = tmp_path / "primary"
    secondary_index = tmp_path / "secondary"
    wheelhouse = tmp_path / "packages"
    dest = tmp_path / "dest"
    primary_index.mkdir()
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_index(secondary_index, "demo-pkg", [wheel])

    status = main(
        [
            "download",
            "--index-url",
            primary_index.as_uri(),
            "--extra-index-url",
            secondary_index.as_uri(),
            "--dest",
            str(dest),
            "demo-pkg",
        ],
    )
    captured = capsys.readouterr()

    assert status == 0, captured.err
    assert (dest / wheel.name).is_file()
    assert "Successfully downloaded demo-pkg" in captured.out


def test_core_download_no_index_ignores_index_url(tmp_path: Path, capsys) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    dest = tmp_path / "dest"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    write_simple_project_index(index, "demo-pkg", [wheel])

    status = main(
        [
            "download",
            "--no-index",
            "--index-url",
            index.as_uri(),
            "--dest",
            str(dest),
            "demo-pkg",
        ],
    )
    captured = capsys.readouterr()

    assert status == 1
    assert not (dest / wheel.name).exists()
    assert "No matching distribution found for demo-pkg" in captured.err


def write_simple_project_index(index: Path, project: str, wheels: list[Path]) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    links = []
    for wheel in wheels:
        href = os.path.relpath(wheel, project_dir).replace(os.sep, "/")
        links.append(f'<a href="{href}#sha256=test">{wheel.name}</a>')
    (project_dir / "index.html").write_text("\n".join(links) + "\n", encoding="utf-8")


def write_simple_project_archive_index(
    index: Path,
    project: str,
    archives: list[Path],
) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    links = []
    for archive in archives:
        href = os.path.relpath(archive, project_dir).replace(os.sep, "/")
        links.append(f'<a href="{href}">{archive.name}</a>')
    (project_dir / "index.html").write_text("\n".join(links) + "\n", encoding="utf-8")


def write_simple_project_html(index: Path, project: str, html: str) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    project_html = f"<!DOCTYPE html><html><body>{html}</body></html>"
    (project_dir / "index.html").write_text(project_html, encoding="utf-8")
