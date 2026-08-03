from __future__ import annotations

import logging
import os
import sys
import urllib.parse  # noqa: F401 - compatibility monkeypatch seam
from collections.abc import Collection, Iterable
from time import perf_counter
from typing import TYPE_CHECKING, cast

from cpip.core.errors import (
    DistributionNotFound,
    ResolutionError,
)
from cpip.core.packaging import (
    Requirement,
    Version,
    marker_applies,
    parse_requirement,
)
from cpip.core.python import CURRENT_PYTHON_VERSION_FULL
from cpip.index.provider import CandidateProvider
from cpip.resolution.engine.algorithms import (
    direct_urls_equivalent,
    is_direct_requirement,
    topological_weights,
)
from cpip.resolution.engine.conflict_learning import ConflictLearning
from cpip.resolution.engine.constraints import ConstraintStore
from cpip.resolution.engine.frontier import ReleaseFrontier
from cpip.resolution.engine.loop import SearchLoop
from cpip.resolution.engine.policy import PolicyChecks
from cpip.resolution.engine.metrics import ResolutionMetrics
from cpip.resolution.engine.state.domains import (
    Assignment,
    LearnedIncompatibility,
    PackageDomain,
    RequirementStateKey,
)
from cpip.resolution.engine.state.plans import (
    InstallPlan,
    SatisfiedRequirement,
)

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.wheel import WheelCandidate
    from cpip.index.candidate_materialization import CandidateStream
    from cpip.resolution.engine.context import EngineContext
    from cpip.resolution.engine.context import ConfigurationContext
    from cpip.resolution.engine.state.requirement_set import RequirementSet
    from cpip.resolution.req_install import InstallRequirement


logger = logging.getLogger(__name__)

FALSE_VALUES = frozenset((None, "", "0", "false", "False"))
SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))
SOURCE_TREE_OR_VCS_KINDS = frozenset(("source-tree", "vcs"))


class ResolutionRuntime(
    SearchLoop,
    ConflictLearning,
    PolicyChecks,
):
    def __getattr__(self, name: str) -> object:
        raise AttributeError(name)

    def __init__(
        self,
        *,
        provider: CandidateProvider | None = None,
        find_links: list[str] | None = None,
        index_urls: list[str] | None = None,
        no_index: bool = False,
        no_deps: bool = False,
        upgrade: bool = False,
        ignore_installed: bool = False,
        constraints: list[str] | None = None,
        allow_prereleases: bool = False,
        require_hashes: bool = False,
        compute_source_hashes: bool = True,
        upgrade_strategy: str = "only-if-needed",
        ignore_requires_python: bool = False,
        python_version: str | None = None,
    ) -> None:
        if provider is None and index_urls is None:
            provider = CandidateProvider.from_options(
                find_links=find_links or (),
                no_index=no_index,
            )
        elif provider is None:
            provider = CandidateProvider.from_options(
                find_links=find_links or (),
                index_url=index_urls[0] if index_urls else None,
                extra_index_urls=index_urls[1:] if index_urls else (),
                no_index=no_index,
            )
        self.provider = provider
        self.metrics = ResolutionMetrics()
        self.release_frontier = ReleaseFrontier(provider)
        self.no_deps = no_deps
        self.upgrade = upgrade
        self.ignore_installed = ignore_installed
        self.constraint_store = ConstraintStore(
            (parse_requirement(item) for item in constraints or ()),
            direct_urls_equivalent=direct_urls_equivalent,
        )
        self.constraints = list(self.constraint_store.constraints)
        self.constraints_by_name = self.constraint_store.constraints_by_name
        self.allow_prereleases = allow_prereleases
        if (
            allow_prereleases
            and provider is not None
            and provider.release_control is not None
        ):
            provider.release_control.apply("all_releases", ":all:")
        self.require_hashes = require_hashes
        self.compute_source_hashes = compute_source_hashes or require_hashes
        self.provider.compute_source_hashes = require_hashes
        # The finite-domain kernel is deliberately guarded by its own
        # eligibility checks.  Keeping this switch internal lets diagnostics
        # and tests force the generic resolver without adding a public API.
        self.kernel_enabled = True
        self.upgrade_strategy = upgrade_strategy
        self.ignore_requires_python = ignore_requires_python
        self.python_version = python_version or ".".join(CURRENT_PYTHON_VERSION_FULL)
        self.root_requirements: list[Requirement] = []
        self.root_requirement_names: set[str] = set()
        self.conflicts: list[str] = []
        self.candidate_cache: dict[tuple[object, ...], CandidateStream] = {}
        self.candidate_count_cache: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None, bool],
            int,
        ] = {}
        self.domain_viability_cache: dict[tuple[str, tuple[str, ...]], bool] = {}
        self.version_tables: dict[str, tuple[Version, ...]] = {}
        self.version_indexes: dict[str, dict[Version, int]] = {}
        self.version_masks: dict[tuple[str, str, bool], int] = {}
        self.active_version_masks: dict[
            tuple[str, tuple[tuple[str, bool], ...]],
            int,
        ] = {}
        self.allowed_versions_cache: dict[tuple[str, int], frozenset[Version]] = {}
        self.allow_prereleases_cache: dict[tuple[str, str, str | None, str], bool] = {}
        self.last_graph: dict[str, set[str]] | None = None
        self.incoming_requirements: dict[str, dict[str, tuple[Requirement, ...]]] = {}
        self.domains_internal: dict[str, PackageDomain] = {}
        self.unavailable_requirements: dict[str, Requirement] = {}
        self.warned_missing_extras: set[tuple[str, str]] = set()
        self.reconsidering: set[
            tuple[
                str,
                tuple[tuple[str, str, tuple[str, ...], str | None, str | None], ...],
            ]
        ] = set()
        self.failed_search_states: set[tuple[object, ...]] = set()
        self.candidate_state_keys: dict[int, tuple[str, str, str, str]] = {}
        self.requirement_state_keys: dict[int, RequirementStateKey] = {}
        self.candidate_dependency_groups: dict[
            tuple[int, frozenset[str]],
            tuple[tuple[str, tuple[Requirement, ...]], ...],
        ] = {}
        self.backtrack_count = 0
        self.root_incompatibilities: set[tuple[int, frozenset[str]]] = set()
        self.root_unsatisfiable_domains: set[tuple[object, ...]] = set()
        self.seen_candidate_conflicts: set[tuple[int, frozenset[str]]] = set()
        self.root_incompatibility_hits = 0
        self.last_conflict_was_root = False
        self.assignment_levels: dict[Assignment, int] = {}
        self.backjump_conflict: LearnedIncompatibility | None = None
        self.last_candidate_conflict: LearnedIncompatibility | None = None
        self.package_ids: dict[str, int] = {}
        self.package_names_internal: list[str] = []
        self.candidate_ids: dict[tuple[int, Version, str], int] = {}
        self.candidate_assignment_cache: dict[
            tuple[int, frozenset[str]],
            Assignment,
        ] = {}
        self.conflict_activity: list[int] = []
        self.conflict_activity_bumps = 0
        self.learned_clause_limit = 4096
        self.resolution_seed: dict[str, tuple[str, str]] = {}
        self.learned_incompatibilities: list[LearnedIncompatibility] = []
        self.learned_incompatibility_terms: set[frozenset[Assignment]] = set()
        self.incompatibility_watches: dict[int, set[int]] = {}
        self.binary_incompatibility_watches: dict[Assignment, set[int]] = {}
        self.learned_non_binary_count = 0
        self.installed_by_name_internal: dict[str, InstalledDistribution] | None = None
        self.debug_internal = bool(os.environ.get("CPIP_RESOLVER_DEBUG"))

    def resolve_plan(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> InstallPlan:
        self.metrics = ResolutionMetrics()
        self.release_frontier.reset()
        started = perf_counter()
        try:
            plan = self.resolve_internal(requirements_input)
        finally:
            self.provider.close()
        self.metrics.resolution_seconds = perf_counter() - started
        frontier_metrics = self.release_frontier.metrics
        self.metrics.catalogs_loaded = frontier_metrics.catalogs_loaded
        self.metrics.catalog_cache_hits = frontier_metrics.catalog_hits
        self.metrics.release_masks_built = frontier_metrics.release_masks_built
        self.metrics.release_intersections = frontier_metrics.release_intersections
        materializer = self.provider.materializer_internal
        if materializer is not None:
            self.metrics.metadata_loads = materializer.metadata_loads
            self.metrics.metadata_cache_hits = materializer.metadata_cache_hits
            self.metrics.metadata_prefetches = materializer.metadata_prefetches
            self.metrics.artifact_materializations = (
                materializer.artifact_materializations
            )
        plan.metrics = self.metrics.as_dict()
        return plan

    def resolve_internal(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> InstallPlan:
        requirements = self.coerce_requirements(requirements_input)
        self.root_requirements = list(requirements)
        self.root_requirement_names = {
            requirement.canonical_name for requirement in requirements
        }
        direct_by_name: dict[str, str] = {}
        for requirement in requirements:
            if not is_direct_requirement(requirement) or requirement.url is None:
                continue
            previous = direct_by_name.get(requirement.canonical_name)
            if previous is not None and not direct_urls_equivalent(
                previous,
                requirement.url,
            ):
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package "
                    "versions have conflicting dependencies.",
                )
            direct_by_name[requirement.canonical_name] = requirement.url
        if self.debug_internal:
            print("Reporter.starting()", file=sys.stdout)
        conflicting_root = (
            None if self.constraints else self.conflicting_root_bounds(requirements)
        )
        if conflicting_root is not None:
            if self.debug_internal:
                print(
                    "conflict is caused by: mutually exclusive root requirements",
                    file=sys.stdout,
                )
            raise ResolutionError(
                f"Cannot install {conflicting_root} because these package versions "
                "have conflicting dependencies.",
            )
        source_requirements, source_requirements_by_url = self.source_requirement_map(
            requirements_input,
        )
        selected: dict[str, WheelCandidate] = {}
        selected_extras: dict[str, frozenset[str]] = {}
        satisfied: dict[str, SatisfiedRequirement] = {}
        graph: dict[str, set[str]] = {"<root>": set()}
        self.unavailable_requirements.clear()
        self.warned_missing_extras.clear()
        self.reconsidering.clear()
        self.failed_search_states.clear()
        self.candidate_state_keys.clear()
        self.requirement_state_keys.clear()
        self.candidate_dependency_groups.clear()
        self.candidate_count_cache.clear()
        self.domain_viability_cache.clear()
        self.version_tables.clear()
        self.version_indexes.clear()
        self.version_masks.clear()
        self.active_version_masks.clear()
        self.allowed_versions_cache.clear()
        self.incoming_requirements.clear()
        self.domains_internal.clear()
        self.root_incompatibilities.clear()
        self.root_unsatisfiable_domains.clear()
        self.seen_candidate_conflicts.clear()
        self.root_incompatibility_hits = 0
        self.last_conflict_was_root = False
        self.assignment_levels.clear()
        self.backjump_conflict = None
        self.last_candidate_conflict = None
        self.package_ids.clear()
        self.package_names_internal.clear()
        self.candidate_ids.clear()
        self.candidate_assignment_cache.clear()
        self.conflict_activity.clear()
        self.conflict_activity_bumps = 0
        self.learned_incompatibilities.clear()
        self.learned_incompatibility_terms.clear()
        self.incompatibility_watches.clear()
        self.binary_incompatibility_watches.clear()
        self.learned_non_binary_count = 0
        for requirement in requirements:
            domain = self.domains_internal.setdefault(
                requirement.canonical_name,
                PackageDomain(),
            )
            domain.roots.append(requirement)
            domain.requirements_internal = None
            domain.constrained_internal = None
            domain.constrained_roots_internal = None
        self.backtrack_count = 0
        kernel_result = None
        if self.kernel_enabled:
            from cpip.resolution.engine.propagation import try_resolve

            kernel_result = try_resolve(
                cast("EngineContext", self),
                requirements,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
        if kernel_result is not None:
            selected.update(kernel_result.selected)
            graph = kernel_result.graph
            resolved = True
        else:
            resolved = SearchLoop.search_internal(
                cast("EngineContext", self),
                requirements,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
        if not resolved:
            if self.unavailable_requirements:
                if self.debug_internal:
                    print(
                        "conflict is caused by unavailable distributions",
                        file=sys.stdout,
                    )
                missing = sorted(
                    self.unavailable_requirements.values(),
                    key=lambda requirement: requirement.canonical_name,
                )
                message = PolicyChecks.no_matching_distribution_message(
                    cast("ConfigurationContext", self),
                    missing[0],
                )
                if missing[0].canonical_name not in self.root_requirement_names:
                    print(
                        "Additionally, some packages in these conflicts have no "
                        "matching distributions available for your environment:\n"
                        f"    {missing[0].canonical_name}\n",
                    )
                    raise ResolutionError(f"ResolutionImpossible: {message}")
                raise DistributionNotFound(message)
            for root in requirements:
                if root.url is None:
                    continue
                for candidate in self.provider.find_candidates(root):
                    constraints = self.constraints_by_name.get(
                        candidate.canonical_name,
                        (),
                    )
                    if (
                        any(
                            not constraint.is_satisfied_by(
                                candidate.version,
                                allow_prereleases=True,
                            )
                            for constraint in constraints
                        )
                        and candidate.source_kind in SOURCE_KINDS
                    ):
                        raise ResolutionError(
                            f"Cannot install {candidate.name} {candidate.version} "
                            "because it conflicts with a constraint.",
                        )
            detail = "; ".join(self.conflicts[-10:]) or "requirements are unsatisfiable"
            if self.debug_internal:
                print(f"conflict is caused by: {detail}", file=sys.stdout)
            raise ResolutionError(
                "package versions have conflicting dependencies: " + detail,
            )
        ordered = self.installation_order(selected, graph)
        candidates = [selected[name] for name in ordered]
        if self.compute_source_hashes:
            from cpip.resolution.engine.output import finalize_candidates

            candidates = finalize_candidates(candidates, self.finalize_source_hashes)
        plan = InstallPlan(
            candidates=candidates,
            graph=graph,
            conflicts=list(self.conflicts),
            satisfied=[
                satisfied[name] for name in sorted(satisfied) if name not in selected
            ],
            metrics=self.metrics.as_dict(),
        )
        self.resolution_seed = {
            name: (str(candidate.version), candidate.source_url or "")
            for name, candidate in selected.items()
        }
        self.last_graph = graph
        return plan

    def conflicting_root_bounds(
        self,
        requirements: list[Requirement],
    ) -> str | None:
        """Return a root project whose active version bounds cannot intersect."""
        requirements_by_name: dict[str, list[Requirement]] = {}
        direct_names: set[str] = set()
        for requirement in requirements:
            if requirement.url is not None:
                direct_names.add(requirement.canonical_name)
                continue
            if requirement.marker is not None and not marker_applies(
                requirement.marker,
                extras=requirement.extras,
            ):
                continue
            requirements_by_name.setdefault(requirement.canonical_name, []).append(
                requirement,
            )

        for name, roots in requirements_by_name.items():
            if name in direct_names:
                continue
            active = [
                *roots,
                *(
                    constraint
                    for constraint in self.constraints_by_name.get(name, ())
                    if constraint.marker is None
                    or marker_applies(constraint.marker, extras=constraint.extras)
                ),
            ]
            lower: tuple[Version, bool] | None = None
            upper: tuple[Version, bool] | None = None
            for requirement in active:
                requirement_lower, requirement_upper = requirement.specifier.bounds()
                if requirement_lower is not None and (
                    lower is None
                    or requirement_lower[0] > lower[0]
                    or (
                        requirement_lower[0] == lower[0]
                        and not requirement_lower[1]
                        and lower[1]
                    )
                ):
                    lower = requirement_lower
                if requirement_upper is not None and (
                    upper is None
                    or requirement_upper[0] < upper[0]
                    or (
                        requirement_upper[0] == upper[0]
                        and not requirement_upper[1]
                        and upper[1]
                    )
                ):
                    upper = requirement_upper
            if lower is None or upper is None:
                continue
            if lower[0] > upper[0] or (
                lower[0] == upper[0] and not (lower[1] and upper[1])
            ):
                return name
            if lower[0] == upper[0] and not all(
                bound_requirement.is_satisfied_by(lower[0], allow_prereleases=True)
                for bound_requirement in active
            ):
                return name
        return None

    @staticmethod
    def finalize_source_hashes(candidate: WheelCandidate) -> WheelCandidate:
        from cpip.resolution.engine.output import finalize_source_hashes

        return finalize_source_hashes(candidate)

    def get_installation_order(
        self,
        requirement_set: RequirementSet[InstallRequirement],
        *,
        graph: dict[str, set[str]] | None = None,
    ) -> list[InstallRequirement]:
        from cpip.resolution.engine.output import get_installation_order

        return get_installation_order(self, requirement_set, graph=graph)

    def get_topological_weights(
        self,
        graph: dict[str, set[str]],
        requirement_keys: set[str],
    ) -> dict[str, int]:
        return topological_weights(graph, requirement_keys)

    def resolve_requirement_set(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> RequirementSet[InstallRequirement]:
        from cpip.resolution.engine.input.coercion import resolve_requirement_set

        return resolve_requirement_set(self, requirements_input)

    def coerce_requirements(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> list[Requirement]:
        from cpip.resolution.engine.input.coercion import coerce_requirements

        return coerce_requirements(self, requirements_input)

    def source_requirement_map(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> tuple[dict[str, InstallRequirement], dict[str, InstallRequirement]]:
        from cpip.resolution.engine.input.coercion import source_requirement_map

        return source_requirement_map(self, requirements_input)

    def installation_order(
        self,
        selected: Collection[str],
        graph: dict[str, set[str]],
    ) -> list[str]:
        from cpip.resolution.engine.output import installation_order

        return installation_order(selected, graph)
