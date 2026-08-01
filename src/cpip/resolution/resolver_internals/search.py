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
    LearnedIncompatibility,
    RequirementStateKey,
    requirement_state_key,
)
from cpip.resolution.resolver_internals.state.plans import SatisfiedRequirement
from cpip.resolution.resolver_internals.state.requests import (
    SearchFrame,
    SearchFailure,
    SearchRequest,
)
from cpip.resolution.resolver_internals.selection import ResolverSelectionOperations

if TYPE_CHECKING:
    from cpip.resolution.resolver_internals.context import ResolverContext
    from cpip.resolution.req_install import InstallRequirement


class ResolverSearchEngine:
    """Backtracking search operations for the resolver."""

    def should_backjump_after_failure(
        self: ResolverContext,
        learned_start: int,
        decision_level: int,
    ) -> SearchFailure | None:
        conflict = self.backjump_conflict
        if conflict is None and len(self.learned_incompatibilities) > learned_start:
            conflict = self.learned_incompatibilities[-1]
        if conflict is None:
            return None
        self.backjump_conflict = None
        if not conflict.decision_levels:
            return None
        levels = sorted({level for _, level in conflict.decision_levels})
        if len(levels) < 2:
            return None
        # A propagated conflict is actionable only when its highest decision
        # level is the branch that just failed.  If it contains a level from
        # a deeper or already-unwound branch, its stored metadata is not an
        # active conflict at this frame; treating it as one can skip valid
        # candidates and make the result depend on traversal order.
        if levels[-1] != decision_level:
            return None
        target_level = levels[-2]
        if target_level >= decision_level:
            return None
        failure = SearchFailure(conflict, target_level)
        self.backjump_conflict = conflict
        if self.metrics.enabled:
            self.metrics.nonchronological_jumps += 1
        return failure

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
        result: bool | SearchFailure | None = None
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
        if self.metrics.enabled:
            self.metrics.search_frames += 1
        pending = request.pending
        selected = request.selected
        if self.metrics.enabled:
            self.metrics.max_trail_depth = max(
                self.metrics.max_trail_depth, len(selected)
            )
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
        # There is no failed state to consult on the first pass.  Defer key
        # construction until a search actually fails; later branches will
        # still build the key before consulting the populated memo.
        if self.failed_search_states:
            state = self.search_state_key_internal(
                pending, selected, selected_extras, satisfied, graph
            )
            if state in self.failed_search_states:
                if self.metrics.enabled:
                    self.metrics.failed_state_hits += 1
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
                source_url = candidate.source_url or ""
                key = (
                    candidate.canonical_name,
                    str(candidate.version),
                    source_url,
                    # Lazy candidates expose their link URL without building
                    # the artifact.  Only concrete candidates without a URL
                    # need their path to distinguish same-version entries.
                    "" if source_url else os.fspath(candidate.path),
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
        if self.metrics.enabled:
            self.metrics.decisions += 1
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
                        for dep in merged_candidate.dependencies:
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
        if name.startswith("file://") or requirement.url is not None:
            if candidates and (
                name.startswith("file://")
                or candidates[0].canonical_name != requirement.canonical_name
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
        candidate_conflicts: list[LearnedIncompatibility] = []
        all_candidates_conflicted = True
        learned_start = len(self.learned_incompatibilities)
        decision_level = len(selected)
        for candidate in candidates:
            if self.metrics.enabled:
                self.metrics.candidates_considered += 1
            if not constrained.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                all_candidates_conflicted = False
                continue
            self.validate_candidate_policy(candidate)
            self.validate_candidate_constraints(candidate)
            attempted_candidates += 1
            self.last_candidate_conflict = None
            incompatibility_key: tuple[int, frozenset[str]] | None = None
            if self.root_incompatibilities:
                incompatibility_key = self.candidate_incompatibility_key(
                    candidate, constrained.extras
                )
                if incompatibility_key in self.root_incompatibilities:
                    self.root_incompatibility_hits += 1
                    if self.metrics.enabled:
                        self.metrics.root_incompatibility_hits += 1
                    root_rejections += 1
                    all_candidates_conflicted = False
                    self.emit_backtracking_message()
                    continue
            if self.violates_watched_incompatibility(
                candidate, constrained.extras, selected, selected_extras
            ):
                all_candidates_conflicted = False
                self.emit_backtracking_message()
                continue
            if self.candidate_dependencies_conflict(
                candidate,
                extras=constrained.extras,
                selected=selected,
                selected_extras=selected_extras,
            ):
                if self.last_candidate_conflict is None:
                    all_candidates_conflicted = False
                else:
                    candidate_conflicts.append(self.last_candidate_conflict)
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
            seed = self.resolution_seed.get(name)
            if seed is not None and self.candidate_matches_seed(candidate, seed):
                if self.metrics.enabled:
                    self.metrics.resolution_seed_hits += 1
            selected[name] = candidate
            self.assignment_levels[
                self.candidate_assignment(candidate, constrained.extras)
            ] = len(selected) - 1
            self.add_candidate_dependencies(name, candidate)
            selected_extras[name] = frozenset(constrained.extras)
            graph.setdefault(name, set())
            branch_checkpoint = remaining.checkpoint()
            learned_start = len(self.learned_incompatibilities)
            decision_level = len(selected) - 1
            if not self.no_deps:
                dependency_pending: list[Requirement] = []
                for dep in candidate.dependencies:
                    if not marker_applies(dep.marker, extras=constrained.extras):
                        continue
                    graph[name].add(dep.canonical_name)
                    dependency_pending.append(dep)
                if self.metrics.enabled:
                    self.metrics.propagations += len(dependency_pending)
                remaining.prepend(dependency_pending)
            satisfied_snapshot = dict(satisfied) if satisfied else None
            child_result = yield SearchRequest(
                remaining,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements,
                source_requirements_by_url,
                checkpoint=branch_checkpoint,
            )
            if child_result:
                return True
            if isinstance(child_result, SearchFailure):
                self.backjump_conflict = child_result.conflict
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
            candidate_assignment = self.candidate_assignment(
                candidate, constrained.extras
            )
            selected.pop(name, None)
            # Assignment levels describe the active trail only.  Retaining a
            # level after backtracking lets a later learned clause backjump
            # using a different branch's history, making search order affect
            # correctness.
            self.assignment_levels.pop(candidate_assignment, None)
            self.remove_candidate_dependencies(name, candidate)
            selected_extras.pop(name, None)
            if satisfied_snapshot is not None:
                satisfied.clear()
                satisfied.update(satisfied_snapshot)
            self.conflicts.append(
                f"learned incompatibility: {candidate.name}=={candidate.version} "
                f"does not satisfy the active dependency set"
            )
            self.emit_backtracking_message()
            failure = self.should_backjump_after_failure(learned_start, decision_level)
            if failure is not None:
                return failure
        if all_candidates_conflicted and candidate_conflicts:
            derived = self.derive_candidate_domain_conflict(
                candidate_conflicts, self.package_id_internal(name)
            )
            if derived is not None:
                self.backjump_conflict = derived
                failure = self.should_backjump_after_failure(
                    learned_start, decision_level
                )
                if failure is not None:
                    return failure
        if attempted_candidates and root_rejections == attempted_candidates:
            self.root_unsatisfiable_domains.add(self.candidate_cache_key(constrained))
        return False


class ResolverSearch(ResolverSearchEngine, ResolverSelectionOperations):
    """Complete search domain: candidate selection plus backtracking."""
