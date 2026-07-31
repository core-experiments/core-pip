"""Stateful dependency search and backtracking behavior."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cpip.core.errors import DistributionNotFound, ResolutionError
from cpip.core.packaging import Requirement, Version, marker_applies
from cpip.core.wheel import WheelCandidate
from cpip.resolution.algorithms import (
    best_candidate_internal,
    direct_urls_equivalent,
    is_direct_requirement,
)
from cpip.resolution.resolver_internals.state.agenda import PendingAgenda
from cpip.resolution.resolver_internals.state.domains import (
    RequirementStateKey,
    requirement_state_key,
)
from cpip.resolution.resolver_internals.state.plans import SatisfiedRequirement
from cpip.resolution.resolver_internals.state.requests import (
    SearchFrame,
    SearchRequest,
)
from cpip.resolution.resolver_internals.selection import ResolverSelectionOperations

if TYPE_CHECKING:
    from cpip.resolution.resolver_internals.context import ResolverContext
    from cpip.resolution.req_install import InstallRequirement


class ResolverSearchEngine:
    """Backtracking search operations for the resolver."""

    def search_internal(
        self: ResolverContext,
        pending: list[Requirement],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> bool:
        frames = [
            self.search_frame_internal(
                SearchRequest(
                    PendingAgenda(pending),
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                )
            )
        ]
        result: bool | None = None
        while frames:
            frame = frames[-1]
            try:
                request = frame.send(result) if result is not None else next(frame)
            except StopIteration as completed:
                frames.pop()
                result = completed.value
                continue
            frames.append(self.search_frame_internal(request))
            result = None
        return bool(result)

    def search_frame_internal(
        self: ResolverContext, request: SearchRequest
    ) -> SearchFrame:
        try:
            resolved = yield from self.search_frame_inner(request)
        except BaseException:
            request.pending.rollback(request.checkpoint)
            raise
        if not resolved:
            request.pending.rollback(request.checkpoint)
        return resolved

    def search_frame_inner(
        self: ResolverContext, request: SearchRequest
    ) -> SearchFrame:
        pending = request.pending
        selected = request.selected
        selected_extras = request.selected_extras
        satisfied = request.satisfied
        graph = request.graph
        source_requirements = request.source_requirements
        source_requirements_by_url = request.source_requirements_by_url
        if (
            selected
            and len(pending) <= 1
            and all(
                name == "<root>" or len(dependencies) <= 1
                for name, dependencies in graph.items()
            )
        ):
            return (
                yield from self.search_uncached(
                    pending,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                )
            )
        state: tuple[object, ...] | None = None
        # Small searches are cheap to key and retain the eager behavior used by
        # callers that inspect the memoization hook.  Once the graph is broad,
        # defer key construction until there is a failed state to consult.
        if self.failed_search_states or len(selected) <= 8:
            state = self.search_state_key_internal(
                pending, selected, selected_extras, satisfied, graph
            )
            if state in self.failed_search_states:
                return False
        resolved = yield from self.search_uncached(
            pending,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if not resolved:
            if state is None:
                state = self.search_state_key_internal(
                    pending, selected, selected_extras, satisfied, graph
                )
            assert state is not None
            self.failed_search_states.add(state)
        return resolved

    def search_state_key_internal(
        self: ResolverContext,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph_internal: dict[str, set[str]],
    ) -> tuple[object, ...]:
        def requirement_key(requirement: Requirement) -> RequirementStateKey:
            key = self.requirement_state_keys.get(id(requirement))
            if key is None:
                key = requirement_state_key(requirement)
                self.requirement_state_keys[id(requirement)] = key
            return key

        def candidate_key(candidate: WheelCandidate) -> tuple[str, str, str, str]:
            key = self.candidate_state_keys.get(id(candidate))
            if key is None:
                key = (
                    candidate.canonical_name,
                    str(candidate.version),
                    candidate.source_url or "",
                    os.fspath(candidate.path),
                )
                self.candidate_state_keys[id(candidate)] = key
            return key

        pending_key = pending.state_key()
        selected_key = tuple(
            sorted(
                (
                    *candidate_key(candidate),
                    tuple(sorted(selected_extras.get(name, ()))),
                )
                for name, candidate in selected.items()
            )
        )
        satisfied_key = tuple(
            sorted(
                (
                    name,
                    item.distribution.version,
                    requirement_key(item.requirement),
                )
                for name, item in satisfied.items()
            )
        )
        return pending_key, selected_key, satisfied_key

    def search_uncached(
        self: ResolverContext,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> SearchFrame:
        if not pending:
            return self.satisfied_dependencies_are_consistent(selected, satisfied)
        entry_id, requirement = self.choose_requirement(pending, selected)
        pending.remove(entry_id)
        remaining = pending
        name = requirement.canonical_name
        constrained = self.apply_constraints(requirement)
        graph.setdefault("<root>", set()).add(name)

        if name in satisfied:
            existing = satisfied[name]
            if not constrained.is_satisfied_by(
                existing.distribution.version,
                allow_prereleases=self.allow_prereleases_internal(requirement),
            ):
                return False
            return (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=remaining.checkpoint(),
                )
            )

        if name in selected:
            selected_candidate = selected[name]
            selected_matches_direct = constrained.url is None or direct_urls_equivalent(
                selected_candidate.source_url, constrained.url
            )
            if selected_matches_direct and constrained.is_satisfied_by(
                selected_candidate.version,
                allow_prereleases=self.allow_prereleases_internal(requirement),
            ):
                branch_checkpoint = remaining.checkpoint()
                merged_extras = selected_extras.get(name, frozenset()) | frozenset(
                    constrained.extras
                )
                if merged_extras != selected_extras.get(name, frozenset()):
                    merged_candidate = self.candidate_with_extras(
                        selected_candidate, constrained, merged_extras
                    )
                    self.remove_candidate_dependencies(name, selected_candidate)
                    selected[name] = merged_candidate
                    self.add_candidate_dependencies(name, merged_candidate)
                    selected_extras[name] = merged_extras
                    graph.setdefault(name, set())
                    if not self.no_deps:
                        extra_pending: list[Requirement] = []
                        for dep in sorted(
                            merged_candidate.dependencies,
                            key=lambda item: item.canonical_name,
                        ):
                            if dep.canonical_name in graph[name]:
                                continue
                            graph[name].add(dep.canonical_name)
                            extra_pending.append(dep)
                        remaining.prepend(extra_pending)
                return (
                    yield SearchRequest(
                        remaining,
                        selected,
                        selected_extras,
                        satisfied,
                        graph,
                        source_requirements,
                        source_requirements_by_url,
                        checkpoint=branch_checkpoint,
                    )
                )
            previous_candidate = selected.pop(name)
            self.remove_candidate_dependencies(name, previous_candidate)
            previous_extras = selected_extras.pop(name, frozenset())
            reconsider = self.active_requirements_for(
                name,
                constrained,
                remaining,
            )
            reconsider_key = self.reconsideration_key(name, reconsider)
            if reconsider_key not in self.reconsidering:
                self.reconsidering.add(reconsider_key)
                try:
                    if (
                        yield SearchRequest(
                            PendingAgenda(reconsider),
                            selected,
                            selected_extras,
                            satisfied,
                            graph,
                            source_requirements,
                            source_requirements_by_url,
                        )
                    ):
                        return True
                finally:
                    self.reconsidering.discard(reconsider_key)
            selected[name] = previous_candidate
            self.add_candidate_dependencies(name, previous_candidate)
            if previous_extras:
                selected_extras[name] = previous_extras
            self.conflicts.append(
                f"{constrained.raw or constrained.name} conflicts with selected "
                f"{selected[name].name}=={selected[name].version}"
            )
            return False

        installed = (
            None
            if self.ignore_installed
            else self.find_installed_internal(constrained.name)
        )
        allow_prereleases = self.allow_prereleases_internal(requirement)
        installed_satisfies = installed is not None and constrained.is_satisfied_by(
            installed.version,
            allow_prereleases=True,
        )
        source_requirement = source_requirements.get(name)
        direct_requirement = is_direct_requirement(requirement) and not (
            source_requirement is not None
            and source_requirement.req is not None
            and source_requirement.req.url is None
        )
        upgrade_allowed = self.upgrade_allowed_for(name)
        if (
            installed is not None
            and installed_satisfies
            and not upgrade_allowed
            and not direct_requirement
        ):
            self.warn_missing_installed_extras(constrained, installed)
            if (
                yield from self.search_with_satisfied(
                    constrained,
                    installed,
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                )
            ):
                return True

        self.preflight_hash_requirement(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        candidates = self.find_candidates_internal(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if candidates and (
            name.startswith("file://")
            or (
                requirement.url is not None
                and candidates[0].canonical_name != requirement.canonical_name
            )
        ):
            resolved_name = candidates[0].canonical_name
            graph["<root>"].discard(name)
            self.root_requirement_names.discard(name)
            self.root_requirement_names.add(resolved_name)
            normalized = Requirement(
                name=candidates[0].name,
                specifier=constrained.specifier,
                extras=constrained.extras,
                url=constrained.url,
                marker=constrained.marker,
                raw=constrained.raw,
            )
            branch_checkpoint = remaining.checkpoint()
            remaining.prepend((normalized,))
            return (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=branch_checkpoint,
                )
            )
        best_candidate = best_candidate_internal(
            candidates,
            constrained,
            allow_prereleases=allow_prereleases,
        )
        if best_candidate is None and not allow_prereleases:
            prerelease_candidate = best_candidate_internal(
                candidates,
                constrained,
                allow_prereleases=True,
            )
            if prerelease_candidate is not None:
                allow_prereleases = True
                best_candidate = prerelease_candidate
        if (
            installed is not None
            and installed_satisfies
            and upgrade_allowed
            and not direct_requirement
        ):
            newer = False
            if best_candidate is not None:
                try:
                    installed_version = Version(installed.version)
                except ValueError:
                    newer = True
                else:
                    newer = best_candidate.version > installed_version
            if not newer:
                self.warn_missing_installed_extras(constrained, installed)
                if (
                    yield from self.search_with_satisfied(
                        constrained,
                        installed,
                        remaining,
                        selected,
                        selected_extras,
                        satisfied,
                        graph,
                        source_requirements=source_requirements,
                        source_requirements_by_url=source_requirements_by_url,
                    )
                ):
                    return True
        if not candidates:
            if (
                requirement.canonical_name in self.root_requirement_names
                or requirement.url is not None
            ):
                matching_constraints = self.constraints_by_name.get(
                    requirement.canonical_name, ()
                )
                unconstrained_candidates = self.provider.find_candidates(requirement)
                if matching_constraints and unconstrained_candidates:
                    for constraint in matching_constraints:
                        print(f"The user requested (constraint) {constraint.raw}")
                    if requirement.url is not None:
                        rejected = unconstrained_candidates[0]
                        raise ResolutionError(
                            f"Cannot install {rejected.name} {rejected.version} "
                            "because it conflicts with a constraint."
                        )
                    raise ResolutionError(
                        "ResolutionImpossible: the requirement conflicts with a "
                        "constraint"
                    )
            if requirement.canonical_name not in self.root_requirement_names:
                self.unavailable_requirements[requirement.canonical_name] = constrained
                self.conflicts.append(
                    f"{requirement.raw or requirement.name} has no matching distribution"
                )
                return False
            raise DistributionNotFound(
                self.no_matching_distribution_message(constrained)
            )

        attempted_candidates = 0
        root_rejections = 0
        for candidate in candidates:
            if not constrained.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                continue
            self.validate_candidate_policy(candidate)
            self.validate_candidate_constraints(candidate)
            attempted_candidates += 1
            incompatibility_key: tuple[int, frozenset[str]] | None = None
            if self.root_incompatibilities:
                incompatibility_key = self.candidate_incompatibility_key(
                    candidate, constrained.extras
                )
                if incompatibility_key in self.root_incompatibilities:
                    self.root_incompatibility_hits += 1
                    root_rejections += 1
                    self.emit_backtracking_message()
                    continue
            if self.violates_watched_incompatibility(
                candidate, constrained.extras, selected, selected_extras
            ):
                self.emit_backtracking_message()
                continue
            if self.candidate_dependencies_conflict(
                candidate,
                extras=constrained.extras,
                selected=selected,
                selected_extras=selected_extras,
            ):
                if self.last_conflict_was_root:
                    root_rejections += 1
                self.conflicts.append(
                    f"learned incompatibility: {candidate.name}=={candidate.version} "
                    "introduces contradictory exact dependencies"
                )
                self.emit_backtracking_message()
                continue
            self.warn_missing_candidate_extras(constrained, candidate)
            self.validate_candidate_hashes(
                constrained,
                candidate,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
            selected[name] = candidate
            self.add_candidate_dependencies(name, candidate)
            selected_extras[name] = frozenset(constrained.extras)
            graph.setdefault(name, set())
            branch_checkpoint = remaining.checkpoint()
            if not self.no_deps:
                dependency_pending: list[Requirement] = []
                for dep in sorted(
                    candidate.dependencies,
                    key=lambda item: item.canonical_name,
                ):
                    if not marker_applies(dep.marker, extras=constrained.extras):
                        continue
                    graph[name].add(dep.canonical_name)
                    dependency_pending.append(dep)
                remaining.prepend(dependency_pending)
            satisfied_snapshot = dict(satisfied)
            if (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=branch_checkpoint,
                )
            ):
                return True
            if any(
                self.candidate_cache_key(dependency) in self.root_unsatisfiable_domains
                for _, dependencies in self.grouped_candidate_dependencies(
                    candidate, constrained.extras
                )
                for dependency in dependencies
            ):
                if incompatibility_key is None:
                    incompatibility_key = self.candidate_incompatibility_key(
                        candidate, constrained.extras
                    )
                self.root_incompatibilities.add(incompatibility_key)
                root_rejections += 1
            selected.pop(name, None)
            self.remove_candidate_dependencies(name, candidate)
            selected_extras.pop(name, None)
            satisfied.clear()
            satisfied.update(satisfied_snapshot)
            self.conflicts.append(
                f"learned incompatibility: {candidate.name}=={candidate.version} "
                f"does not satisfy the active dependency set"
            )
            self.emit_backtracking_message()
        if attempted_candidates and root_rejections == attempted_candidates:
            self.root_unsatisfiable_domains.add(self.candidate_cache_key(constrained))
        return False


class ResolverSearch(ResolverSearchEngine, ResolverSelectionOperations):
    """Complete search domain: candidate selection plus backtracking."""
