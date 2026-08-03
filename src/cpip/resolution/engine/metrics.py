"""Low-overhead counters for resolution performance investigations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ResolutionMetrics:
    """Counters collected for one resolver invocation.

    Metrics are intentionally plain values so callers can serialize them without
    depending on the resolver's internal state objects.
    """

    candidate_queries: int = 0
    candidate_cache_hits: int = 0
    candidates_consumed: int = 0
    decisions: int = 0
    propagations: int = 0
    conflicts: int = 0
    backjumps: int = 0
    learned_incompatibilities: int = 0
    catalogs_loaded: int = 0
    catalog_cache_hits: int = 0
    release_masks_built: int = 0
    release_intersections: int = 0
    metadata_loads: int = 0
    metadata_cache_hits: int = 0
    metadata_prefetches: int = 0
    artifact_materializations: int = 0
    resolution_seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "candidate_queries": self.candidate_queries,
            "candidate_cache_hits": self.candidate_cache_hits,
            "candidates_consumed": self.candidates_consumed,
            "decisions": self.decisions,
            "propagations": self.propagations,
            "conflicts": self.conflicts,
            "backjumps": self.backjumps,
            "learned_incompatibilities": self.learned_incompatibilities,
            "catalogs_loaded": self.catalogs_loaded,
            "catalog_cache_hits": self.catalog_cache_hits,
            "release_masks_built": self.release_masks_built,
            "release_intersections": self.release_intersections,
            "metadata_loads": self.metadata_loads,
            "metadata_cache_hits": self.metadata_cache_hits,
            "metadata_prefetches": self.metadata_prefetches,
            "artifact_materializations": self.artifact_materializations,
            "resolution_seconds": self.resolution_seconds,
        }
