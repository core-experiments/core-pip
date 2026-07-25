from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pip.cli.main import main
from pip.core.packaging import parse_requirement
from pip.core.wheel import TargetContext
from pip.index.cache import origin_hashes
from pip.index.provider import CandidateProvider
from pip.index.source_models import ArtifactKind, MetadataFile, RejectionReason
from pip.index.vcs import is_immutable_vcs_link, vcs_reference
from wheel_helpers import make_sdist, make_wheel


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
        "0123456789abcdef0123456789abcdef01234567#egg=demo-pkg"
    )
    assert not is_immutable_vcs_link(
        "git+https://example.com/demo/pkg.git@master#egg=demo-pkg"
    )
    assert not is_immutable_vcs_link(
        "git+https://example.com/demo/pkg.git@0123456#egg=demo-pkg"
    )


def test_candidate_provider_reads_pep503_file_index(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    older = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    newer = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0")
    _write_simple_project_index(index, "demo-pkg", [older, newer])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("Demo_Pkg>=1"))

    assert [str(candidate.version) for candidate in candidates] == ["2.0", "1.0"]


def test_candidate_provider_filters_wheels_for_download_target(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    linux = wheelhouse / "demo_pkg-2.0-py3-none-linux_x86_64.whl"
    linux.write_bytes(
        make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "2.0").read_bytes()
    )
    any_wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    any_target = wheelhouse / "demo_pkg-1.0-py3-none-any.whl"
    if any_target != any_wheel:
        any_target.write_bytes(any_wheel.read_bytes())
    _write_simple_project_index(index, "demo-pkg", [linux, any_target])

    provider = CandidateProvider.from_options(
        index_url=index.as_uri(),
        target=TargetContext(platforms=("linux_x86_64",)),
    )
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert [candidate.path.name for candidate in candidates] == [
        "demo_pkg-2.0-py3-none-linux_x86_64.whl",
        "demo_pkg-1.0-py3-none-any.whl",
    ]


def test_origin_hashes_with_invalid_json(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    origin_file = tmp_path / "origin.json"
    origin_file.write_text("{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pip.index.candidate_evaluators"):
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
            ]
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)
    monkeypatch.setattr(
        "pip.index.candidates.prepare_project_metadata",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
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
    tmp_path: Path, anchor_html: str, expected: str | None
) -> None:
    _write_simple_project_html(tmp_path / "simple", "pkg", anchor_html)

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
    tmp_path: Path, anchor_html: str, expected: str | None
) -> None:
    _write_simple_project_html(tmp_path / "simple", "pkg", anchor_html)

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
    _write_simple_project_html(index, "pkg1", anchor)

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
    _write_simple_project_index(second_index, "complex-name", [wheel])

    provider = CandidateProvider.from_options(
        index_url=first_index.as_uri(),
        extra_index_urls=[second_index.as_uri()],
    )
    candidates = provider.find_candidates(parse_requirement("Complex_Name"))

    assert [candidate.path for candidate in candidates] == [wheel]


def test_candidate_provider_reads_direct_file_url(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "packages"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")

    provider = CandidateProvider.from_options(no_index=True)
    candidates = provider.find_candidates(
        parse_requirement(f"demo-pkg @ {wheel.as_uri()}")
    )

    assert [candidate.path for candidate in candidates] == [wheel]


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
            ]
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)
    selection = provider.evaluate_links(parse_requirement(str(project)))
    candidates = provider.find_candidates(parse_requirement(str(project)))

    assert [candidate.link.kind for candidate in selection.accepted] == [
        ArtifactKind.SOURCE_TREE
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
            ]
        ),
        encoding="utf-8",
    )

    provider = CandidateProvider.from_options(no_index=True)

    selection = provider.evaluate_links(parse_requirement(str(project)))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.INVALID_VERSION
    ]


def test_evaluate_links_rejects_incompatible_requires_python(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    _write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py3-none-any.whl" data-requires-python=">=99"></a>',
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    selection = provider.evaluate_links(parse_requirement("demo-pkg"))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.REQUIRES_PYTHON
    ]


def test_evaluate_links_rejects_unsupported_wheel_tags(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    _write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py1-none-any.whl"></a>',
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    selection = provider.evaluate_links(parse_requirement("demo-pkg"))

    assert selection.accepted == ()
    assert [rejected.reason for rejected in selection.rejected] == [
        RejectionReason.UNSUPPORTED_WHEEL
    ]


def test_evaluate_links_yanked_policy_oracle(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    _write_simple_project_html(
        index,
        "demo-pkg",
        '<a href="demo_pkg-1.0-py3-none-any.whl" data-yanked="bad release"></a>',
    )
    provider = CandidateProvider.from_options(index_url=index.as_uri())

    unpinned = provider.evaluate_links(parse_requirement("demo-pkg"))
    pinned = provider.evaluate_links(parse_requirement("demo-pkg==1.0"))

    assert [rejected.reason for rejected in unpinned.rejected] == [
        RejectionReason.YANKED
    ]
    assert [str(candidate.version) for candidate in pinned.accepted] == ["1.0"]


def test_evaluate_links_collects_all_artifact_kinds(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    _write_simple_project_html(
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
            ]
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
        RejectionReason.UNSUPPORTED_ARTIFACT
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
    _write_simple_project_archive_index(index, "source-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("source-pkg"))

    assert [candidate.name for candidate in candidates] == ["source-pkg"]
    assert [str(candidate.version) for candidate in candidates] == ["1.0"]
    assert candidates[0].path.name == "source_pkg-1.0-py3-none-any.whl"
    assert [dependency.raw for dependency in candidates[0].dependencies] == [
        "dep-pkg>=1"
    ]


def test_candidate_provider_defers_sdist_build_when_matching_wheel_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    sdist = make_sdist(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [wheel, sdist])

    def fail_build(*_args, **_kwargs):
        raise AssertionError("sdist build should be skipped when a wheel exists")

    monkeypatch.setattr("pip.build.build.build_wheel_from_source", fail_build)

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert candidates[0].path.name == wheel.name


def test_candidate_provider_only_builds_highest_ranked_source_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    newest = make_sdist(packages, "demo-pkg", "demo_pkg", "3.0")
    older = make_sdist(packages, "demo-pkg", "demo_pkg", "2.0")
    wheel = make_wheel(packages, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_archive_index(index, "demo-pkg", [newest, older, wheel])

    built: list[str] = []
    real_build = __import__(
        "pip.build.build", fromlist=["build_wheel_from_source"]
    ).build_wheel_from_source

    def tracking_build(path, *args, **kwargs):
        built.append(Path(path).name)
        return real_build(path, *args, **kwargs)

    monkeypatch.setattr(
        "pip.build.build.build_wheel_from_source",
        tracking_build,
    )

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("demo-pkg"))

    assert built == []
    preferred = candidates[:2]

    assert built == [newest.name]
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
        backend=True,
    )
    _write_simple_project_archive_index(index, "backend-pkg", [sdist])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("backend-pkg"))

    assert [candidate.path.name for candidate in candidates] == [
        "backend_pkg-1.0-py3-none-any.whl"
    ]


def test_candidate_provider_prefers_wheel_over_matching_sdist(tmp_path: Path) -> None:
    index = tmp_path / "simple"
    packages = tmp_path / "packages"
    packages.mkdir()
    wheel = make_wheel(packages, "priority-pkg", "priority_pkg", "1.0")
    sdist = make_sdist(packages, "priority-pkg", "priority_pkg", "1.0")
    _write_simple_project_archive_index(index, "priority-pkg", [sdist, wheel])

    provider = CandidateProvider.from_options(index_url=index.as_uri())
    candidates = provider.find_candidates(parse_requirement("priority-pkg"))

    assert candidates[0].path == wheel


def test_core_download_uses_index_and_extra_index_url(tmp_path: Path, capsys) -> None:
    primary_index = tmp_path / "primary"
    secondary_index = tmp_path / "secondary"
    wheelhouse = tmp_path / "packages"
    dest = tmp_path / "dest"
    primary_index.mkdir()
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo-pkg", "demo_pkg", "1.0")
    _write_simple_project_index(secondary_index, "demo-pkg", [wheel])

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
        ]
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
    _write_simple_project_index(index, "demo-pkg", [wheel])

    status = main(
        [
            "download",
            "--no-index",
            "--index-url",
            index.as_uri(),
            "--dest",
            str(dest),
            "demo-pkg",
        ]
    )
    captured = capsys.readouterr()

    assert status == 1
    assert not (dest / wheel.name).exists()
    assert "No matching distribution found for demo-pkg" in captured.err


def _write_simple_project_index(index: Path, project: str, wheels: list[Path]) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    links = []
    for wheel in wheels:
        href = os.path.relpath(wheel, project_dir).replace(os.sep, "/")
        links.append(f'<a href="{href}#sha256=test">{wheel.name}</a>')
    (project_dir / "index.html").write_text("\n".join(links) + "\n", encoding="utf-8")


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


def _write_simple_project_html(index: Path, project: str, html: str) -> None:
    project_dir = index / project
    project_dir.mkdir(parents=True)
    project_html = f"<!DOCTYPE html><html><body>{html}</body></html>"
    (project_dir / "index.html").write_text(project_html, encoding="utf-8")
