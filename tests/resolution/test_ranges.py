"""``Range`` set predicates must agree with the set algebra they replace.

``is_subset`` and ``is_disjoint`` are hot enough during resolution that they
walk the interval lists directly instead of building a difference or an
intersection and asking whether it is empty.  That is a local patch to a
vendored package (see ``src/cpip/_vendor/VENDORED.md``), so these tests pin
the equivalence rather than the implementation: they compare each predicate
both against its original set-algebra definition and against an independent
membership oracle over sampled points.
"""

from __future__ import annotations

import random

import pytest
from cpip._vendor.nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)

# Sample points, including half steps so an exclusive bound is distinguishable
# from an inclusive one.
PROBES = [value * 0.5 for value in range(-2, 20)]


def subset_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left - right).is_empty


def disjoint_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left & right).is_empty


def contains_by_linear_scan(candidate: Range, version: object) -> bool:
    """The scan the binary search in ``__contains__`` replaced."""

    for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals:
        if lower is not NEGATIVE_INFINITY and (
            version < lower or (version == lower and not lower_inclusive)
        ):
            continue
        if upper is not POSITIVE_INFINITY and (
            version > upper or (version == upper and not upper_inclusive)
        ):
            continue
        return True
    return False


def members(candidate: Range) -> set[float]:
    return {probe for probe in PROBES if contains_by_linear_scan(candidate, probe)}


def build(intervals: list[tuple]) -> Range:
    """Normalize intervals the way the resolver does, through union."""

    result: Range = Range.empty()
    for interval in intervals:
        result = result | Range((interval,))
    return result


def random_range(rng: random.Random) -> Range:
    intervals = []
    for _ in range(rng.randint(0, 3)):
        lower, upper = sorted(rng.sample(range(9), 2))
        kind = rng.randint(0, 3)
        if kind == 0:
            intervals.append((lower, True, upper, True))
        elif kind == 1:
            intervals.append((lower, True, upper, False))
        elif kind == 2:
            intervals.append(
                (NEGATIVE_INFINITY, False, upper, rng.choice([True, False])),
            )
        else:
            intervals.append(
                (lower, rng.choice([True, False]), POSITIVE_INFINITY, False),
            )
    return build(intervals)


@pytest.mark.parametrize("seed", range(12))
def test_contains_matches_a_linear_scan(seed: int) -> None:
    rng = random.Random(seed)

    for _ in range(200):
        candidate = random_range(rng)
        for probe in PROBES:
            assert (probe in candidate) == contains_by_linear_scan(candidate, probe)


@pytest.mark.parametrize("seed", range(12))
def test_predicates_match_set_algebra_and_membership(seed: int) -> None:
    rng = random.Random(seed)

    for _ in range(400):
        left, right = random_range(rng), random_range(rng)
        left_members, right_members = members(left), members(right)

        assert left.is_subset(right) == subset_by_set_algebra(left, right)
        assert left.is_disjoint(right) == disjoint_by_set_algebra(left, right)
        assert left.is_disjoint(right) == (not left_members & right_members)

        relation = left.relation(right)
        assert relation.is_subset == left.is_subset(right)
        assert relation.is_disjoint == left.is_disjoint(right)


def test_empty_range_is_subset_and_disjoint() -> None:
    empty: Range = Range.empty()
    other = Range.between(1, 5)

    assert empty.is_subset(other)
    assert empty.is_disjoint(other)

    relation = empty.relation(other)
    assert relation.is_subset
    assert relation.is_disjoint


def test_nonempty_against_empty_is_disjoint_but_not_subset() -> None:
    populated = Range.between(1, 5)
    empty: Range = Range.empty()

    assert not populated.is_subset(empty)
    assert populated.is_disjoint(empty)


def test_touching_exclusive_bounds_do_not_overlap() -> None:
    lower = Range.less_than(3)
    upper = Range.at_least(3)

    assert lower.is_disjoint(upper)
    assert not lower.is_subset(upper)


def test_subset_requires_a_single_covering_interval() -> None:
    """A gap in the covering range makes the span a non-subset."""

    span = Range.between(1, 5)
    gapped = Range.between(1, 2) | Range.between(3, 5)

    assert not span.is_subset(gapped)
    assert Range.between(3, 5).is_subset(gapped)


def test_full_range_contains_everything() -> None:
    full: Range = Range.full()

    assert Range.between(1, 5).is_subset(full)
    assert not Range.between(1, 5).is_disjoint(full)
    assert full.is_subset(full)


def test_hash_is_stable_and_matches_equality() -> None:
    left = Range.between(1, 5) | Range.at_least(9)
    right = Range.between(1, 5) | Range.at_least(9)

    assert left == right
    assert hash(left) == hash(right)
    assert hash(left) == hash(left)


@pytest.mark.parametrize("seed", range(12))
def test_difference_matches_complement_and_intersect(seed: int) -> None:
    """``a - b`` carves intervals directly; it must equal ``a & ~b``."""

    rng = random.Random(seed + 100)

    for _ in range(300):
        left, right = random_range(rng), random_range(rng)

        assert (left - right)._intervals == (left & ~right)._intervals
        assert members(left - right) == members(left) - members(right)


class Strict:
    """An integer-like bound that refuses to compare with anything else.

    ``Version`` compares only with ``Version``; a range operation that lets
    an infinity sentinel reach a bound comparison would raise here instead
    of quietly succeeding through the sentinel's reflected methods.
    """

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    def _other(self, other: object) -> int:
        assert isinstance(other, Strict), f"bound compared with {other!r}"
        return other.value

    def __eq__(self, other: object) -> bool:
        return self.value == self._other(other)

    def __hash__(self) -> int:
        return hash(self.value)

    def __lt__(self, other: object) -> bool:
        return self.value < self._other(other)

    def __le__(self, other: object) -> bool:
        return self.value <= self._other(other)

    def __gt__(self, other: object) -> bool:
        return self.value > self._other(other)

    def __ge__(self, other: object) -> bool:
        return self.value >= self._other(other)

    def __repr__(self) -> str:
        return f"Strict({self.value})"


def strict(candidate: Range) -> Range:
    return Range(
        tuple(
            (
                lower if lower is NEGATIVE_INFINITY else Strict(lower),
                lower_inclusive,
                upper if upper is POSITIVE_INFINITY else Strict(upper),
                upper_inclusive,
            )
            for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals
        ),
    )


def plain(candidate: Range) -> Range:
    return Range(
        tuple(
            (
                lower if lower is NEGATIVE_INFINITY else lower.value,
                lower_inclusive,
                upper if upper is POSITIVE_INFINITY else upper.value,
                upper_inclusive,
            )
            for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals
        ),
    )


@pytest.mark.parametrize("seed", range(8))
def test_bounds_are_never_compared_with_a_sentinel(seed: int) -> None:
    """The interval algebra over bounds that refuse foreign operands gives
    the same answers as over plain integers."""
    rng = random.Random(seed)

    for _ in range(300):
        left, right = random_range(rng), random_range(rng)
        strict_left, strict_right = strict(left), strict(right)

        assert plain(strict_left & strict_right) == (left & right)
        assert plain(strict_left | strict_right) == (left | right)
        assert plain(strict_left - strict_right) == (left - right)
        assert plain(~strict_left) == ~left
        assert strict_left.is_subset(strict_right) == left.is_subset(right)
        assert strict_left.is_disjoint(strict_right) == left.is_disjoint(right)
        assert strict_left.relation(strict_right) == left.relation(right)
        for probe in range(-1, 10):
            assert (Strict(probe) in strict_left) == (probe in left)
