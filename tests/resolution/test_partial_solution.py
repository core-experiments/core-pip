"""Backtracking must restore exactly the state a full replay would produce.

``PartialSolution`` maintains ``_positive_ranges``, ``_negative_ranges``,
``_decided_versions`` and ``_undecided`` incrementally, and ``backtrack``
rebuilds them from the ``cum_*`` snapshot carried by the surviving top entry
of each package's trail.  That is a local patch to a vendored package (see
``src/cpip/_vendor/VENDORED.md``) replacing a rescan of the whole trail, so
these tests pin the equivalence rather than the implementation: they compare
the incremental state against a replay of the surviving assignments after
randomized decide/derive/backtrack sequences.
"""

from __future__ import annotations

import random
from typing import Any, cast

import pytest
from cpip._vendor.nab_resolver.partial_solution import (
    Assignment,
    PartialSolution,
)
from cpip._vendor.nab_resolver.ranges import Range
from cpip._vendor.nab_resolver.types import Incompatibility, IncompatibilityCause

PACKAGES = ("alpha", "beta", "gamma", "delta")
VERSIONS = tuple(range(1, 9))

CAUSE: Incompatibility[str, int] = Incompatibility(
    [],
    IncompatibilityCause.DEPENDENCY,
)

State = dict[str, tuple[object, object, object, bool]]


def replay(solution: PartialSolution[str, int]) -> State:
    """Rebuild every package's state from its surviving trail.

    The definition the ``cum_*`` snapshots replaced: walk the package's
    assignments in order and keep the latest of each kind.
    """
    internals = cast("Any", solution)
    expected: State = {}
    for package, entries in internals._assignments_by_package.items():
        if not entries:
            continue
        positive: object = None
        negative: object = None
        decided: object = None
        for assignment in entries:
            if assignment.is_decision:
                positive = assignment.accumulated_range
                decided = assignment.version
            elif assignment.positive:
                positive = assignment.accumulated_range
            else:
                negative = assignment.accumulated_range
        expected[package] = (
            positive,
            negative,
            decided,
            positive is not None and decided is None,
        )
    return expected


def observed(solution: PartialSolution[str, int]) -> State:
    """Read the same state back out of the incrementally maintained maps."""
    internals = cast("Any", solution)
    packages = (
        set(internals._positive_ranges)
        | set(internals._negative_ranges)
        | set(internals._decided_versions)
        | set(internals._undecided)
    )
    return {
        package: (
            internals._positive_ranges.get(package),
            internals._negative_ranges.get(package),
            internals._decided_versions.get(package),
            package in internals._undecided,
        )
        for package in packages
    }


def assert_consistent(solution: PartialSolution[str, int]) -> None:
    assert observed(solution) == replay(solution)

    internals = cast("Any", solution)
    # The per-package index must stay a partition of the chronological trail.
    indexed = [
        assignment
        for entries in internals._assignments_by_package.values()
        for assignment in entries
    ]
    assert sorted(id(entry) for entry in indexed) == sorted(
        id(entry) for entry in internals._assignments
    )
    for assignment in internals._assignments:
        assert assignment.decision_level <= solution.decision_level


def drive(seed: int, steps: int = 200) -> None:
    """Run a random decide/derive/backtrack sequence, checking as it goes."""
    rng = random.Random(seed)
    solution: PartialSolution[str, int] = PartialSolution()

    for _ in range(steps):
        package = rng.choice(PACKAGES)
        action = rng.random()

        if action < 0.35:
            undecided = solution.undecided_packages()
            target = rng.choice(sorted(undecided)) if undecided else package
            if target not in solution.decisions():
                solution.decide(target, rng.choice(VERSIONS))
        elif action < 0.85:
            version = rng.choice(VERSIONS)
            positive = rng.random() < 0.5
            constraint = (
                Range.at_least(version) if rng.random() < 0.5 else Range.less_than(version)
            )
            solution.derive(package, constraint, positive=positive, cause=CAUSE)
        elif solution.decision_level > 0:
            solution.backtrack(rng.randrange(solution.decision_level))

        assert_consistent(solution)


@pytest.mark.parametrize("seed", range(24))
def test_backtrack_matches_a_full_replay(seed: int) -> None:
    drive(seed)


def test_backtrack_restores_an_earlier_decision() -> None:
    """A decision below the target level survives, along with its version.

    The case the snapshot has to get right: ``alpha`` keeps a decision made
    before the target level even though a later derivation of its own is
    popped.  Losing it would leave ``alpha`` undecided and let the resolver
    re-decide a version it had already committed to.
    """
    solution: PartialSolution[str, int] = PartialSolution()

    solution.decide("alpha", 3)
    solution.derive("alpha", Range.at_least(1), positive=True, cause=CAUSE)
    level_after_alpha = solution.decision_level
    solution.decide("beta", 5)
    solution.derive("alpha", Range.less_than(9), positive=True, cause=CAUSE)

    solution.backtrack(level_after_alpha)

    assert solution.decisions() == {"alpha": 3}
    assert "alpha" not in solution.undecided_packages()
    assert_consistent(solution)


def test_backtrack_to_zero_clears_every_package() -> None:
    solution: PartialSolution[str, int] = PartialSolution()

    solution.decide("alpha", 3)
    solution.derive("beta", Range.at_least(2), positive=True, cause=CAUSE)
    solution.decide("beta", 4)
    solution.derive("gamma", Range.less_than(7), positive=False, cause=CAUSE)

    solution.backtrack(0)

    assert solution.decisions() == {}
    assert solution.undecided_packages() == set()
    assert solution.trail_length == 0
    assert_consistent(solution)


def test_decision_snapshot_tracks_later_derivations() -> None:
    """Derivations recorded after a decision carry that decision forward."""
    solution: PartialSolution[str, int] = PartialSolution()

    solution.decide("alpha", 3)
    solution.derive("alpha", Range.less_than(9), positive=True, cause=CAUSE)

    entries = solution.assignments_for("alpha")
    assert [cast("Assignment[str, int]", entry).cum_decision for entry in entries] == [
        3,
        3,
    ]
