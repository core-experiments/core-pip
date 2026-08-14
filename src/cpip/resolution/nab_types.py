"""Standalone types and pure helpers shared by the NAB provider adapter.

These carry no ``NabProvider`` instance state, so they live apart from the
adapter's stateful decision/candidate-selection logic in nab_provider.py.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from cpip._vendor.nab_resolver.ranges import Range
from cpip.core.metadata import InstalledDistribution
from cpip.core.packaging import (
    InvalidVersion,
    Requirement,
    SpecifierSet,
    Version,
    canonicalize_name,
)


# Version("0") is the "no declared version" sentinel checked against on
# every candidate in _inconsistent_metadata_rejects; parsing it fresh each
# call is pure overhead for a fixed value.
_ZERO_VERSION = Version("0")


class InstalledCandidate:
    """NAB candidate backed by an already-installed distribution."""

    source_kind = "installed"
    source_url = None
    source_vcs = None
    source_hashes = None
    path = ""
    from_cache = False
    yanked_reason = None
    requires_python = None
    provided_extras = frozenset()

    def __init__(
        self, distribution: InstalledDistribution, extras: frozenset[str]
    ) -> None:
        self.distribution = distribution
        self.name = distribution.name
        self.version = Version(distribution.version)
        self.dependencies = tuple(distribution.dependencies(extras))
        self.path = distribution.location

    @property
    def canonical_name(self) -> str:
        return self.distribution.canonical_name


# Fewer than two exact pins cannot intersect to nothing.
_MIN_PINS_TO_DISAGREE = 2


def _dependencies_or_none(candidate: object) -> tuple[Requirement, ...] | None:
    """A candidate's dependencies, or None when its metadata will not load.

    Reading ``dependencies`` is what pulls metadata, which for a source
    artifact means a build and for a remote one a request.  The forward check
    asks this of releases the resolver may never select, so a release with
    broken or unreachable metadata must read as undecidable rather than take
    the whole resolution down with it -- a different release may well resolve.
    """
    try:
        return tuple(getattr(candidate, "dependencies", ()))
    except Exception:
        return None


def _implied_range(specifier: SpecifierSet) -> Range[Version]:
    """Widen a specifier to the interval that contains everything it admits.

    ``bounds`` drops ``!=``, ``===`` and ``==X.*`` and reads ``~=`` as its
    half-open interval, and it is blind to pre-release rules.  Every one of
    those is a widening, which is the direction a rejection needs: an empty
    intersection of intervals that each contain *more* than their specifier is
    empty for the specifiers too.

    Working in intervals rather than over the catalog also keeps the answer
    independent of which releases the active policy happens to admit -- a
    yanked-only release cannot turn a possible fan-out into a rejected one.
    """
    lower, upper = specifier.bounds()
    result: Range[Version] = Range.full()

    if lower is not None:
        version, inclusive = lower
        result = result & (
            Range.at_least(version) if inclusive else Range.greater_than(version)
        )

    if upper is not None:
        version, inclusive = upper
        result = result & (
            Range.at_most(version) if inclusive else Range.less_than(version)
        )

    return result


def _exact_pin(requirement: Requirement) -> Version | None:
    """The single release a ``==`` requirement names, or None.

    Anything else -- a range, several clauses, a wildcard, an unparseable
    version -- has no unique release and so cannot narrow a domain to a point.
    """
    clauses = requirement.specifier.specifiers
    if len(clauses) != 1:
        return None

    clause = clauses[0]
    if clause.operator != "==" or clause.version.endswith("*"):
        return None

    try:
        return Version(clause.version)
    except InvalidVersion:
        return None


def _key(requirement: Requirement) -> str:
    name = requirement.name
    if name.startswith(("file://", "http://", "https://")):
        name = urlsplit(name).path.rstrip("/").rsplit("/", 1)[-1] or name
    return canonicalize_name(name)


class _RecordingRequirements(dict):
    """The package -> requirement map, recording every package it replaces.

    ``prioritize`` answers from ``len(_versions(package))``, which follows
    the package's requirement, and the resolver caches sort keys between
    decision scans. Recording in ``__setitem__`` rather than at the handful
    of assignment sites is what keeps that record complete as merging grows
    new ones.
    """

    __slots__ = ("touched",)

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def __setitem__(self, key: str, value: Requirement) -> None:
        self.touched.add(key)
        super().__setitem__(key, value)
