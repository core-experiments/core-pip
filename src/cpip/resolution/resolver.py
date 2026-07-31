from __future__ import annotations

import logging
import os
import sys
import urllib.parse  # noqa: F401 - compatibility monkeypatch seam
from collections.abc import Iterable
from typing import TYPE_CHECKING

from cpip.core.errors import (
    DistributionNotFound,
    ResolutionError,
)
from cpip.core.packaging import (
    Requirement,
    Version,
    parse_requirement,
)
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import CandidateStream
from cpip.index.provider import CandidateProvider
from cpip.resolution.algorithms import (
    direct_urls_equivalent,
    is_direct_requirement,
    topological_weights,
)
from cpip.resolution.constraints import ConstraintStore
from cpip.resolution.resolver_internals.conflicts import ResolverConflicts
from cpip.resolution.resolver_internals.policy import ResolverChecks
from cpip.resolution.resolver_internals.search import ResolverSearch
from cpip.resolution.resolver_internals.state.domains import (
    Assignment,
    LearnedIncompatibility,
    PackageDomain,
    RequirementStateKey,
)
from cpip.resolution.resolver_internals.state.plans import (
    InstallPlan,
    SatisfiedRequirement,
)

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.resolution.req_install import InstallRequirement
    from cpip.resolution.requirement_set import RequirementSet


logger = logging.getLogger(__name__)

FALSE_VALUES = frozenset((None, "", "0", "false", "False"))
SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))
SOURCE_TREE_OR_VCS_KINDS = frozenset(("source-tree", "vcs"))


class Resolver(
    ResolverSearch,
    ResolverConflicts,
    ResolverChecks,
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
        self.upgrade_strategy = upgrade_strategy
        self.ignore_requires_python = ignore_requires_python
        self.python_version = python_version or ".".join(
            str(part) for part in sys.version_info[:3]
        )
        self.root_requirements: list[Requirement] = []
        self.root_requirement_names: set[str] = set()
        self.conflicts: list[str] = []
        self.candidate_cache: dict[tuple[object, ...], CandidateStream] = {}
        self.candidate_count_cache: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None, bool], int
        ] = {}
        self.domain_viability_cache: dict[tuple[str, tuple[str, ...]], bool] = {}
        self.version_tables: dict[str, tuple[Version, ...]] = {}
        self.version_masks: dict[tuple[str, str, bool], int] = {}
        self.active_version_masks: dict[
            tuple[str, tuple[tuple[str, bool], ...]], int
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
        self.package_ids: dict[str, int] = {}
        self.package_names_internal: list[str] = []
        self.candidate_ids: dict[tuple[int, Version, str], int] = {}
        self.conflict_activity: list[int] = []
        self.learned_incompatibilities: list[LearnedIncompatibility] = []
        self.learned_incompatibility_terms: set[frozenset[Assignment]] = set()
        self.incompatibility_watches: dict[int, set[int]] = {}
        self.installed_by_name_internal: dict[str, InstalledDistribution] | None = None
        self.debug_internal = os.environ.get("CPIP_RESOLVER_DEBUG") not in FALSE_VALUES

    def resolve(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
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
                previous, requirement.url
            ):
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package "
                    "versions have conflicting dependencies."
                )
            direct_by_name[requirement.canonical_name] = requirement.url
        if self.debug_internal:
            print("Reporter.starting()", file=sys.stdout)
        source_requirements, source_requirements_by_url = self.source_requirement_map(
            requirements_input
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
        self.package_ids.clear()
        self.package_names_internal.clear()
        self.candidate_ids.clear()
        self.conflict_activity.clear()
        self.learned_incompatibilities.clear()
        self.learned_incompatibility_terms.clear()
        self.incompatibility_watches.clear()
        for requirement in requirements:
            domain = self.domains_internal.setdefault(
                requirement.canonical_name, PackageDomain()
            )
            domain.roots.append(requirement)
            domain.requirements_internal = None
            domain.constrained_internal = None
            domain.constrained_roots_internal = None
        self.backtrack_count = 0
        if not self.search_internal(
            requirements,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        ):
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
                message = self.no_matching_distribution_message(missing[0])
                if missing[0].canonical_name not in self.root_requirement_names:
                    print(
                        "Additionally, some packages in these conflicts have no "
                        "matching distributions available for your environment:\n"
                        f"    {missing[0].canonical_name}\n"
                    )
                    raise ResolutionError(f"ResolutionImpossible: {message}")
                raise DistributionNotFound(message)
            for root in requirements:
                if root.url is None:
                    continue
                for candidate in self.provider.find_candidates(root):
                    constraints = self.constraints_by_name.get(
                        candidate.canonical_name, ()
                    )
                    if (
                        any(
                            not constraint.is_satisfied_by(
                                candidate.version, allow_prereleases=True
                            )
                            for constraint in constraints
                        )
                        and candidate.source_kind in SOURCE_KINDS
                    ):
                        raise ResolutionError(
                            f"Cannot install {candidate.name} {candidate.version} "
                            "because it conflicts with a constraint."
                        )
            detail = "; ".join(self.conflicts[-10:]) or "requirements are unsatisfiable"
            if self.debug_internal:
                print(f"conflict is caused by: {detail}", file=sys.stdout)
            raise ResolutionError(
                "package versions have conflicting dependencies: " + detail
            )
        ordered = self.installation_order(selected, graph)
        plan = InstallPlan(
            candidates=[
                (
                    self.finalize_source_hashes(selected[name])
                    if self.compute_source_hashes
                    else selected[name]
                )
                for name in ordered
            ],
            graph=graph,
            conflicts=list(self.conflicts),
            satisfied=[
                satisfied[name] for name in sorted(satisfied) if name not in selected
            ],
        )
        self.last_graph = graph
        return plan

    @staticmethod
    def finalize_source_hashes(candidate: WheelCandidate) -> WheelCandidate:
        from cpip.resolution.resolver_internals.outputs import finalize_source_hashes

        return finalize_source_hashes(candidate)

    def get_installation_order(
        self,
        requirement_set: RequirementSet,
        *,
        graph: dict[str, set[str]] | None = None,
    ) -> list[InstallRequirement]:
        from cpip.resolution.resolver_internals.outputs import get_installation_order

        return get_installation_order(self, requirement_set, graph=graph)

    def get_topological_weights(
        self,
        graph: dict[str, set[str]],
        requirement_keys: set[str],
    ) -> dict[str, int]:
        return topological_weights(graph, requirement_keys)

    def resolve_requirement_set(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> RequirementSet:
        from cpip.resolution.resolver_internals.inputs import resolve_requirement_set

        return resolve_requirement_set(self, requirements_input)

    def coerce_requirements(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> list[Requirement]:
        from cpip.resolution.resolver_internals.inputs import coerce_requirements

        return coerce_requirements(self, requirements_input)

    def source_requirement_map(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> tuple[dict[str, InstallRequirement], dict[str, InstallRequirement]]:
        from cpip.resolution.resolver_internals.inputs import source_requirement_map

        return source_requirement_map(self, requirements_input)

    def installation_order(
        self,
        selected: dict[str, WheelCandidate],
        graph: dict[str, set[str]],
    ) -> list[str]:
        from cpip.resolution.resolver_internals.outputs import installation_order

        return installation_order(selected, graph)
