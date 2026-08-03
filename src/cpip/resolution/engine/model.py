"""Canonical values exchanged by resolution engines and candidate sources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.packaging import Requirement, Version


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    """Normalized candidate information shared by every candidate source."""

    name: str
    canonical_name: str
    version: Version | Any
    location: str | None = None
    source_kind: str | None = None
    requires_python: str | None = None
    dependencies: tuple[Requirement, ...] = ()
    artifact: Any = None

    @classmethod
    def from_candidate(cls, candidate: Any) -> ResolutionCandidate:
        return cls(
            name=candidate.name,
            canonical_name=candidate.canonical_name,
            version=candidate.version,
            location=getattr(candidate, "source_url", None)
            or getattr(candidate, "path", None),
            source_kind=getattr(candidate, "source_kind", None),
            requires_python=getattr(candidate, "requires_python", None),
            dependencies=tuple(getattr(candidate, "dependencies", ())),
            artifact=candidate,
        )


@dataclass(frozen=True, slots=True)
class ResolvedRequirement:
    """A requirement satisfied by an already-installed distribution."""

    requirement: Requirement
    distribution: InstalledDistribution


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """The single result shape returned by the canonical engine."""

    candidates: tuple[Any, ...]
    graph: Mapping[str, frozenset[str]]
    conflicts: tuple[str, ...] = ()
    satisfied: tuple[ResolvedRequirement, ...] = ()
    normalized_candidates: tuple[ResolutionCandidate, ...] = ()
    metrics: Mapping[str, int | float] = MappingProxyType({})

    @classmethod
    def from_plan(cls, plan: Any) -> ResolutionResult:
        graph = {
            name: frozenset(dependencies)
            for name, dependencies in getattr(plan, "graph", {}).items()
        }
        satisfied = tuple(
            ResolvedRequirement(item.requirement, item.distribution)
            for item in getattr(plan, "satisfied", ())
        )
        artifacts = tuple(getattr(plan, "candidates", ()))
        normalized = tuple(
            ResolutionCandidate.from_candidate(candidate) for candidate in artifacts
        )
        return cls(
            candidates=artifacts,
            graph=MappingProxyType(graph),
            conflicts=tuple(getattr(plan, "conflicts", ())),
            satisfied=satisfied,
            normalized_candidates=normalized,
            metrics=MappingProxyType(dict(getattr(plan, "metrics", {}))),
        )

    @classmethod
    def from_candidates(cls, candidates: Iterable[Any]) -> ResolutionResult:
        artifacts = tuple(candidates)
        return cls(
            candidates=artifacts,
            graph=MappingProxyType({}),
            normalized_candidates=tuple(
                ResolutionCandidate.from_candidate(candidate) for candidate in artifacts
            ),
        )

    def candidate_artifacts(self) -> tuple[Any, ...]:
        """Return source-specific artifacts for installation adapters."""
        return self.candidates
