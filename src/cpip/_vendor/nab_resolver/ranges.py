"""Generic version range with interval operations.

A Range represents a set of versions as a sorted list of non-overlapping
intervals. Supports intersection, union, complement, and containment.

This is equivalent to pubgrub-rs's ``version_ranges::Ranges<V>``:
https://github.com/pubgrub-rs/pubgrub/tree/release/version-ranges

The type parameter V can be any ordered, hashable type. The simple test
provider uses int; the Python provider uses packaging.version.Version.
"""

from __future__ import annotations

from typing import Any, Generic, TypeAlias

try:
    from typing import override
except ImportError:  # pragma: no cover - Python < 3.12
    from cpip._vendor.typing_extensions import override

from .types import RangeRelation, VersionType

# Bound once so the hot return paths load a module global instead of a class
# attribute.
_EMPTY_REL = RangeRelation.EMPTY
_SUBSET_REL = RangeRelation.SUBSET
_DISJOINT_REL = RangeRelation.DISJOINT
_OVERLAPPING_REL = RangeRelation.OVERLAPPING

__all__ = [
    "NEGATIVE_INFINITY",
    "POSITIVE_INFINITY",
    "Bound",
    "Interval",
    "Range",
]


class _NegativeInfinity:
    """Sentinel that sorts before every version.

    Used as the lower bound of unbounded intervals like ``(-inf, 5)``.
    Use the module-level ``NEGATIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return not isinstance(other, _NegativeInfinity)

    def __le__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return isinstance(other, _NegativeInfinity)

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _NegativeInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_NegativeInfinity")

    @override
    def __repr__(self) -> str:
        return "-inf"


class _PositiveInfinity:
    """Sentinel that sorts after every version.

    Used as the upper bound of unbounded intervals like ``[5, +inf)``.
    Use the module-level ``POSITIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return isinstance(other, _PositiveInfinity)

    def __gt__(self, other: object) -> bool:
        return not isinstance(other, _PositiveInfinity)

    def __ge__(self, other: object) -> bool:
        return True

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _PositiveInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_PositiveInfinity")

    @override
    def __repr__(self) -> str:
        return "+inf"


NEGATIVE_INFINITY = _NegativeInfinity()
POSITIVE_INFINITY = _PositiveInfinity()

# An interval is (lower, lower_inclusive, upper, upper_inclusive)
# where lower/upper can be NEGATIVE_INFINITY/POSITIVE_INFINITY for unbounded.
Bound: TypeAlias = Any  # V | _NegativeInfinity | _PositiveInfinity
Interval: TypeAlias = tuple[Bound, bool, Bound, bool]


def _same_bound(left: Bound, right: Bound) -> bool:
    """Whether two bounds are the same point, without comparing a version
    against an infinity sentinel."""
    if left is right:
        return True
    if (
        left is NEGATIVE_INFINITY
        or left is POSITIVE_INFINITY
        or right is NEGATIVE_INFINITY
        or right is POSITIVE_INFINITY
    ):
        return False
    return bool(left == right)


def _max_lower_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the higher of two lower bounds (for intersection)."""
    left_lower, left_lower_inc = left[0], left[1]
    right_lower, right_lower_inc = right[0], right[1]
    if _same_bound(left_lower, right_lower):
        return left_lower, left_lower_inc and right_lower_inc
    if left_lower is NEGATIVE_INFINITY or (
        right_lower is not NEGATIVE_INFINITY and left_lower < right_lower
    ):
        return right_lower, right_lower_inc
    return left_lower, left_lower_inc


def _min_upper_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the lower of two upper bounds (for intersection)."""
    left_upper, left_upper_inc = left[2], left[3]
    right_upper, right_upper_inc = right[2], right[3]
    if _same_bound(left_upper, right_upper):
        return left_upper, left_upper_inc and right_upper_inc
    if left_upper is POSITIVE_INFINITY or (
        right_upper is not POSITIVE_INFINITY and left_upper > right_upper
    ):
        return right_upper, right_upper_inc
    return left_upper, left_upper_inc


def _ends_before(right: Interval, left: Interval) -> bool:
    """Return True if ``right`` finishes below everything in ``left``."""
    right_upper, right_upper_inclusive = right[2], right[3]
    left_lower, left_lower_inclusive = left[0], left[1]
    if right_upper is POSITIVE_INFINITY or left_lower is NEGATIVE_INFINITY:
        return False
    if right_upper == left_lower:
        # They meet at a single point, shared only if both ends include it.
        return not (right_upper_inclusive and left_lower_inclusive)
    return bool(right_upper < left_lower)


def _advance_left(left: Interval, right: Interval) -> bool:
    """Return True if the merge walk should step ``left`` rather than ``right``.

    Whichever interval ends first cannot overlap anything further along the
    other side, so it is the one to retire.
    """
    left_upper = left[2]
    right_upper = right[2]
    if left_upper is POSITIVE_INFINITY:
        return right_upper is POSITIVE_INFINITY
    if right_upper is POSITIVE_INFINITY:
        return True
    return bool(left_upper <= right_upper)


def _interval_is_empty(
    lower: Bound,
    *,
    lower_inclusive: bool,
    upper: Bound,
    upper_inclusive: bool,
) -> bool:
    """Return True if the interval contains no versions."""
    if lower is NEGATIVE_INFINITY or upper is POSITIVE_INFINITY:
        return False
    if lower > upper:
        return True
    return lower == upper and not (lower_inclusive and upper_inclusive)


class Range(Generic[VersionType]):
    """A set of versions represented as sorted, non-overlapping intervals.

    Modeled after pubgrub-rs ``version_ranges::Ranges<V>``:
    https://docs.rs/version-ranges/latest/version_ranges/struct.Ranges.html

    Each interval is ``(lower, lower_inclusive, upper, upper_inclusive)``.
    The list is sorted by lower bound and intervals do not overlap or touch.
    """

    __slots__ = ("_hash", "_intervals")

    def __init__(self, intervals: tuple[Interval, ...] = ()) -> None:
        """Create a range from pre-sorted, non-overlapping intervals."""
        self._intervals = intervals
        self._hash = 0

    @classmethod
    def empty(cls) -> Range[VersionType]:
        """Create a range containing no versions."""
        return cls(())

    @classmethod
    def full(cls) -> Range[VersionType]:
        """Create a range containing all versions.

        Mirrors :meth:`packaging.ranges.VersionRange.full`.
        """
        return cls(((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),))

    @classmethod
    def singleton(cls, version: VersionType) -> Range[VersionType]:
        """Create a range containing exactly one version.

        Mirrors :meth:`packaging.ranges.VersionRange.singleton`.
        """
        return cls(((version, True, version, True),))

    @classmethod
    def at_least(cls, version: VersionType) -> Range[VersionType]:
        """Create ``[version, +inf)``."""
        return cls(((version, True, POSITIVE_INFINITY, False),))

    @classmethod
    def greater_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(version, +inf)``."""
        return cls(((version, False, POSITIVE_INFINITY, False),))

    @classmethod
    def at_most(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version]``."""
        return cls(((NEGATIVE_INFINITY, False, version, True),))

    @classmethod
    def less_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version)``."""
        return cls(((NEGATIVE_INFINITY, False, version, False),))

    @classmethod
    def between(cls, lower: VersionType, upper: VersionType) -> Range[VersionType]:
        """Create ``[lower, upper)``, or the empty range if ``lower >= upper``."""
        if _interval_is_empty(
            lower, lower_inclusive=True, upper=upper, upper_inclusive=False
        ):
            return cls(())
        return cls(((lower, True, upper, False),))

    @property
    def is_empty(self) -> bool:
        """``True`` if this range contains no versions."""
        return len(self._intervals) == 0

    def __contains__(self, version: object) -> bool:
        """Test whether version falls within this range.

        Intervals are sorted and non-overlapping, so at most one can hold the
        version: the last one starting at or below it.  Binary search finds
        that one, which matters because callers test every release of a
        package against the same range.
        """
        intervals = self._intervals
        low = 0
        high = len(intervals)

        while low < high:
            middle = (low + high) // 2
            lower = intervals[middle][0]
            if lower is NEGATIVE_INFINITY or not version < lower:
                low = middle + 1
            else:
                high = middle

        if low == 0:
            return False

        lower, lower_inclusive, upper, upper_inclusive = intervals[low - 1]

        if lower is not NEGATIVE_INFINITY and version == lower and not lower_inclusive:
            return False

        if upper is POSITIVE_INFINITY:
            return True

        return not (version > upper or (version == upper and not upper_inclusive))

    def __and__(self, other: object) -> Range[VersionType]:
        """Compute the intersection of two ranges (versions in both)."""
        if not isinstance(other, Range):
            return NotImplemented
        left_intervals = self._intervals
        right_intervals = other._intervals
        left_count = len(left_intervals)
        right_count = len(right_intervals)
        result: list[Interval] = []
        left_index = right_index = 0
        while left_index < left_count and right_index < right_count:
            left_interval = left_intervals[left_index]
            right_interval = right_intervals[right_index]

            inter_lower, inter_lower_inc = _max_lower_bound(
                left_interval, right_interval
            )
            inter_upper, inter_upper_inc = _min_upper_bound(
                left_interval, right_interval
            )

            if not _interval_is_empty(
                inter_lower,
                lower_inclusive=inter_lower_inc,
                upper=inter_upper,
                upper_inclusive=inter_upper_inc,
            ):
                result.append(
                    (inter_lower, inter_lower_inc, inter_upper, inter_upper_inc)
                )

            # Advance the side with the smaller upper bound
            left_upper = left_interval[2]
            right_upper = right_interval[2]
            if _same_bound(left_upper, right_upper):
                left_index += 1
                right_index += 1
            elif left_upper is POSITIVE_INFINITY or (
                right_upper is not POSITIVE_INFINITY and left_upper > right_upper
            ):
                right_index += 1
            else:
                left_index += 1

        return Range(tuple(result))

    def __or__(self, other: object) -> Range[VersionType]:
        """Union of two ranges (versions in either)."""
        if not isinstance(other, Range):
            return NotImplemented
        all_intervals = list(self._intervals) + list(other._intervals)
        return Range(_normalize_intervals(all_intervals))

    def __invert__(self) -> Range[VersionType]:
        """Complement (versions NOT in this range)."""
        if self.is_empty:
            return Range.full()

        result: list[Interval] = []
        previous_upper: Bound = NEGATIVE_INFINITY
        previous_upper_inclusive = False

        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            # Gap between the previous interval's upper and this lower.
            if (
                previous_upper is not NEGATIVE_INFINITY
                or lower is not NEGATIVE_INFINITY
            ):
                gap_lower = previous_upper
                gap_lower_inclusive = (
                    not previous_upper_inclusive
                    and previous_upper is not NEGATIVE_INFINITY
                )
                gap_upper = lower
                gap_upper_inclusive = (
                    not lower_inclusive and lower is not POSITIVE_INFINITY
                )

                if (
                    gap_lower is NEGATIVE_INFINITY
                    or gap_upper is POSITIVE_INFINITY
                    or gap_lower < gap_upper
                    or (
                        gap_lower == gap_upper
                        and gap_lower_inclusive
                        and gap_upper_inclusive
                    )
                ):
                    result.append(
                        (gap_lower, gap_lower_inclusive, gap_upper, gap_upper_inclusive)
                    )

            previous_upper = upper
            previous_upper_inclusive = upper_inclusive

        # Trailing gap after the last interval.
        if previous_upper is not POSITIVE_INFINITY:
            result.append(
                (previous_upper, not previous_upper_inclusive, POSITIVE_INFINITY, False)
            )

        return Range(tuple(result))

    def __sub__(self, other: object) -> Range[VersionType]:
        """Set difference: versions in self but not in other.

        Carves each of this range's intervals directly, rather than building
        the complement of ``other`` and intersecting with it.  Conflict
        analysis subtracts on every probe of the assignment trail, so the
        intermediate complement was pure allocation.
        """
        if not isinstance(other, Range):
            return NotImplemented

        right_intervals = other._intervals
        right_count = len(right_intervals)
        result: list[Interval] = []
        right_index = 0

        # _ends_before and _interval_is_empty are inlined throughout this
        # method: conflict analysis subtracts on every probe of the
        # assignment trail, and those two helpers were the largest self-time
        # of a deep backtrack.
        for left in self._intervals:
            lower, lower_inclusive, upper, upper_inclusive = left

            # Retire right intervals that finish below this one; they cannot
            # touch it or anything after it.
            while right_index < right_count:
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or lower is NEGATIVE_INFINITY:
                    break
                if right_upper == lower:
                    if right[3] and lower_inclusive:
                        break
                elif not right_upper < lower:
                    break
                right_index += 1

            exhausted = False
            scan = right_index

            while scan < right_count:
                right = right_intervals[scan]
                right_lower, right_lower_inclusive, right_upper, right_upper_inc = right

                # The remainder ends before this right interval: nothing
                # further right can touch it either.
                if upper is not POSITIVE_INFINITY and right_lower is not NEGATIVE_INFINITY:
                    if upper == right_lower:
                        if not (upper_inclusive and right_lower_inclusive):
                            break
                    elif upper < right_lower:
                        break

                # Keep the part of the remainder below this right interval,
                # i.e. (lower .. right_lower) unless that interval is empty.
                if right_lower is not NEGATIVE_INFINITY and not (
                    lower is not NEGATIVE_INFINITY
                    and (
                        lower > right_lower
                        or (
                            lower == right_lower
                            and not (lower_inclusive and not right_lower_inclusive)
                        )
                    )
                ):
                    result.append(
                        (
                            lower,
                            lower_inclusive,
                            right_lower,
                            not right_lower_inclusive,
                        ),
                    )

                if right_upper is POSITIVE_INFINITY:
                    exhausted = True
                    break

                # Resume above this right interval.
                lower, lower_inclusive = right_upper, not right_upper_inc
                if upper is not POSITIVE_INFINITY and (
                    lower > upper
                    or (lower == upper and not (lower_inclusive and upper_inclusive))
                ):
                    exhausted = True
                    break

                scan += 1

            if not exhausted and not (
                lower is not NEGATIVE_INFINITY
                and upper is not POSITIVE_INFINITY
                and (
                    lower > upper
                    or (lower == upper and not (lower_inclusive and upper_inclusive))
                )
            ):
                result.append((lower, lower_inclusive, upper, upper_inclusive))

        return Range(tuple(result))

    def is_subset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in self is also in other.

        Walks both interval lists once and stops at the first uncovered
        interval.  The set-difference formulation this replaces built a whole
        complement and a whole intersection only to ask whether the result was
        empty, which dominates resolution on packages with many releases.
        """
        right_intervals = other._intervals
        right_count = len(right_intervals)
        right_index = 0

        for left in self._intervals:
            # Skip right intervals that end before this one starts; they can
            # never cover it, and neither can they cover anything later.
            left_lower, left_lower_inclusive = left[0], left[1]
            while right_index < right_count:  # _ends_before(right, left), inlined
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or left_lower is NEGATIVE_INFINITY:
                    break
                if right_upper == left_lower:
                    if right[3] and left_lower_inclusive:
                        break
                elif not right_upper < left_lower:
                    break
                right_index += 1

            if right_index >= right_count:
                return False

            # Intervals are normalized -- sorted, non-overlapping and
            # non-touching -- so consecutive right intervals always have a gap
            # between them.  A left interval is therefore covered only if a
            # single right interval contains all of it.
            right = right_intervals[right_index]
            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            if (
                lower_inclusive is not left[1]
                or upper_inclusive is not left[3]
                or not _same_bound(lower, left[0])
                or not _same_bound(upper, left[2])
            ):
                return False

        return True

    def is_disjoint(self, other: Range[VersionType]) -> bool:
        """Return whether self and other share no version.

        Stops at the first shared version rather than materializing the whole
        intersection.
        """
        left_intervals = self._intervals
        right_intervals = other._intervals
        left_count = len(left_intervals)
        right_count = len(right_intervals)
        left_index = right_index = 0

        while left_index < left_count and right_index < right_count:
            left = left_intervals[left_index]
            right = right_intervals[right_index]

            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            if not _interval_is_empty(
                lower,
                lower_inclusive=lower_inclusive,
                upper=upper,
                upper_inclusive=upper_inclusive,
            ):
                return False

            if _advance_left(left, right):
                left_index += 1
            else:
                right_index += 1

        return True

    def relation(self, other: Range[VersionType]) -> RangeRelation:
        """Return how self's members sit against other's."""
        if self.is_empty:
            return _EMPTY_REL

        left_intervals = self._intervals
        right_intervals = other._intervals
        right_count = len(right_intervals)

        is_subset = True
        is_disjoint = True
        right_index = 0

        for left in left_intervals:
            left_lower, left_lower_inclusive = left[0], left[1]
            # Skip right intervals that end before this left starts
            # (_ends_before, inlined: this is the hottest loop in the
            # relation check that unit propagation runs per term).
            while right_index < right_count:
                right = right_intervals[right_index]
                right_upper = right[2]
                if right_upper is POSITIVE_INFINITY or left_lower is NEGATIVE_INFINITY:
                    break
                if right_upper == left_lower:
                    if right[3] and left_lower_inclusive:
                        break
                elif not right_upper < left_lower:
                    break
                right_index += 1

            if right_index >= right_count:
                is_subset = False
                if not is_disjoint:
                    return _OVERLAPPING_REL
                break

            right = right_intervals[right_index]

            if _ends_before(left, right):
                is_subset = False
                if not is_disjoint:
                    return _OVERLAPPING_REL
                continue

            # If we are here, left and right overlap.
            is_disjoint = False

            # Check if left is a subset of right
            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            if (
                lower_inclusive is not left[1]
                or upper_inclusive is not left[3]
                or not _same_bound(lower, left[0])
                or not _same_bound(upper, left[2])
            ):
                is_subset = False
                if not is_disjoint:  # always True here
                    return _OVERLAPPING_REL

        if is_subset:
            return _SUBSET_REL
        if is_disjoint:
            return _DISJOINT_REL
        return _OVERLAPPING_REL

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        if not isinstance(other, Range):
            return NotImplemented
        return self._intervals == other._intervals

    @override
    def __hash__(self) -> int:
        """Hash the interval tuple once; ranges are immutable.

        Ranges are used as cache keys during propagation, where a range over
        a package with many releases would otherwise be re-hashed -- walking
        every interval and every version in it -- on each lookup.
        """
        cached = self._hash
        if cached == 0:
            cached = hash(self._intervals) or 1
            self._hash = cached
        return cached

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"Range({self._intervals!r})"

    @override
    def __str__(self) -> str:
        """Return a human-readable representation."""
        if self.is_empty:
            return "<empty>"
        if self._intervals == ((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),):
            return "*"
        parts = []
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if lower == upper and lower_inclusive and upper_inclusive:
                parts.append(str(lower))
            else:
                left_bracket = "[" if lower_inclusive else "("
                right_bracket = "]" if upper_inclusive else ")"
                parts.append(f"{left_bracket}{lower}, {upper}{right_bracket}")
        return " | ".join(parts)

    def __bool__(self) -> bool:
        """Return True if this range is non-empty."""
        return not self.is_empty


def _interval_sort_key(interval: Interval) -> tuple[Any, ...]:
    lower = interval[0]
    if lower is NEGATIVE_INFINITY:
        return (0,)
    return (1, lower, 0 if interval[1] else 1)


def _normalize_intervals(intervals: list[Interval]) -> tuple[Interval, ...]:
    """Sort intervals by lower bound and merge overlapping or adjacent ones."""
    if not intervals:
        return ()

    intervals.sort(key=_interval_sort_key)

    merged: list[Interval] = [intervals[0]]
    for lower, lower_inclusive, upper, upper_inclusive in intervals[1:]:
        merged_lower, merged_lower_inclusive, merged_upper, merged_upper_inclusive = (
            merged[-1]
        )

        # Infinities at the boundary always overlap; otherwise compare values.
        intervals_overlap = (
            merged_upper is POSITIVE_INFINITY
            or lower is NEGATIVE_INFINITY
            or (
                merged_upper > lower
                or (
                    merged_upper == lower
                    and (merged_upper_inclusive or lower_inclusive)
                )
            )
        )

        if intervals_overlap:
            if merged_upper is POSITIVE_INFINITY or upper is POSITIVE_INFINITY:
                new_upper: Bound = POSITIVE_INFINITY
                new_upper_inclusive = False
            elif merged_upper > upper:
                new_upper, new_upper_inclusive = merged_upper, merged_upper_inclusive
            elif merged_upper == upper:
                new_upper = merged_upper
                new_upper_inclusive = merged_upper_inclusive or upper_inclusive
            else:
                new_upper, new_upper_inclusive = upper, upper_inclusive
            merged[-1] = (
                merged_lower,
                merged_lower_inclusive,
                new_upper,
                new_upper_inclusive,
            )
        else:
            merged.append((lower, lower_inclusive, upper, upper_inclusive))

    return tuple(merged)
