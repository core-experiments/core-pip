"""``CandidateProvider.release_candidates`` answers exactly what the full
query answers for a ``==`` pin on that release, for every release."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from cpip.core.packaging import parse_requirement
from cpip.index.provider import CandidateProvider

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import (  # noqa: E402
    make_dependency_graph,
    make_wheel,
    make_wrong_package_graph,
    reset_caches,
)


def _provider(wheelhouse: Path) -> CandidateProvider:
    reset_caches()
    return CandidateProvider.from_options(find_links=[str(wheelhouse)], no_index=True)


def _assert_matches_full_query(provider: CandidateProvider, name: str) -> int:
    requirement = parse_requirement(name)
    checked = 0
    for summary in provider.available_versions(requirement):
        version = summary.version
        pinned = parse_requirement(f"{name}=={version}")
        expected = tuple(
            record
            for record in provider.applicable_candidate_records(pinned)
            if record.version == version
        )
        assert provider.release_candidates(requirement, version) == expected, (
            name,
            version,
        )
        checked += 1
    return checked


@pytest.mark.parametrize("build", [make_dependency_graph, None])
def test_release_candidates_match_the_full_query(
    tmp_path: Path,
    build: object,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    if build is None:
        make_wrong_package_graph(wheelhouse, "fam", versions=12)
        names = ["fam-root", "fam-left", "fam-right", "fam-shared"]
    else:
        make_dependency_graph(wheelhouse)
        names = ["application", "middle-0", "leaf-0", "leaf-19"]
    provider = _provider(wheelhouse)

    assert sum(_assert_matches_full_query(provider, name) for name in names) > 10


def test_release_candidates_apply_the_prerelease_policy(tmp_path: Path) -> None:
    """A pre-release is offered only the way the full query offers it."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo", "1.0.0")
    make_wheel(wheelhouse, "demo", "2.0.0rc1")
    provider = _provider(wheelhouse)

    assert _assert_matches_full_query(provider, "demo") == 2


def test_release_candidates_decline_without_a_catalog(tmp_path: Path) -> None:
    """A direct URL has no package catalog; the caller must use the full query."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo", "1.0.0")
    provider = _provider(wheelhouse)
    requirement = parse_requirement(f"demo @ {wheel.as_uri()}")

    assert (
        provider.release_candidates(
            requirement, parse_requirement("demo==1.0.0").specifier.exact_version
        )
        is None
    )


def test_release_candidates_collapse_equivalent_artifacts(tmp_path: Path) -> None:
    """A wheel reachable through two find-links entries is one record, the
    way the full query returns it -- not an ambiguous release."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    wheel = make_wheel(first, "demo", "1.0.0")
    shutil.copy2(wheel, second / wheel.name)
    reset_caches()
    provider = CandidateProvider.from_options(
        find_links=[str(first), str(second)],
        no_index=True,
    )

    assert _assert_matches_full_query(provider, "demo") == 1
    records = provider.release_candidates(
        parse_requirement("demo"),
        parse_requirement("demo==1.0.0").specifier.exact_version,
    )
    assert records is not None
    assert len(records) == 1


def test_release_candidates_keep_distinct_builds_in_preferred_order(
    tmp_path: Path,
) -> None:
    """Two different compatible builds of one release stay two records,
    ordered as the full query orders them."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = make_wheel(wheelhouse, "demo", "1.0.0")
    shutil.copy2(wheel, wheelhouse / "demo-1.0.0-1-py3-none-any.whl")
    provider = _provider(wheelhouse)

    assert _assert_matches_full_query(provider, "demo") == 1
    records = provider.release_candidates(
        parse_requirement("demo"),
        parse_requirement("demo==1.0.0").specifier.exact_version,
    )
    assert records is not None
    assert len(records) == 2
