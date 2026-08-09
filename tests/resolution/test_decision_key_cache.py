"""Cached sort keys must not change which package is decided next.

``choose_package_to_decide`` reuses each package's sort key between decision
scans and rebuilds only the ones whose inputs were reported to have moved --
the package's range, its conflict and culprit counts, or provider state.
That is a local patch to a vendored package (see
``src/cpip/_vendor/VENDORED.md``).

Its failure mode is quiet and expensive rather than loud.  A key that
outlives an input still sorts, so the resolver simply decides a different
package, and decision order is what determines how much backtracking a
resolution does at all: caching keys with no invalidation whatsoever still
produces a correct answer for every graph here, while taking 47% longer on
the backtracking workload.  Comparing final versions would not see that.

So these tests compare the *decision sequence* against the uncached path,
package by package, which is the only thing that pins the equivalence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from cpip._vendor.nab_resolver import decide as decide_module
from cpip.core.errors import ResolutionError
from cpip.index.provider import CandidateProvider
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.nab_provider import NabProvider

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import (  # noqa: E402
    make_backtracking_graph,
    make_stress_graph,
    make_wrong_package_graph,
    reset_caches,
)

from .test_forward_check import build_random_graph  # noqa: E402


def resolve_recording_decisions(
    wheelhouse: Path,
    roots: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    uncached: bool,
) -> tuple[list[Any], dict[str, str] | None]:
    """Resolve, returning every package chosen to decide, in order."""
    reset_caches()
    chosen: list[Any] = []
    original = decide_module.choose_package_to_decide

    def recording(resolver: Any) -> Any:
        package = original(resolver)
        chosen.append(package)
        return package

    monkeypatch.setattr(decide_module, "choose_package_to_decide", recording)
    if uncached:
        # What a provider that cannot report its own invalidations gets:
        # every key rebuilt on every scan.
        monkeypatch.setattr(
            NabProvider,
            "consume_priority_invalidations",
            lambda self: None,
        )

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
        return chosen, None
    return chosen, {
        candidate.name: str(candidate.version) for candidate in result.candidates
    }


def assert_same_decisions(
    wheelhouse: Path,
    roots: list[str],
    monkeypatch: pytest.MonkeyPatch,
    label: str = "",
) -> None:
    with monkeypatch.context() as cached_patch:
        cached, cached_result = resolve_recording_decisions(
            wheelhouse, roots, cached_patch, uncached=False
        )
    with monkeypatch.context() as plain_patch:
        plain, plain_result = resolve_recording_decisions(
            wheelhouse, roots, plain_patch, uncached=True
        )

    assert cached_result == plain_result, f"{label}: different resolution"
    assert cached == plain, (
        f"{label}: the key cache changed the decision order "
        f"({len(cached)} vs {len(plain)} decisions)"
    )


@pytest.mark.parametrize("seed", range(30))
def test_key_cache_preserves_decision_order(
    seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)
    assert_same_decisions(wheelhouse, roots, monkeypatch, f"seed {seed}")


def test_key_cache_preserves_decision_order_under_backtracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph a stale key actually derails.

    Caching keys with no invalidation resolves this to the same versions
    while taking 47% longer, because it keeps choosing packages whose
    conflict counts it can no longer see. That makes this the case that
    tells a correct invalidation from a plausible one.
    """
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_backtracking_graph(wheelhouse)
    assert_same_decisions(wheelhouse, ["conflicting"], monkeypatch, "backtracking")


def test_key_cache_preserves_decision_order_while_backtracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape where a stale key costs the most: conflict counts moving."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wrong_package_graph(wheelhouse, "fam", versions=24)
    assert_same_decisions(wheelhouse, ["fam-root"], monkeypatch, "wrong-package")


def test_key_cache_preserves_decision_order_over_many_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape the cache exists for: a wide undecided set."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_stress_graph(wheelhouse, roots=40)
    roots = [f"stress-{index}" for index in range(40)]
    assert_same_decisions(wheelhouse, roots, monkeypatch, "stress")


def test_key_cache_is_cleared_between_resolutions(tmp_path: Path) -> None:
    """A reused engine must not answer from the previous resolution's keys."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_stress_graph(wheelhouse, roots=4)

    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    first = engine.resolve(["stress-0"]).candidates
    second = engine.resolve(["stress-0"]).candidates

    assert [(c.name, str(c.version)) for c in first] == [
        (c.name, str(c.version)) for c in second
    ]


# --- Each invalidation source, in isolation -------------------------------
#
# End to end the sources overlap: a conflict backjumps, and the backjump
# touches the same packages whose counts moved, so disabling any single
# source above still reproduces the reference decision order.  Only
# disabling all of them at once is visible there.  These drive the scan
# directly instead, over a provider whose priority is whatever the test
# says it is, so each source can be moved on its own.


class StubProvider:
    """A provider whose sort key the test controls outright."""

    def __init__(self) -> None:
        from cpip._vendor.nab_resolver.ranges import Range

        self.priorities: dict[str, int] = {}
        self.invalidations: set[str] = set()
        self.use_range = False
        self.full_range = Range.full()

    def begin_decision_scan(self) -> None:
        return None

    def prioritize(
        self,
        package: str,
        version_range: Any,
        conflict_counts: Any,
        culprit_counts: Any = None,
    ) -> tuple[int, ...]:
        if self.use_range:
            # An untouched package sorts first, so a derivation moves the key.
            base = 0 if version_range == self.full_range else 1
        else:
            base = self.priorities.get(package, 0)
        culprits = 0 if culprit_counts is None else culprit_counts.get(package, 0)
        return (base, -conflict_counts.get(package, 0), -culprits)

    def is_ready(self, package: str) -> bool:
        return True

    def consume_priority_invalidations(self) -> list[str]:
        reported = list(self.invalidations)
        self.invalidations.clear()
        return reported


def scan_resolver() -> tuple[Any, StubProvider]:
    """A resolver with two undecided packages and nothing decided."""
    from cpip._vendor.nab_resolver.ranges import Range
    from cpip._vendor.nab_resolver.resolver import Resolver
    from cpip._vendor.nab_resolver.types import (
        Incompatibility,
        IncompatibilityCause,
    )

    stub = StubProvider()
    resolver: Any = Resolver(provider=stub)
    cause: Incompatibility[Any, Any] = Incompatibility(
        [], IncompatibilityCause.DEPENDENCY
    )
    for package in ("alpha", "beta"):
        resolver.solution.derive(
            package, Range.full(), positive=True, cause=cause
        )
    return resolver, stub


def choose(resolver: Any) -> Any:
    return decide_module.choose_package_to_decide(resolver)


def test_a_moved_range_rebuilds_the_key() -> None:
    from cpip._vendor.nab_resolver.ranges import Range
    from cpip._vendor.nab_resolver.types import (
        Incompatibility,
        IncompatibilityCause,
    )

    resolver, stub = scan_resolver()
    stub.use_range = True
    assert choose(resolver) == "alpha"

    # Narrow alpha so beta's wider range now sorts first.
    cause: Incompatibility[Any, Any] = Incompatibility(
        [], IncompatibilityCause.DEPENDENCY
    )
    resolver.solution.derive(
        "alpha", Range.at_least(5), positive=False, cause=cause
    )
    assert choose(resolver) == "beta"


def test_a_moved_conflict_count_rebuilds_the_key() -> None:
    resolver, stub = scan_resolver()
    assert choose(resolver) == "alpha"

    resolver.stats.package_conflict_counts["beta"] += 1
    assert choose(resolver) == "beta"


def test_a_moved_culprit_count_rebuilds_the_key() -> None:
    resolver, stub = scan_resolver()
    assert choose(resolver) == "alpha"

    resolver.stats.package_culprit_counts["beta"] += 1
    assert choose(resolver) == "beta"


def test_a_provider_reported_change_rebuilds_the_key() -> None:
    resolver, stub = scan_resolver()
    assert choose(resolver) == "alpha"

    # Provider-internal state the resolver cannot see on its own.
    stub.priorities["beta"] = -1
    assert choose(resolver) == "alpha", "unreported change must not be picked up"

    stub.invalidations.add("beta")
    assert choose(resolver) == "beta"


def test_a_provider_that_cannot_report_rebuilds_every_key() -> None:
    resolver, stub = scan_resolver()
    stub.consume_priority_invalidations = lambda: None  # type: ignore[assignment]
    assert choose(resolver) == "alpha"

    stub.priorities["beta"] = -1
    assert choose(resolver) == "beta"


def test_a_provider_without_the_method_rebuilds_every_key() -> None:
    """A third-party provider predating the hook still resolves correctly."""
    resolver, stub = scan_resolver()
    del StubProvider.consume_priority_invalidations
    try:
        assert choose(resolver) == "alpha"
        stub.priorities["beta"] = -1
        assert choose(resolver) == "beta"
    finally:
        StubProvider.consume_priority_invalidations = (
            _ORIGINAL_CONSUME  # type: ignore[assignment]
        )


_ORIGINAL_CONSUME = StubProvider.consume_priority_invalidations


def test_a_restart_drops_every_key() -> None:
    from cpip._vendor.nab_resolver import conflict as conflict_module

    resolver, stub = scan_resolver()
    assert choose(resolver) == "alpha"
    assert resolver.priority_keys

    resolver.stats.package_conflict_counts["alpha"] = 99
    threshold, remaining, restarted = conflict_module.maybe_restart(resolver, 1, 1)

    assert restarted
    assert not resolver.priority_keys
