from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pip.core.errors import (
    DirectoryUrlHashUnsupported,
    DistributionNotFound,
    HashMismatch,
    HashMissing,
    HashUnpinned,
    VcsHashUnsupported,
)
from pip.core.format_control import FormatControl
from pip.core.packaging import parse_requirement, Requirement, Version
from pip.core.wheel import WheelCandidate
from pip.index.cache import wheel_cache_path
from pip.index.candidate_materialization import CandidateStream
from pip.index.provider import CandidateProvider
from pip.index.source_models import CandidateSummary
from pip.resolution.req_install import (
    file_hashes,
    install_req_from_editable,
    install_req_from_line,
)
from pip.resolution.requirement_set import RequirementSet
from pip.resolution.resolver import Resolver
from wheel_helpers import make_sdist, make_wheel


class _CountingFailedResolver(Resolver):
    def __init__(self) -> None:
        super().__init__(no_index=True)
        self.uncached_searches = 0

    def _search_uncached(  # type: ignore[override]
        self, *args: object, **kwargs: object
    ) -> bool:
        self.uncached_searches += 1
        return False


def test_resolver_memoizes_equivalent_failed_search_states() -> None:
    resolver = _CountingFailedResolver()
    pending = [resolver._apply_constraints(parse_requirement("demo-pkg>=1"))]
    search_args = (pending, {}, {}, {}, {"<root>": set()})
    search_kwargs = {
        "source_requirements": {},
        "source_requirements_by_url": {},
    }

    assert not resolver._search(*search_args, **search_kwargs)
    assert not resolver._search(*search_args, **search_kwargs)

    assert resolver.uncached_searches == 1


def test_resolver_caches_viable_candidate_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_archive_index(index, "demo-pkg", [old, new])
    provider = CandidateProvider.from_options(index_url=index.as_uri())
    available_versions = provider.available_versions
    calls = 0

    def counting_available_versions(
        requirement: Requirement,
    ) -> tuple[CandidateSummary, ...]:
        nonlocal calls
        calls += 1
        return available_versions(requirement)

    monkeypatch.setattr(provider, "available_versions", counting_available_versions)
    resolver = Resolver(provider=provider)

    assert resolver._candidate_count(parse_requirement("demo-pkg>=2")) == 1
    assert resolver._candidate_count(parse_requirement("demo-pkg>=2")) == 1
    assert resolver._candidate_count(parse_requirement("demo-pkg<2")) == 1
    assert calls == 2


def test_resolver_caches_candidate_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_archive_index(index, "demo-pkg", [old, new])
    provider = CandidateProvider.from_options(index_url=index.as_uri())
    find_candidates = provider.find_candidates
    calls = 0

    def counting_find_candidates(
        requirement: Requirement,
    ) -> CandidateStream:
        nonlocal calls
        calls += 1
        return find_candidates(requirement)

    monkeypatch.setattr(provider, "find_candidates", counting_find_candidates)
    resolver = Resolver(provider=provider)
    requirement = parse_requirement("demo-pkg")

    first = resolver._find_candidates(requirement)
    second = resolver._find_candidates(requirement)

    assert [candidate.version for candidate in first] == [
        Version("2.0"),
        Version("1.0"),
    ]
    assert list(second) == list(first)
    assert calls == 1


def test_resolver_prefers_stable_release_by_default(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    _write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_allows_prerelease_with_pre_flag(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    _write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider, allow_prereleases=True).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0b1"]


def test_resolver_allows_prerelease_when_specifier_mentions_one(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    stable = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    prerelease = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0b1")
    _write_simple_project_archive_index(index, "demo-pkg", [stable, prerelease])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider).resolve(["demo-pkg>=0.0.dev0"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0b1"]


def test_resolver_only_binary_rejects_source_only_project(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [sdist])
    format_control = FormatControl()
    format_control.apply("only-binary", ":all:")

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        format_control=format_control,
    )

    with pytest.raises(DistributionNotFound, match="No matching distribution found"):
        Resolver(provider=provider).resolve(["demo-pkg"])


def test_resolver_no_binary_allows_source_only_selection(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_archive_index(index, "demo-pkg", [wheel, sdist])
    format_control = FormatControl()
    format_control.apply("no-binary", ":all:")

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        format_control=format_control,
    )
    plan = Resolver(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["2.0"]


def test_resolver_prefer_binary_prefers_older_wheel_over_newer_source(
    tmp_path: Path,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_archive_index(index, "demo-pkg", [sdist, wheel])

    default_provider = CandidateProvider.from_options(index_url=index.as_uri())
    preferred_provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        prefer_binary=True,
    )

    default_plan = Resolver(provider=default_provider).resolve(["demo-pkg"])
    preferred_plan = Resolver(provider=preferred_provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in default_plan.candidates] == ["2.0"]
    assert preferred_plan.candidates[0].path == wheel


def test_resolver_applies_version_constraints(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    old = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    new = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_archive_index(index, "demo-pkg", [old, new])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider, constraints=["demo-pkg<2"]).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_indexes_constraints_by_canonical_name() -> None:
    resolver = Resolver(
        no_index=True,
        constraints=["Demo_Pkg>=1", "demo-pkg<3", "other-pkg==2"],
    )

    constrained = resolver._apply_constraints(parse_requirement("DEMO.pkg"))

    assert set(resolver._constraints_by_name) == {"demo-pkg", "other-pkg"}
    assert constrained.is_satisfied_by("1")
    assert constrained.is_satisfied_by("2")
    assert not constrained.is_satisfied_by("3")


def test_resolver_maintains_reverse_dependency_index(tmp_path: Path) -> None:
    resolver = Resolver(no_index=True)
    candidate = WheelCandidate(
        name="parent",
        version=Version("1"),
        path=tmp_path / "parent.whl",
        dependencies=(
            parse_requirement("child>=1"),
            parse_requirement("child<3"),
            parse_requirement("other"),
        ),
    )
    sibling = WheelCandidate(
        name="sibling",
        version=Version("1"),
        path=tmp_path / "sibling.whl",
        dependencies=(parse_requirement("child>=2"),),
    )

    resolver._add_candidate_dependencies("parent", candidate)
    resolver._add_candidate_dependencies("sibling", sibling)
    active = resolver._active_requirements_for(
        "child", parse_requirement("child!=2"), [parse_requirement("deferred")]
    )

    assert [item.raw for item in active] == [
        "child!=2",
        "child>=1",
        "child<3",
        "child>=2",
        "deferred",
    ]

    resolver._remove_candidate_dependencies("parent", candidate)
    assert tuple(resolver._incoming_requirements["child"]) == ("sibling",)

    resolver._remove_candidate_dependencies("sibling", sibling)
    assert resolver._incoming_requirements == {}


def test_resolver_only_materializes_top_matching_wheels(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    newest = make_wheel(packages, "demo-pkg", "demo_pkg", "3.0")
    older = make_wheel(packages, "demo-pkg", "demo_pkg", "2.0")
    prerelease = make_wheel(packages, "demo-pkg", "demo_pkg", "4.0b1")
    _write_simple_project_archive_index(index, "demo-pkg", [prerelease, newest, older])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider).resolve(["demo-pkg"])

    assert [str(candidate.version) for candidate in plan.candidates] == ["3.0"]


def test_resolver_applies_direct_url_constraint(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    direct = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True)
    plan = Resolver(
        provider=provider,
        constraints=[f"demo-pkg @ {direct.as_uri()}"],
    ).resolve(["demo-pkg"])

    assert len(plan.candidates) == 1
    assert plan.candidates[0].path == direct


def test_resolver_prefers_direct_requirement_over_index_candidates(
    tmp_path: Path,
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    indexed = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    direct = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [indexed])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    plan = Resolver(provider=provider).resolve(
        ["demo-pkg", f"demo-pkg @ {direct.as_uri()}"]
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].path == direct


def test_resolver_accepts_requirement_set_input(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    reqset = RequirementSet()
    reqset.add_named_requirement(install_req_from_line("demo-pkg"))

    plan = Resolver(provider=provider).resolve(reqset)

    assert [str(candidate.version) for candidate in plan.candidates] == ["1.0"]


def test_resolver_can_return_requirement_set(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [wheel])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    resolved = Resolver(provider=provider).resolve_requirement_set(["demo-pkg"])

    assert resolved.has_requirement("demo-pkg")
    assert len(resolved.all_requirements) == 1
    req = resolved.get_requirement("demo-pkg")
    assert req.req is not None
    assert str(req.req) == "demo-pkg==1.0"
    assert req.link is not None
    assert req.link.filename == wheel.name


def test_resolved_requirement_set_includes_download_info_for_local_wheel(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")

    resolved = Resolver().resolve_requirement_set([wheel.as_posix()])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(wheel)


def test_resolved_requirement_set_includes_download_info_for_local_dir(
    tmp_path: Path,
) -> None:
    source = tmp_path / "demo-pkg"
    package = source / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    resolved = Resolver().resolve_requirement_set([source.as_posix()])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.dir_info is not None
    assert req.download_info.dir_info.editable is False


def test_resolved_requirement_set_includes_download_info_for_find_links(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )
    resolved = Resolver(provider=provider).resolve_requirement_set(["demo-pkg"])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(wheel)


def test_resolved_requirement_set_preserves_direct_archive_url(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()

    resolved = Resolver().resolve_requirement_set([f"demo-pkg @ {archive_url}"])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.url == archive_url
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == file_hashes(archive)


def test_resolved_requirement_set_marks_local_editable_dir(
    tmp_path: Path,
) -> None:
    source = tmp_path / "editable-demo"
    package = source / "editable_demo"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text(
        "NAME = 'editable-demo'\n", encoding="utf-8"
    )
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "editable-demo"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    resolved = Resolver().resolve_requirement_set(
        [install_req_from_editable(source.as_posix())]
    )

    req = resolved.all_requirements[0]
    assert req.editable is True
    assert req.download_info is not None
    assert req.download_info.url.startswith("file://")
    assert req.download_info.dir_info is not None
    assert req.download_info.dir_info.editable is True


def test_resolved_requirement_set_includes_vcs_info_for_git_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    package = repo / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    repo.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    resolved = Resolver().resolve_requirement_set([f"demo-pkg @ git+{repo.as_uri()}"])

    req = resolved.all_requirements[0]
    assert req.download_info is not None
    assert req.download_info.vcs_info is not None
    assert req.download_info.vcs_info.vcs == "git"
    assert req.download_info.url == repo.as_uri()


def test_resolved_requirement_set_marks_cached_wheel_for_direct_archive(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir.mkdir(parents=True)
    cached_wheel = make_wheel(entry_dir, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = Resolver(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"]
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.cached_wheel_source_link is not None
    assert req.cached_wheel_source_link.url == archive_url
    assert req.download_info is not None
    assert req.download_info.url == archive_url
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {}
    assert req.link is not None
    assert req.link.filename == cached_wheel.name


def test_resolved_requirement_set_reads_origin_hashes_from_cache(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir.mkdir(parents=True)
    make_wheel(entry_dir, "demo-pkg", "demo_pkg", "1.0")
    entry_dir.joinpath("origin.json").write_text(
        f'{{"url": "{archive_url}", '
        '"archive_info": {"hashes": {"sha256": "abc123"}}}',
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = Resolver(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"]
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.download_info is not None
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {"sha256": "abc123"}


def test_invalid_cache_origin_file_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    archive_url = archive.as_uri()
    cache_dir = tmp_path / "cache"
    entry_dir = wheel_cache_path(cache_dir, archive_url)
    entry_dir.mkdir(parents=True)
    make_wheel(entry_dir, "demo-pkg", "demo_pkg", "1.0")
    entry_dir.joinpath("origin.json").write_text("{", encoding="utf-8")

    provider = CandidateProvider.from_options(no_index=True, wheel_cache_dir=cache_dir)
    resolved = Resolver(provider=provider).resolve_requirement_set(
        [f"demo-pkg @ {archive_url}"]
    )

    req = resolved.all_requirements[0]
    assert req.is_wheel_from_cache is True
    assert req.download_info is not None
    assert req.download_info.archive_info is not None
    assert req.download_info.archive_info.hashes == {}
    assert any(
        "Ignoring invalid cache entry origin file" in message
        for message in caplog.messages
    )


def test_require_hashes_rejects_missing_hash_for_pinned_requirement(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )
    reqset = RequirementSet()
    reqset.add_named_requirement(install_req_from_line("demo-pkg==1.0"))

    with pytest.raises(
        HashMissing, match="Missing hash for:\n    demo-pkg==1.0 --hash=sha256:"
    ) as exc_info:
        Resolver(provider=provider, require_hashes=True).resolve(reqset)
    assert file_hashes(wheel)["sha256"] in str(exc_info.value)


def test_require_hashes_rejects_vcs_requirements(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    repo.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    req = install_req_from_line(f"demo-pkg @ git+{repo.as_uri()}")
    req.hash_options = {"sha256": ["badbad"]}

    with pytest.raises(VcsHashUnsupported, match="hash version control repositories"):
        Resolver(require_hashes=True).resolve([req])


def test_require_hashes_rejects_directory_urls(tmp_path: Path) -> None:
    source = tmp_path / "demo-pkg"
    package = source / "demo_pkg"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("NAME = 'demo-pkg'\n", encoding="utf-8")
    source.joinpath("pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-pkg"',
                'version = "1.0"',
                "dependencies = []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    req = install_req_from_line(source.as_posix())

    with pytest.raises(DirectoryUrlHashUnsupported, match="they point to directories"):
        Resolver(require_hashes=True).resolve([req])


def test_require_hashes_rejects_hash_mismatch(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    req = install_req_from_line(f"demo-pkg @ {archive.as_uri()}")
    req.hash_options = {"sha256": ["badbad"]}

    with pytest.raises(HashMismatch, match="Expected sha256 badbad"):
        Resolver(require_hashes=True).resolve([req])


def test_require_hashes_lazily_selects_matching_artifact(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    archive = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [wheel, archive])
    req = install_req_from_line("demo-pkg==1.0")
    req.hash_options = {"sha256": [file_hashes(archive)["sha256"]]}
    provider = CandidateProvider.from_options(index_url=index.as_uri())

    plan = Resolver(provider=provider, require_hashes=True).resolve([req])

    assert len(plan.candidates) == 1
    assert plan.candidates[0].source_kind == "sdist"


def test_require_hashes_rejects_unpinned_transitive_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(
        wheelhouse,
        "toporequires",
        "toporequires",
        "0.0.1",
    )
    make_wheel(
        wheelhouse,
        "toporequires2",
        "toporequires2",
        "0.0.1",
        requires=["toporequires"],
    )
    req = install_req_from_line("toporequires2==0.0.1")
    req.hash_options = {
        "sha256": [
            file_hashes(wheelhouse / "toporequires2-0.0.1-py3-none-any.whl")["sha256"]
        ]
    }
    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )

    with pytest.raises(HashUnpinned, match="Unpinned requirement:\n    toporequires"):
        Resolver(provider=provider, require_hashes=True).resolve([req])


def test_require_hashes_allows_hashed_transitive_dependency(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    dep_wheel = make_wheel(
        wheelhouse,
        "toporequires",
        "toporequires",
        "0.0.1",
    )
    parent_wheel = make_wheel(
        wheelhouse,
        "toporequires2",
        "toporequires2",
        "0.0.1",
        requires=["toporequires==0.0.1"],
    )
    dep_req = install_req_from_line("toporequires==0.0.1")
    dep_req.hash_options = {"sha256": [file_hashes(dep_wheel)["sha256"]]}
    parent_req = install_req_from_line("toporequires2==0.0.1")
    parent_req.hash_options = {"sha256": [file_hashes(parent_wheel)["sha256"]]}
    provider = CandidateProvider.from_options(
        find_links=[wheelhouse.as_posix()],
        no_index=True,
    )

    plan = Resolver(provider=provider, require_hashes=True).resolve(
        [parent_req, dep_req]
    )

    assert [candidate.name for candidate in plan.candidates] == [
        "toporequires",
        "toporequires2",
    ]


def _write_simple_project_archive_index(
    index: Path, project: str, archives: list[Path]
) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    links = []
    for archive in archives:
        href = os.path.relpath(archive, project_dir).replace(os.sep, "/")
        links.append(f'<a href="{href}">{archive.name}</a>')
    (project_dir / "index.html").write_text("\n".join(links) + "\n", encoding="utf-8")
