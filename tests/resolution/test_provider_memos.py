"""The provider's identity memos must follow a replaced requirement.

``NabProvider._versions`` and ``NabProvider.prioritize`` keep a fast path
keyed on the identity of the package's current ``Requirement``, in front of
the content-keyed ``_version_cache``.  That is sound only because entries in
``self.requirements`` are replaced rather than mutated as the resolver merges
dependencies -- a memo that outlived a replacement would keep serving the
version list of a wider requirement.

The invalidation is pinned directly, because the end-to-end differential
below cannot see it: a stale version count only misorders decisions, and on
graphs this small the resolver reaches the same solution either way.  The
differential still earns its place as the check that the memos do not perturb
a real resolution; it just is not what catches a broken key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cpip.core.packaging import Version, parse_requirement
from cpip.index.provider import CandidateProvider
from cpip.resolution.models import ResolutionConfig
from cpip.resolution.nab_provider import NabProvider

from .test_forward_check import build_random_graph, make_wrong_package_graph, resolve

VERSIONS = ("1.0.0", "1.1.0", "2.0.0")


def make_provider(wheelhouse: Path) -> NabProvider:
    return NabProvider(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        context=ResolutionConfig(find_links=(str(wheelhouse),), ignore_installed=True),
    )


def wheelhouse_with_demo(tmp_path: Path) -> Path:
    from benchmark_support import make_wheel, reset_caches

    reset_caches()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in VERSIONS:
        make_wheel(wheelhouse, "demo", version)
    return wheelhouse


def test_version_memo_recomputes_for_a_replaced_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new ``Requirement`` object must not be served the old answer.

    What ``_versions`` returns for a given requirement is the package's
    catalog, which the specifier does not narrow -- the URL and extras
    branches are what make the answer requirement-dependent.  So the property
    worth pinning is the key discipline itself: replacing the requirement has
    to reach ``_versions_uncached`` again.
    """
    provider = make_provider(wheelhouse_with_demo(tmp_path))
    provider.requirements["demo"] = parse_requirement("demo")
    assert len(provider._versions("demo")) == len(VERSIONS)

    computed = 0
    sentinel = (Version("9.9.9"),)

    def counting_uncached(package: str, requirement: object) -> tuple:
        nonlocal computed
        computed += 1
        return sentinel

    monkeypatch.setattr(provider, "_versions_uncached", counting_uncached)

    # Same object: the memo answers.
    assert len(provider._versions("demo")) == len(VERSIONS)
    assert computed == 0

    provider.requirements["demo"] = parse_requirement("demo ; python_version >= '3'")
    assert provider._versions("demo") == sentinel
    assert computed == 1


def test_priority_memo_recomputes_for_a_replaced_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision-scan priority must move with the version count."""
    provider = make_provider(wheelhouse_with_demo(tmp_path))
    provider.requirements["demo"] = parse_requirement("demo")
    assert provider.prioritize("demo", None, {})[0] == len(VERSIONS)

    monkeypatch.setattr(
        provider,
        "_versions_uncached",
        lambda package, requirement: (Version("1.0.0"),),
    )
    provider.requirements["demo"] = parse_requirement("demo ; python_version >= '3'")
    assert provider.prioritize("demo", None, {})[0] == 1


def test_priority_memo_recomputes_for_a_rising_conflict_count(tmp_path: Path) -> None:
    """A conflict must re-rank the package it was blamed on."""
    provider = make_provider(wheelhouse_with_demo(tmp_path))
    provider.requirements["demo"] = parse_requirement("demo")

    assert provider.prioritize("demo", None, {})[1] == 0
    assert provider.prioritize("demo", None, {"demo": 3})[1] == -3


def bypass_memos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the pre-memo behaviour of both methods."""

    def versions(self: NabProvider, package: str) -> tuple:
        return self._versions_uncached(package, self.requirements[package])

    def prioritize(
        self: NabProvider,
        package: str,
        version_range: object,
        conflict_counts: dict,
        culprit_counts: dict | None = None,
    ) -> tuple:
        return (
            len(self._versions(package)),
            -conflict_counts.get(package, 0),
            package,
        )

    monkeypatch.setattr(NabProvider, "_versions", versions)
    monkeypatch.setattr(NabProvider, "prioritize", prioritize)


@pytest.mark.parametrize("seed", range(40))
def test_memos_never_change_the_answer(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    memoized = resolve(wheelhouse, roots)

    bypass_memos(monkeypatch)
    plain = resolve(wheelhouse, roots)

    assert (memoized is None) == (plain is None), (
        f"seed {seed}: the memos changed whether the graph is solvable"
    )
    assert memoized == plain, (
        f"seed {seed}: the memos changed the selected versions"
    )


def test_memos_never_change_a_backtracking_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape that re-decides packages, so requirements get replaced."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wrong_package_graph(wheelhouse, "fam", versions=16)

    memoized = resolve(wheelhouse, ["fam-root"])

    bypass_memos(monkeypatch)
    plain = resolve(wheelhouse, ["fam-root"])

    assert memoized == plain
    assert memoized is not None
