"""Static contracts shared by the resolver's internal operation domains."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.packaging import Requirement, Version
    from cpip.index.candidate_materialization import CandidateStream
    from cpip.index.provider import CandidateProvider
    from cpip.resolution.engine.constraints import ConstraintStore
    from cpip.resolution.engine.input.models import RequirementInput
    from cpip.resolution.engine.metrics import ResolutionMetrics
    from cpip.resolution.engine.state.memo import FailedStateMemo
    from cpip.resolution.engine.state.domains import (
        Assignment,
        LearnedIncompatibility,
        PackageDomain,
        RequirementStateKey,
    )
    from cpip.resolution.engine.state.requirement_set import RequirementSet


class EngineConfiguration(Protocol):
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
    debug_internal: bool


class SearchState(Protocol):
    """Mutable state owned by the search and selection domains."""

    candidate_cache: dict[tuple[object, ...], CandidateStream]
    candidate_count_cache: dict[
        tuple[str, str, tuple[str, ...], str | None, str | None, bool],
        int,
    ]
    domain_viability_cache: dict[tuple[str, tuple[str, ...]], bool]
    specifier_intersection_cache: dict[tuple[str, ...], bool]
    version_tables: dict[str, tuple[Version, ...]]
    version_indexes: dict[str, dict[Version, int]]
    version_masks: dict[tuple[str, str, bool], int]
    active_version_masks: dict[tuple[str, tuple[tuple[str, bool], ...]], int]
    allowed_versions_cache: dict[tuple[str, int], frozenset[Version]]
    allow_prereleases_cache: dict[int, tuple[Requirement, bool]]
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
    failed_search_states: FailedStateMemo
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
    kernel_enabled: bool
    metrics: ResolutionMetrics
    release_frontier: Any


class ConflictState(Protocol):
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


class EngineOperations(Protocol):
    """Cross-domain operations shared by search, selection, and learning."""

    def apply_constraints(self, *args: Any, **kwargs: Any) -> Any: ...

    def allow_prereleases_internal(self, *args: Any, **kwargs: Any) -> Any: ...

    def find_candidates_internal(self, *args: Any, **kwargs: Any) -> Any: ...

    def find_installed_internal(self, *args: Any, **kwargs: Any) -> Any: ...

    def candidate_cache_key(self, *args: Any, **kwargs: Any) -> Any: ...

    def candidate_is_satisfied(self, *args: Any, **kwargs: Any) -> Any: ...

    def candidate_is_satisfied_by(self, *args: Any, **kwargs: Any) -> Any: ...

    def validate_candidate_policy(self, *args: Any, **kwargs: Any) -> Any: ...

    def validate_candidate_constraints(self, *args: Any, **kwargs: Any) -> Any: ...

    def validate_candidate_hashes(self, *args: Any, **kwargs: Any) -> Any: ...

    def domain_version_mask(self, *args: Any, **kwargs: Any) -> Any: ...

    def package_id_internal(self, *args: Any, **kwargs: Any) -> Any: ...

    def upgrade_allowed_for(self, *args: Any, **kwargs: Any) -> Any: ...

    # The engine is assembled from operation mixins.  These declarations make
    # that composition explicit to type checkers while keeping each operation
    # module independent of the concrete runtime class.
    def active_allowed_versions(self, *args: Any, **kwargs: Any) -> Any: ...
    def active_requirements_for(self, *args: Any, **kwargs: Any) -> Any: ...
    def add_candidate_dependencies(self, *args: Any, **kwargs: Any) -> Any: ...
    def bump_conflict_activity(self, *args: Any, **kwargs: Any) -> Any: ...
    def candidate_dependencies_conflict(self, *args: Any, **kwargs: Any) -> Any: ...
    def candidate_matches_python(self, *args: Any, **kwargs: Any) -> Any: ...
    def candidate_matches_seed(self, *args: Any, **kwargs: Any) -> Any: ...
    def candidate_with_extras(self, *args: Any, **kwargs: Any) -> Any: ...
    def candidate_count_internal(self, *args: Any, **kwargs: Any) -> Any: ...
    def choose_requirement(self, *args: Any, **kwargs: Any) -> Any: ...
    def compact_learned_incompatibilities(self, *args: Any, **kwargs: Any) -> Any: ...
    def decision_candidate_count(self, *args: Any, **kwargs: Any) -> Any: ...
    def derive_candidate_domain_conflict(self, *args: Any, **kwargs: Any) -> Any: ...
    def does_not_provide_extra_text(self, *args: Any, **kwargs: Any) -> Any: ...
    def emit_backtracking_message(self, *args: Any, **kwargs: Any) -> Any: ...
    def grouped_candidate_dependencies(self, *args: Any, **kwargs: Any) -> Any: ...
    def no_matching_distribution_message(self, *args: Any, **kwargs: Any) -> Any: ...
    def preflight_hash_requirement(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconsideration_key(self, *args: Any, **kwargs: Any) -> Any: ...
    def remove_candidate_dependencies(self, *args: Any, **kwargs: Any) -> Any: ...
    def requirement_version_mask(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_frame_inner(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_frame_internal(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_step(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_state_key_internal(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_state_fingerprint_internal(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_uncached(self, *args: Any, **kwargs: Any) -> Any: ...
    def search_with_satisfied(self, *args: Any, **kwargs: Any) -> Any: ...
    def should_backjump_after_failure(self, *args: Any, **kwargs: Any) -> Any: ...
    def satisfied_dependencies_are_consistent(
        self, *args: Any, **kwargs: Any
    ) -> Any: ...
    def validate_requires_python(self, *args: Any, **kwargs: Any) -> Any: ...
    def validate_external_url_dependencies(self, *args: Any, **kwargs: Any) -> Any: ...
    def validate_link_hashes(self, *args: Any, **kwargs: Any) -> Any: ...
    def violates_watched_incompatibility(self, *args: Any, **kwargs: Any) -> Any: ...
    def version_table(self, *args: Any, **kwargs: Any) -> Any: ...
    def warn_missing_candidate_extras(self, *args: Any, **kwargs: Any) -> Any: ...
    def warn_missing_extras(self, *args: Any, **kwargs: Any) -> Any: ...
    def warn_missing_installed_extras(self, *args: Any, **kwargs: Any) -> Any: ...


class ConflictCallbacks(Protocol):
    """Callbacks used by the search loop to learn and inspect conflicts."""

    def candidate_assignment(self, *args: Any, **kwargs: Any) -> Any: ...

    def candidate_incompatibility_key(self, *args: Any, **kwargs: Any) -> Any: ...

    def dependency_domain_conflicts(self, *args: Any, **kwargs: Any) -> Any: ...

    def learn_candidate_pair_incompatibility(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...

    def learn_watched_incompatibility(self, *args: Any, **kwargs: Any) -> Any: ...

    def record_learned_incompatibility(self, *args: Any, **kwargs: Any) -> Any: ...

    def minimal_exact_conflict_sources(self, *args: Any, **kwargs: Any) -> Any: ...

    def requirements_version_mask(self, *args: Any, **kwargs: Any) -> Any: ...


class ConfigurationContext(
    EngineConfiguration,
    SearchState,
    EngineOperations,
    Protocol,
):
    """Configuration-only contract used by policy and validation checks."""


class SelectionContext(EngineConfiguration, SearchState, EngineOperations, Protocol):
    """Candidate-selection contract over configuration and search state."""

    conflict_activity: list[int]
    package_ids: dict[str, int]


class ConflictContext(
    EngineConfiguration,
    SearchState,
    ConflictState,
    EngineOperations,
    ConflictCallbacks,
    Protocol,
):
    """Conflict-learning contract over search and incompatibility state."""


class EngineContext(
    EngineConfiguration,
    SearchState,
    ConflictState,
    EngineOperations,
    ConflictCallbacks,
    Protocol,
):
    """State contract consumed by the resolver's internal operation domains.

    The concrete runtime supplies storage and method dispatch through multiple
    inheritance. State is split into capability protocols above so each
    operation can depend on explicit state and callbacks.
    """

    root_requirements: list[Requirement]
    conflicts: list[str]
    debug_internal: bool

    def source_requirement_map(
        self,
        requirements_input: RequirementSet | Iterable[RequirementInput] | list[str],
    ) -> tuple[dict[str, RequirementInput], dict[str, RequirementInput]]:
        """Return source requirements used by the search boundary."""
