"""Static contracts shared by the resolver's internal operation domains."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.packaging import Requirement, Version
    from cpip.index.candidate_materialization import CandidateStream
    from cpip.index.provider import CandidateProvider
    from cpip.resolution.constraints import ConstraintStore
    from cpip.resolution.req_install import InstallRequirement
    from cpip.resolution.requirement_set import RequirementSet
    from cpip.resolution.resolver_internals.state.domains import (
        Assignment,
        LearnedIncompatibility,
        PackageDomain,
        RequirementStateKey,
    )


class ResolverMetrics:
    """Optional counters for resolver performance investigations.

    The hot-path increment is guarded by ``enabled`` and the object is slotted
    so normal installs do not allocate per-event objects or dictionaries.
    """

    __slots__ = (
        "enabled",
        "candidate_cache_hits",
        "candidate_cache_misses",
        "candidates_considered",
        "decisions",
        "propagations",
        "search_frames",
        "backtracks",
        "failed_state_hits",
        "root_incompatibility_hits",
        "max_trail_depth",
        "nonchronological_jumps",
        "learned_clause_evictions",
        "resolution_seed_hits",
    )

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.candidate_cache_hits = 0
        self.candidate_cache_misses = 0
        self.candidates_considered = 0
        self.decisions = 0
        self.propagations = 0
        self.search_frames = 0
        self.backtracks = 0
        self.failed_state_hits = 0
        self.root_incompatibility_hits = 0
        self.max_trail_depth = 0
        self.nonchronological_jumps = 0
        self.learned_clause_evictions = 0
        self.resolution_seed_hits = 0

    def snapshot(self) -> dict[str, int]:
        return {
            name: getattr(self, name) for name in self.__slots__ if name != "enabled"
        }


class ResolverConfiguration(Protocol):
    """Resolver options and package-source collaborators."""

    provider: CandidateProvider
    constraint_store: ConstraintStore
    constraints: list[Requirement]
    constraints_by_name: dict[str, tuple[Requirement, ...]]
    no_deps: bool
    ignore_installed: bool
    upgrade: bool
    upgrade_strategy: str
    allow_prereleases: bool
    require_hashes: bool
    compute_source_hashes: bool
    ignore_requires_python: bool
    python_version: str
    root_requirement_names: set[str]
    installed_by_name_internal: dict[str, InstalledDistribution] | None


class ResolverSearchState(Protocol):
    """Mutable state owned by the search and selection domains."""

    candidate_cache: dict[tuple[object, ...], CandidateStream]
    candidate_count_cache: dict[
        tuple[str, str, tuple[str, ...], str | None, str | None, bool], int
    ]
    domain_viability_cache: dict[tuple[str, tuple[str, ...]], bool]
    version_tables: dict[str, tuple[Version, ...]]
    version_indexes: dict[str, dict[Version, int]]
    version_masks: dict[tuple[str, str, bool], int]
    active_version_masks: dict[tuple[str, tuple[tuple[str, bool], ...]], int]
    allowed_versions_cache: dict[tuple[str, int], frozenset[Version]]
    allow_prereleases_cache: dict[tuple[str, str, str | None, str], bool]
    incoming_requirements: dict[str, dict[str, tuple[Requirement, ...]]]
    domains_internal: dict[str, PackageDomain]
    unavailable_requirements: dict[str, Requirement]
    warned_missing_extras: set[tuple[str, str]]
    reconsidering: set[
        tuple[
            str,
            tuple[tuple[str, str, tuple[str, ...], str | None, str | None], ...],
        ]
    ]
    failed_search_states: set[tuple[object, ...]]
    candidate_state_keys: dict[int, tuple[str, str, str, str]]
    requirement_state_keys: dict[int, RequirementStateKey]
    candidate_dependency_groups: dict[
        tuple[int, frozenset[str]],
        tuple[tuple[str, tuple[Requirement, ...]], ...],
    ]
    backtrack_count: int
    last_conflict_was_root: bool
    root_incompatibility_hits: int
    conflict_activity_bumps: int
    learned_clause_limit: int
    resolution_seed: dict[str, tuple[str, str]]
    assignment_levels: dict[Assignment, int]
    backjump_conflict: LearnedIncompatibility | None
    last_candidate_conflict: LearnedIncompatibility | None
    metrics: ResolverMetrics


class ResolverConflictState(Protocol):
    """Mutable indexes used by conflict analysis and learned clauses."""

    package_ids: dict[str, int]
    package_names_internal: list[str]
    candidate_ids: dict[tuple[int, Version, str], int]
    candidate_assignment_cache: dict[tuple[int, frozenset[str]], Assignment]
    conflict_activity: list[int]
    learned_incompatibilities: list[LearnedIncompatibility]
    learned_incompatibility_terms: set[frozenset[Assignment]]
    incompatibility_watches: dict[int, set[int]]
    binary_incompatibility_watches: dict[Assignment, set[int]]
    learned_non_binary_count: int
    root_incompatibilities: set[tuple[int, frozenset[str]]]
    root_unsatisfiable_domains: set[tuple[object, ...]]
    seen_candidate_conflicts: set[tuple[int, frozenset[str]]]


class ResolverContext(
    ResolverConfiguration, ResolverSearchState, ResolverConflictState, Protocol
):
    """State contract consumed by the resolver's internal operation domains.

    The concrete :class:`Resolver` supplies storage and method dispatch through
    multiple inheritance.  State is split into capability protocols above so a
    future operation can depend on one domain without claiming ownership of all
    resolver state.
    """

    root_requirements: list[Requirement]
    conflicts: list[str]
    debug_internal: bool

    def source_requirement_map(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> tuple[dict[str, InstallRequirement], dict[str, InstallRequirement]]:
        """Return source requirements used by the search boundary."""

    def __getattr__(self, name: str) -> Any:
        """Expose operations supplied by the other resolver mixins.

        Methods are intentionally not duplicated across these state contracts:
        the concrete resolver composes them from operation domains.  The
        fallback is limited to cross-domain method lookup; resolver state above
        remains explicit and typed.
        """

        raise AttributeError(name)
