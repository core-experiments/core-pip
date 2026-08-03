"""Compact release-frontier state used by the resolution search."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from cpip.core.packaging import Requirement, Version
from cpip.index.source_models import CandidateRecord, PackageCatalog

MAX_COMPACT_RELEASES = 128


@dataclass(slots=True)
class ReleaseFrontierMetrics:
    catalogs_loaded: int = 0
    catalog_hits: int = 0
    release_masks_built: int = 0
    release_intersections: int = 0


@dataclass(frozen=True, slots=True)
class ReleaseDomain:
    versions: tuple[Version, ...]
    candidates_by_version: MappingProxyType
    full_mask: int

    def mask_for(self, requirement: Requirement, *, allow_prereleases: bool) -> int:
        mask = 0
        for index, version in enumerate(self.versions):
            if requirement.is_satisfied_by(
                version,
                allow_prereleases=allow_prereleases,
            ):
                mask |= 1 << index
        return mask


class ReleaseFrontier:
    """Release-level catalog and mask cache for one resolution invocation."""

    __slots__ = (
        "provider",
        "catalogs",
        "domains",
        "mask_cache",
        "candidate_cache",
        "metrics",
    )

    def __init__(self, provider: object) -> None:
        self.provider = provider
        self.catalogs: dict[tuple[str, bool, bool], PackageCatalog] = {}
        self.domains: dict[tuple[str, bool, bool], ReleaseDomain] = {}
        self.mask_cache: dict[tuple[object, ...], int] = {}
        self.candidate_cache: dict[
            tuple[str, Version], tuple[CandidateRecord, ...]
        ] = {}
        self.metrics = ReleaseFrontierMetrics()

    def reset(self) -> None:
        self.catalogs.clear()
        self.domains.clear()
        self.mask_cache.clear()
        self.candidate_cache.clear()
        self.metrics = ReleaseFrontierMetrics()

    def catalog_for(self, requirement: Requirement) -> PackageCatalog | None:
        # Remote catalog acquisition is already overlapped by the provider's
        # prefetcher.  Do not pay for a second release-mask representation on
        # that path until a catalog is large enough to justify a dedicated
        # remote frontier batch.  Local catalogs are cheap and deterministic,
        # so they remain eligible for compact-domain propagation.
        if getattr(self.provider, "prefetch_remote_sources", False):
            return None
        allowed = self.provider.allowed_formats_internal(requirement)
        key = (requirement.canonical_name, *allowed)
        catalog = self.catalogs.get(key)
        if catalog is not None:
            self.metrics.catalog_hits += 1
            return catalog
        catalog = self.provider.package_catalog_cache.get(key)
        if catalog is None:
            return None
        if len(catalog.summary_versions) > MAX_COMPACT_RELEASES:
            return None
        self.catalogs[key] = catalog
        self.metrics.catalogs_loaded += 1
        return catalog

    def domain_for(self, requirement: Requirement) -> ReleaseDomain | None:
        allowed = self.provider.allowed_formats_internal(requirement)
        key = (requirement.canonical_name, *allowed)
        domain = self.domains.get(key)
        if domain is not None:
            self.metrics.catalog_hits += 1
            return domain
        catalog = self.catalog_for(requirement)
        if catalog is None:
            return None
        versions = tuple(catalog.summary_versions)
        domain = ReleaseDomain(
            versions=versions,
            candidates_by_version=MappingProxyType(catalog.candidates_by_version),
            full_mask=(1 << len(versions)) - 1,
        )
        self.domains[key] = domain
        self.metrics.release_masks_built += 1
        return domain

    def allowed_versions(
        self,
        requirement: Requirement,
        *,
        allow_prereleases: bool,
    ) -> frozenset[Version] | None:
        domain = self.domain_for(requirement)
        if domain is None:
            return None
        cache_key = (
            requirement.canonical_name,
            requirement.specifier.text_internal,
            requirement.marker or "",
            tuple(sorted(requirement.extras)),
            allow_prereleases,
            domain.full_mask,
        )
        mask = self.mask_cache.get(cache_key)
        if mask is None:
            mask = domain.mask_for(
                requirement,
                allow_prereleases=allow_prereleases,
            )
            self.mask_cache[cache_key] = mask
        self.metrics.release_intersections += 1
        return frozenset(
            version
            for index, version in enumerate(domain.versions)
            if mask & (1 << index)
        )

    def candidates_for(
        self,
        requirement: Requirement,
        version: Version,
    ) -> tuple[CandidateRecord, ...]:
        key = (requirement.canonical_name, version)
        candidates = self.candidate_cache.get(key)
        if candidates is not None:
            return candidates
        domain = self.domain_for(requirement)
        if domain is None:
            return ()
        candidates = tuple(domain.candidates_by_version.get(version, ()))
        self.candidate_cache[key] = candidates
        return candidates
