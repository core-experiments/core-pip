"""Caches and serialized metadata for the fast wheelhouse path."""

from __future__ import annotations

from cpip.resolution.fast_wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelVersion,
)

MetadataHeaders = dict[str, list[str]]
CachedValue = tuple[str, str, bool]
CachedDependency = tuple[
    str,
    tuple[CachedValue, ...],
    tuple[str, ...],
    tuple[str, str] | None,
]
CachedMetadata = tuple[
    str,
    str,
    tuple[CachedDependency, ...],
    tuple[str, ...],
    str | None,
]
CachedCandidateParts = tuple[
    str,
    LocalWheelVersion,
    tuple[LocalWheelRequirement, ...],
    frozenset[str],
    str | None,
]
MetadataCache = dict[str, tuple[int, int, MetadataHeaders | CachedMetadata]]
MetadataCacheIdentity = tuple[int, int, int]
MetadataCacheStore = dict[str, tuple[MetadataCacheIdentity | None, MetadataCache]]
CandidateCache = dict[tuple[str, int, int, int], LocalWheelCandidate]
CatalogRecords = dict[str, list[tuple[str, LocalWheelVersion]]]
CatalogSignatures = tuple[tuple[str, str, int, int, int], ...]
CatalogSnapshotStore = dict[
    str,
    tuple[tuple[int, int, int] | None, CatalogSignatures, CatalogRecords],
]
Domain = int
RangeIndex = tuple[
    tuple[tuple[int, ...], ...],
    tuple[tuple[str, LocalWheelVersion], ...],
]
ExactIndex = dict[str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]]
ExactMasks = dict[str, dict[tuple[int, ...], Domain]]
CatalogIndexes = tuple[ExactIndex, ExactMasks, dict[str, RangeIndex]]
DomainCache = dict[tuple[str, tuple[tuple[str, LocalWheelVersion | str], ...]], Domain]
PreflightCache = dict[tuple[str, frozenset[str]], bool]
SharedPreflightCache = dict[
    tuple[int, int, frozenset[str]], tuple[ExactIndex, LocalWheelCandidate, bool]
]
_CATALOG_CACHE_VERSION = 2
metadata_cache_store: MetadataCacheStore = {}
metadata_cache_paths: dict[int, str] = {}
metadata_cache_dirty: set[str] = set()
catalog_indexes_cache: dict[int, tuple[CatalogRecords, CatalogIndexes]] = {}
candidate_cache: CandidateCache = {}
catalog_snapshot_store: CatalogSnapshotStore = {}
shared_preflight_cache: SharedPreflightCache = {}


def cache_candidate(
    key: tuple[str, int, int, int], candidate: LocalWheelCandidate
) -> None:
    candidate_cache[key] = candidate
    if len(candidate_cache) > 16384:
        candidate_cache.pop(next(iter(candidate_cache)))


def cache_preflight_result(
    cache: SharedPreflightCache,
    exact_index: ExactIndex,
    candidate: LocalWheelCandidate,
    extras: frozenset[str],
    result: bool,
) -> None:
    key = (id(exact_index), id(candidate), extras)
    cache[key] = (exact_index, candidate, result)
    if len(cache) > 8192:
        cache.pop(next(iter(cache)))


def cache_metadata(candidate: LocalWheelCandidate) -> CachedMetadata:
    dependencies = tuple(
        (
            dependency.name,
            tuple(
                (
                    operator,
                    expected.text
                    if isinstance(expected, LocalWheelVersion)
                    else expected,
                    not isinstance(expected, LocalWheelVersion),
                )
                for operator, expected in dependency.specifier.values
            ),
            tuple(dependency.extras),
            dependency.marker,
        )
        for dependency in candidate.dependencies
    )
    return (
        candidate.name,
        candidate.version.text,
        dependencies,
        tuple(candidate.provided_extras),
        candidate.requires_python,
    )
