"""Conflict learning and version-domain reasoning for dependency resolution."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cpip.core.packaging import Requirement, Version, canonicalize_name, marker_applies
from cpip.core.wheel import WheelCandidate
from cpip.resolution.algorithms import (
    exact_pinned_version,
    specifier_intersection_is_empty,
)
from cpip.resolution.resolver_internals.state.domains import (
    Assignment,
    INCREMENTAL_STATE_KEY_THRESHOLD,
    LearnedIncompatibility,
    PackageDomain,
)

SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))

if TYPE_CHECKING:
    from cpip.resolution.resolver_internals.context import ResolverContext


class ResolverConflicts:
    """Conflict learning and version-domain reasoning operations."""

    @staticmethod
    def candidate_incompatibility_key(
        candidate: WheelCandidate, extras: frozenset[str]
    ) -> tuple[int, frozenset[str]]:
        return id(candidate), extras

    def grouped_candidate_dependencies(
        self: ResolverContext, candidate: WheelCandidate, extras: frozenset[str]
    ) -> tuple[tuple[str, tuple[Requirement, ...]], ...]:
        key = id(candidate), extras
        cached = self.candidate_dependency_groups.get(key)
        if cached is not None:
            return cached
        grouped: dict[str, list[Requirement]] = {}
        for dependency in candidate.dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            grouped.setdefault(dependency.canonical_name, []).append(
                self.apply_constraints(dependency)
            )
        result = tuple(
            (name, tuple(dependencies)) for name, dependencies in grouped.items()
        )
        self.candidate_dependency_groups[key] = result
        return result

    def candidate_dependencies_conflict(
        self: ResolverContext,
        candidate: WheelCandidate,
        *,
        extras: frozenset[str],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> bool:
        self.last_conflict_was_root = False
        grouped = self.grouped_candidate_dependencies(candidate, extras)
        for target, dependencies in grouped:
            for constrained_dependency in dependencies:
                selected_target = selected.get(target)
                domain = self.domains_internal.get(target)
                root_conflict = bool(
                    domain
                    and domain.constrained_roots(self.apply_constraints)
                    and self.dependency_domain_conflicts(
                        constrained_dependency,
                        domain.constrained_roots(self.apply_constraints),
                    )
                )
                if (
                    selected_target is not None
                    and not root_conflict
                    and not constrained_dependency.is_satisfied_by(
                        selected_target.version,
                        allow_prereleases=self.allow_prereleases_internal(
                            constrained_dependency
                        ),
                    )
                ):
                    self.learn_candidate_pair_incompatibility(
                        candidate,
                        extras,
                        selected_target,
                        selected_extras.get(target, frozenset()),
                        decision_level=len(selected),
                    )
                if (
                    self.candidate_cache_key(constrained_dependency)
                    in self.root_unsatisfiable_domains
                ):
                    self.root_incompatibilities.add(
                        self.candidate_incompatibility_key(candidate, extras)
                    )
                    self.bump_conflict_activity(candidate.canonical_name, target)
                    self.last_conflict_was_root = True
                    return True
        active_targets = self.domains_internal.keys() & {
            target for target, _ in grouped
        }
        if not active_targets:
            return False
        for target, dependencies in grouped:
            if target not in active_targets:
                continue
            domain = self.domains_internal[target]
            constrained_active = domain.constrained_requirements(self.apply_constraints)
            for constrained_dependency in dependencies:
                if not self.dependency_domain_conflicts(
                    constrained_dependency,
                    constrained_active,
                    domain=domain,
                ):
                    continue
                self.learn_watched_incompatibility(
                    candidate,
                    extras,
                    constrained_dependency,
                    domain,
                    selected,
                    selected_extras,
                )
                self.bump_conflict_activity(candidate.canonical_name, target)
                incompatibility_key = self.candidate_incompatibility_key(
                    candidate, extras
                )
                if incompatibility_key in self.seen_candidate_conflicts:
                    constrained_roots = domain.constrained_roots(self.apply_constraints)
                    if constrained_roots and self.dependency_domain_conflicts(
                        constrained_dependency, constrained_roots
                    ):
                        self.root_incompatibilities.add(incompatibility_key)
                        self.last_conflict_was_root = True
                else:
                    self.seen_candidate_conflicts.add(incompatibility_key)
                return True
        return False

    def learn_candidate_pair_incompatibility(
        self: ResolverContext,
        candidate: WheelCandidate,
        extras: frozenset[str],
        selected_candidate: WheelCandidate,
        selected_extras: frozenset[str],
        decision_level: int,
    ) -> None:
        """Learn the cheapest nogood when a dependency meets a selected value.

        The regular watched-incompatibility path learns from incoming
        requirements. A direct contradiction with the already selected
        candidate is stronger and can be represented by two assignment terms,
        avoiding repeated domain scans on later branches.
        """

        terms = frozenset(
            (
                self.candidate_assignment(candidate, extras),
                self.candidate_assignment(selected_candidate, selected_extras),
            )
        )
        if len(terms) < 2 or terms in self.learned_incompatibility_terms:
            return
        candidate_term = self.candidate_assignment(candidate, extras)
        # This candidate is being rejected at the current trail level.  A
        # previous branch may have assigned the same candidate identity at a
        # different level, so do not reuse that historical level.
        self.assignment_levels[candidate_term] = decision_level
        other_term = next(term for term in terms if term != candidate_term)
        watches = candidate_term[0], other_term[0]
        decision_levels = tuple(
            sorted(
                (
                    (
                        term,
                        self.assignment_levels.get(
                            term, decision_level if term == candidate_term else 0
                        ),
                    )
                    for term in terms
                ),
                key=lambda item: (item[0][0], item[0][1], tuple(sorted(item[0][2]))),
            )
        )
        self.record_learned_incompatibility(
            LearnedIncompatibility(terms, watches, decision_levels)
        )

    def derive_candidate_domain_conflict(
        self: ResolverContext,
        conflicts: list[LearnedIncompatibility],
        blocked_package_id: int,
    ) -> LearnedIncompatibility | None:
        """Resolve fresh candidate nogoods into a domain-level conflict.

        The blocked package's candidate terms are eliminated because exactly
        one candidate can satisfy the pending package requirement. The
        resulting clause is ephemeral until the search driver proves it can
        safely backjump; it is not inserted into the watched-conflict index.
        """

        levels: dict[Assignment, int] = {}
        for conflict in conflicts:
            for term, level in conflict.decision_levels:
                if term[0] != blocked_package_id:
                    levels.setdefault(term, level)
        if not levels:
            return None
        terms = frozenset(levels)
        package_ids = tuple(dict.fromkeys(term[0] for term in terms))
        watches = (
            package_ids[0],
            package_ids[1] if len(package_ids) > 1 else package_ids[0],
        )
        decision_levels = tuple(
            sorted(
                levels.items(),
                key=lambda item: (item[0][0], item[0][1], tuple(sorted(item[0][2]))),
            )
        )
        return LearnedIncompatibility(terms, watches, decision_levels)

    def package_id_internal(self: ResolverContext, name: str) -> int:
        """Return a stable compact package id for conflict clauses."""
        package_id = self.package_ids.get(name)
        canonical_name = name
        if package_id is None:
            canonical_name = canonicalize_name(name)
            package_id = self.package_ids.get(canonical_name)
        if package_id is None:
            package_id = len(self.package_names_internal)
            self.package_ids[canonical_name] = package_id
            self.package_names_internal.append(canonical_name)
            self.conflict_activity.append(0)
        return package_id

    def candidate_assignment(
        self: ResolverContext, candidate: WheelCandidate, extras: frozenset[str]
    ) -> Assignment:
        package_id = self.package_id_internal(candidate.canonical_name)
        identity = (
            package_id,
            candidate.version,
            candidate.source_url or os.fspath(candidate.path),
        )
        candidate_id = self.candidate_ids.get(identity)
        if candidate_id is None:
            candidate_id = len(self.candidate_ids)
            self.candidate_ids[identity] = candidate_id
        return package_id, candidate_id, extras

    def bump_conflict_activity(self: ResolverContext, *names: str) -> None:
        self.conflict_activity_bumps += 1
        for name in names:
            package_id = self.package_id_internal(name)
            self.conflict_activity[package_id] += 1

    def record_learned_incompatibility(
        self: ResolverContext, incompatibility: LearnedIncompatibility
    ) -> None:
        self.learned_incompatibilities.append(incompatibility)
        self.learned_incompatibility_terms.add(incompatibility.terms)
        self.last_candidate_conflict = incompatibility
        incompatibility_id = len(self.learned_incompatibilities) - 1
        for package_id in incompatibility.watches:
            self.incompatibility_watches.setdefault(package_id, set()).add(
                incompatibility_id
            )
        non_binary_count = sum(
            len(clause.terms) != 2 for clause in self.learned_incompatibilities
        )
        if non_binary_count > self.learned_clause_limit:
            self.compact_learned_incompatibilities()

    def compact_learned_incompatibilities(self: ResolverContext) -> None:
        """Retain useful learned clauses while keeping watch indexes compact."""
        clauses = self.learned_incompatibilities
        limit = self.learned_clause_limit
        protected = [clause for clause in clauses if len(clause.terms) == 2]
        remaining = [clause for clause in clauses if len(clause.terms) != 2]
        if len(remaining) <= limit:
            return
        remaining.sort(key=lambda clause: (clause.activity, clause.last_used))
        kept = protected + remaining[-limit:]
        evicted = len(clauses) - len(kept)
        self.learned_incompatibilities[:] = kept
        self.learned_incompatibility_terms.clear()
        self.incompatibility_watches.clear()
        for incompatibility_id, clause in enumerate(kept):
            self.learned_incompatibility_terms.add(clause.terms)
            for package_id in clause.watches:
                self.incompatibility_watches.setdefault(package_id, set()).add(
                    incompatibility_id
                )
        if self.metrics.enabled:
            self.metrics.learned_clause_evictions += evicted

    def learn_watched_incompatibility(
        self: ResolverContext,
        candidate: WheelCandidate,
        extras: frozenset[str],
        dependency: Requirement,
        domain: PackageDomain,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> None:
        candidate_term = self.candidate_assignment(candidate, extras)
        # The candidate is not selected yet.  Its assignment level may be
        # stale from an earlier branch, so always record the level for this
        # conflict rather than retaining historical trail state.
        self.assignment_levels[candidate_term] = len(selected)
        terms = {candidate_term}
        sources = [source for source in domain.incoming if source in selected]
        if len(sources) > 1:
            roots = tuple(self.apply_constraints(item) for item in domain.roots)
            requirements_by_source = {
                source: tuple(
                    self.apply_constraints(item) for item in domain.incoming[source]
                )
                for source in sources
            }
            exact_sources = self.minimal_exact_conflict_sources(
                dependency, roots, requirements_by_source, sources
            )
            if exact_sources is not None:
                sources = exact_sources
            else:
                necessary_sources = list(sources)
                for source in sources:
                    without_source = tuple(
                        requirement
                        for other in necessary_sources
                        if other != source
                        for requirement in requirements_by_source[other]
                    )
                    if self.dependency_domain_conflicts(
                        dependency, (*roots, *without_source)
                    ):
                        necessary_sources.remove(source)
                sources = necessary_sources
        for source in sources:
            selected_candidate = selected.get(source)
            if selected_candidate is not None:
                terms.add(
                    self.candidate_assignment(
                        selected_candidate,
                        selected_extras.get(source, frozenset()),
                    )
                )
        frozen_terms = frozenset(terms)
        if len(frozen_terms) < 2 or frozen_terms in self.learned_incompatibility_terms:
            return
        other_watch = next(term[0] for term in frozen_terms if term != candidate_term)
        watches = candidate_term[0], other_watch
        decision_levels = tuple(
            sorted(
                (
                    (term, self.assignment_levels.get(term, len(selected)))
                    for term in frozen_terms
                ),
                key=lambda item: (item[0][0], item[0][1], tuple(sorted(item[0][2]))),
            )
        )
        self.record_learned_incompatibility(
            LearnedIncompatibility(frozen_terms, watches, decision_levels)
        )

    @staticmethod
    def minimal_exact_conflict_sources(
        dependency: Requirement,
        roots: tuple[Requirement, ...],
        requirements_by_source: dict[str, tuple[Requirement, ...]],
        sources: list[str],
    ) -> list[str] | None:
        dependency_version = exact_pinned_version(dependency)
        if dependency_version is not None:
            if any(
                not requirement.is_satisfied_by(dependency_version)
                for requirement in roots
            ):
                return []
            conflicting = next(
                (
                    source
                    for source in reversed(sources)
                    if any(
                        not requirement.is_satisfied_by(dependency_version)
                        for requirement in requirements_by_source[source]
                    )
                ),
                None,
            )
            return [conflicting] if conflicting is not None else None
        for requirement in roots:
            version = exact_pinned_version(requirement)
            if version is not None and not dependency.is_satisfied_by(version):
                return []
        conflicting = next(
            (
                source
                for source in reversed(sources)
                if any(
                    (version := exact_pinned_version(requirement)) is not None
                    and not dependency.is_satisfied_by(version)
                    for requirement in requirements_by_source[source]
                )
            ),
            None,
        )
        return [conflicting] if conflicting is not None else None

    def violates_watched_incompatibility(
        self: ResolverContext,
        candidate: WheelCandidate,
        extras: frozenset[str],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> bool:
        if not self.learned_incompatibilities:
            return False
        candidate_term = self.candidate_assignment(candidate, extras)
        for incompatibility_id in self.incompatibility_watches.get(
            candidate_term[0], ()
        ):
            incompatibility = self.learned_incompatibilities[incompatibility_id]
            if candidate_term not in incompatibility.terms:
                continue
            incompatibility.activity += 1
            incompatibility.last_used = self.conflict_activity_bumps
            if len(incompatibility.terms) == 2:
                other_term = next(
                    term for term in incompatibility.terms if term != candidate_term
                )
                selected_candidate = selected.get(
                    self.package_names_internal[other_term[0]]
                )
                if selected_candidate is None or self.candidate_assignment(
                    selected_candidate,
                    selected_extras.get(
                        self.package_names_internal[other_term[0]], frozenset()
                    ),
                ) != other_term:
                    continue
                self.bump_conflict_activity(
                    self.package_names_internal[candidate_term[0]],
                    self.package_names_internal[other_term[0]],
                )
                return True
            if all(
                term == candidate_term
                or (
                    (
                        selected_candidate := selected.get(
                            self.package_names_internal[term[0]]
                        )
                    )
                    is not None
                    and self.candidate_assignment(
                        selected_candidate,
                        selected_extras.get(
                            self.package_names_internal[term[0]], frozenset()
                        ),
                    )
                    == term
                )
                for term in incompatibility.terms
            ):
                self.bump_conflict_activity(
                    *(
                        self.package_names_internal[term[0]]
                        for term in incompatibility.terms
                    )
                )
                return True
        return False

    def dependency_domain_conflicts(
        self: ResolverContext,
        dependency: Requirement,
        active: tuple[Requirement, ...],
        *,
        domain: PackageDomain | None = None,
    ) -> bool:
        dependency_version = exact_pinned_version(dependency)
        if (
            dependency_version is not None
            and domain is not None
            and len(active) >= INCREMENTAL_STATE_KEY_THRESHOLD
        ):
            versions = self.version_table(dependency)
            active_mask = self.domain_version_mask(domain)
            if versions is not None and active_mask is not None:
                try:
                    version_index = versions.index(dependency_version)
                except ValueError:
                    pass
                else:
                    return not active_mask & (1 << version_index)
        if dependency_version is not None and any(
            not requirement.is_satisfied_by(dependency_version)
            for requirement in active
        ):
            return True
        active_versions = tuple(exact_pinned_version(item) for item in active)
        if any(
            version is not None and not dependency.is_satisfied_by(version)
            for version in active_versions
        ):
            return True
        if dependency_version is not None or any(
            version is not None for version in active_versions
        ):
            return False
        requirements = (dependency, *active)
        if any(requirement.url is not None for requirement in requirements):
            return False
        if specifier_intersection_is_empty(requirements):
            return True
        version_mask = self.requirements_version_mask(requirements)
        if version_mask is not None:
            return version_mask == 0
        domain_key = (
            dependency.canonical_name,
            tuple(sorted(str(item.specifier) for item in requirements)),
        )
        viable = self.domain_viability_cache.get(domain_key)
        if viable is None:
            viable = any(
                all(
                    requirement.is_satisfied_by(
                        summary.version,
                        allow_prereleases=True,
                    )
                    for requirement in requirements
                )
                for summary in self.provider.available_versions(dependency)
            )
            self.domain_viability_cache[domain_key] = viable
        return not viable

    def version_table(
        self: ResolverContext, requirement: Requirement
    ) -> tuple[Version, ...] | None:
        if requirement.url is not None:
            return None
        name = requirement.canonical_name
        cached = self.version_tables.get(name)
        if cached is not None:
            return cached
        summaries = self.provider.available_versions(requirement)
        versions = tuple(dict.fromkeys(summary.version for summary in summaries))
        self.version_tables[name] = versions
        return versions

    def requirement_version_mask(
        self: ResolverContext,
        requirement: Requirement,
        versions: tuple[Version, ...],
        *,
        allow_prereleases: bool,
    ) -> int:
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            allow_prereleases,
        )
        cached = self.version_masks.get(key)
        if cached is not None:
            return cached
        mask = sum(
            1 << index
            for index, version in enumerate(versions)
            if requirement.is_satisfied_by(version, allow_prereleases=allow_prereleases)
        )
        self.version_masks[key] = mask
        return mask

    def requirements_version_mask(
        self: ResolverContext, requirements: tuple[Requirement, ...]
    ) -> int | None:
        if not requirements or any(item.url is not None for item in requirements):
            return None
        versions = self.version_table(requirements[0])
        if versions is None:
            return None
        parts = tuple(
            (
                str(requirement.specifier),
                self.allow_prereleases_internal(requirement),
            )
            for requirement in requirements
        )
        key = requirements[0].canonical_name, tuple(sorted(parts))
        cached = self.active_version_masks.get(key)
        if cached is not None:
            return cached
        mask = (1 << len(versions)) - 1
        for requirement, (_, allow_prereleases) in zip(requirements, parts):
            requirement_mask = self.requirement_version_mask(
                requirement,
                versions,
                allow_prereleases=allow_prereleases,
            )
            if not requirement_mask and not allow_prereleases:
                requirement_mask = self.requirement_version_mask(
                    requirement,
                    versions,
                    allow_prereleases=True,
                )
            mask &= requirement_mask
            if not mask:
                break
        self.active_version_masks[key] = mask
        return mask

    def domain_version_mask(self: ResolverContext, domain: PackageDomain) -> int | None:
        if domain.active_version_mask is not None:
            return domain.active_version_mask
        requirements = domain.requirements()
        if not requirements or any(item.url is not None for item in requirements):
            return None
        versions = self.version_table(requirements[0])
        if versions is None:
            return None
        mask = (1 << len(versions)) - 1
        if domain.roots:
            if domain.root_version_mask is None:
                root_mask = self.requirements_version_mask(
                    domain.constrained_roots(self.apply_constraints)
                )
                if root_mask is None:
                    return None
                domain.root_version_mask = root_mask
            mask &= domain.root_version_mask
        for source, incoming in domain.incoming.items():
            incoming_mask = domain.incoming_version_masks.get(source)
            if incoming_mask is None:
                incoming_mask = self.requirements_version_mask(
                    tuple(self.apply_constraints(item) for item in incoming)
                )
                if incoming_mask is None:
                    return None
                domain.incoming_version_masks[source] = incoming_mask
            mask &= incoming_mask
            if not mask:
                break
        domain.active_version_mask = mask
        return mask

    def active_allowed_versions(
        self: ResolverContext, requirement: Requirement
    ) -> tuple[int | None, frozenset[Version] | None]:
        domain = self.domains_internal.get(requirement.canonical_name)
        if domain is None:
            return None, None
        active = domain.constrained_requirements(self.apply_constraints)
        if len(active) < 2:
            return None, None
        mask = self.domain_version_mask(domain)
        versions = self.version_table(requirement)
        if mask is None or versions is None:
            return None, None
        key = requirement.canonical_name, mask
        allowed_versions = self.allowed_versions_cache.get(key)
        if allowed_versions is None:
            allowed_versions = frozenset(
                version for index, version in enumerate(versions) if mask & (1 << index)
            )
            self.allowed_versions_cache[key] = allowed_versions
        return mask, allowed_versions
