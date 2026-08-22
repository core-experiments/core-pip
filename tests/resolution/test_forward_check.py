"""The forward check may reorder work, never change the answer.

``NabProvider._pins_are_impossible`` skips candidate versions whose ``==``
pins already contradict each other, so the resolver never spends a decision
and a conflict discovering it. That is an optimization, which makes its
failure mode quiet: a check that rejects a *satisfiable* version returns an
older solution, or reports a solvable graph as unsolvable, and every existing
assertion still passes.

So the tests here are differential. Each randomized graph is resolved twice --
once normally, once with the check disabled -- and the two must agree on
whether they solved it and on every selected version. Comparing against the
resolver's own behavior is what makes the property checkable at all: there is
no independent oracle for "which version should PubGrub have picked", because
the contract is the locally-newest one its decision order happens to reach.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from cpip.core.errors import ResolutionError
from cpip.core.packaging import parse_requirement
from cpip.core.versions import Version
from cpip.index.provider import CandidateProvider
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.models import ResolutionConfig
from cpip.resolution.nab_provider import NabProvider

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import (  # noqa: E402
    make_dependency_graph,
    make_wheel,
    make_wrong_package_graph,
    reset_caches,
)

VERSIONS = ("1.0.0", "1.1.0", "2.0.0", "2.1.0")


def resolve(wheelhouse: Path, roots: list[str]) -> dict[str, str] | None:
    """Resolve, returning name -> version, or None when unsolvable."""
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    try:
        result = engine.resolve(roots)
    except ResolutionError:
        return None
    return {candidate.name: str(candidate.version) for candidate in result.candidates}


def build_random_graph(wheelhouse: Path, seed: int) -> list[str]:
    """Write a small random wheelhouse; return its root requirements.

    Pins are deliberately over-represented: a graph with no ``==`` never
    reaches the check, so it would prove nothing.
    """
    rng = random.Random(seed)
    names = [f"pkg{index}" for index in range(rng.randint(3, 5))]

    for depth, name in enumerate(names):
        for version in VERSIONS:
            requires = []
            # Only depend on later packages, so the graph stays acyclic and
            # every generated wheelhouse is a fair test rather than a
            # cycle-handling test.
            for other in names[depth + 1 :]:
                if rng.random() > 0.55:
                    continue
                target = rng.choice(VERSIONS)
                form = rng.random()
                if form < 0.55:
                    requires.append(f"{other}=={target}")
                elif form < 0.8:
                    requires.append(f"{other}>={target}")
                else:
                    requires.append(f"{other}<{target}")
            make_wheel(wheelhouse, name, version, requires=requires)

    return [names[0]]


@pytest.mark.parametrize("seed", range(40))
def test_forward_check_never_changes_the_answer(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    with_check = resolve(wheelhouse, roots)

    monkeypatch.setattr(
        NabProvider,
        "_pins_are_impossible",
        lambda self, package, version: False,
    )
    without_check = resolve(wheelhouse, roots)

    assert (with_check is None) == (without_check is None), (
        f"seed {seed}: the forward check changed whether the graph is solvable"
    )
    assert with_check == without_check, (
        f"seed {seed}: the forward check changed the selected versions"
    )


def test_impossible_pins_are_skipped_without_conflicts(tmp_path: Path) -> None:
    """The workload the check exists for: only the oldest root is satisfiable."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wrong_package_graph(wheelhouse, "fam", versions=16)

    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    result = engine.resolve(["fam-root"])

    selected = {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }
    assert selected == {
        "fam-root": "1.1.0",
        "fam-left": "1.1.0",
        "fam-right": "1.1.0",
        "fam-shared": "1.1.0",
    }
    # Reaching that answer by walking every root is what the check prevents;
    # the ceiling is what makes this a regression test rather than a smoke test.
    assert result.metrics["nab_conflicts"] <= 2, result.metrics


def test_unsolvable_graphs_still_fail(tmp_path: Path) -> None:
    """Rejecting every candidate must not turn into a silent wrong answer."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0", requires=["left==1.0.0", "right==1.0.0"])
    make_wheel(wheelhouse, "left", "1.0.0", requires=["shared==1.0.0"])
    make_wheel(wheelhouse, "right", "1.0.0", requires=["shared==2.0.0"])
    make_wheel(wheelhouse, "shared", "1.0.0")
    make_wheel(wheelhouse, "shared", "2.0.0")

    with pytest.raises(ResolutionError) as caught:
        resolve_or_raise(wheelhouse, ["app"])

    # The resolver's own derivation is what explains the failure; the check
    # must not have short-circuited it into something less informative.
    assert str(caught.value)


def resolve_or_raise(wheelhouse: Path, roots: list[str]) -> None:
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    engine.resolve(roots)


def test_verdicts_are_not_reused_across_extras(tmp_path: Path) -> None:
    """Extras gate which dependencies apply, so they must key the memo.

    A verdict reached under narrower extras, reused after they widen, skips a
    version that the wider set may well allow.
    """
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0")

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    adapter.requirements["app"] = parse_requirement("app")

    adapter._pins_are_impossible("app", Version("1.0.0"))
    narrow = dict(adapter._preflight_cache)

    adapter.requirements["app"] = parse_requirement("app[extra]")
    adapter._pins_are_impossible("app", Version("1.0.0"))

    assert len(adapter._preflight_cache) == len(narrow) + 1, (
        "widening extras reused the earlier verdict"
    )


def test_unreadable_metadata_is_undecidable_not_fatal(tmp_path: Path) -> None:
    """A release whose metadata will not load must not fail the resolution."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "app", "1.0.0")

    class Exploding:
        version = Version("1.0.0")

        @property
        def dependencies(self) -> tuple[object, ...]:
            raise OSError("metadata is unreachable")

    provider = CandidateProvider.from_options(
        find_links=[str(wheelhouse)],
        no_index=True,
    )
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))
    adapter.requirements["app"] = parse_requirement("app")
    adapter._catalog_candidate_cache["app"] = {Version("1.0.0"): Exploding()}

    assert adapter._pins_are_impossible("app", Version("1.0.0")) is False


def test_malformed_requires_python_rejects_without_raising() -> None:
    """The index provider treats bad metadata as incompatible; so must this."""

    class BadMetadata:
        requires_python = "not a specifier"

    provider = CandidateProvider.from_options(no_index=True)
    adapter = NabProvider(provider, ResolutionConfig(ignore_installed=True))

    assert adapter._requires_python_rejects(BadMetadata()) is True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("dep==1.2.3", "1.2.3"),
        ("dep == 1.2.3", "1.2.3"),
        ("dep>=1.2.3", None),
        ("dep==1.*", None),
        ("dep===1.2.3", None),
        ("dep>=1.0,<2.0", None),
        ("dep~=1.2", None),
        ("dep", None),
    ],
)
def test_exact_pin_recognizes_only_unique_releases(
    text: str,
    expected: str | None,
) -> None:
    """Anything that is not one concrete release must read as "not a pin"."""
    pinned = parse_requirement(text).specifier.exact_version

    assert (None if pinned is None else str(pinned)) == expected


def test_finite_range_matches_a_union_of_singletons() -> None:
    """``_finite_range`` builds in one pass what unioning built step by step.

    Unioning re-sorted and re-merged the whole interval list on every step,
    which is quadratic in a package's release count. The one-pass form has to
    produce the identical range, including for duplicate and unsorted input.
    """
    from cpip._vendor.nab_resolver.ranges import Range
    from cpip.resolution.nab_provider import NabProvider

    def by_union(versions: list[Version]) -> Range:
        result: Range = Range.empty()
        for version in versions:
            result = result | Range.singleton(version)
        return result

    rng = random.Random(4)
    for _ in range(200):
        versions = [
            Version(f"1.{rng.randint(0, 20)}.{rng.randint(0, 3)}")
            for _ in range(rng.randint(0, 12))
        ]
        if versions and rng.random() < 0.4:
            versions += rng.sample(versions, k=min(3, len(versions)))
        if rng.random() < 0.3:
            versions.sort(reverse=True)

        assert (
            NabProvider._finite_range(versions)._intervals
            == by_union(versions)._intervals
        )


def test_forward_check_queries_releases_by_exact_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check reads one catalog entry per release it inspects.

    Asking the provider for a package by name re-evaluates every link and
    materializes every release -- the work the resolver's own requirement
    already paid for. The resolver's own version lookup queries each package
    by name once (a root twice: once when added, once when decided); the
    check must not add a second name-only query per package on top of it.
    """
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_dependency_graph(wheelhouse)
    queried: list[str] = []
    releases: list[tuple[str, Version]] = []
    original = CandidateProvider.find_candidates
    original_release = CandidateProvider.release_candidates

    def recording(self: CandidateProvider, requirement, **kwargs):  # type: ignore[no-untyped-def]
        queried.append(requirement.specifier.text)
        return original(self, requirement, **kwargs)

    def recording_release(self: CandidateProvider, requirement, version):  # type: ignore[no-untyped-def]
        releases.append((requirement.canonical_name, version))
        return original_release(self, requirement, version)

    monkeypatch.setattr(CandidateProvider, "find_candidates", recording)
    monkeypatch.setattr(CandidateProvider, "release_candidates", recording_release)

    selected = resolve(wheelhouse, ["application"])
    assert selected is not None

    unconstrained = [text for text in queried if not text]
    assert len(unconstrained) <= len(selected) + 1, queried
    assert releases, "the forward check inspected no release"
    assert len(releases) == len(set(releases)), "a release was read twice"
