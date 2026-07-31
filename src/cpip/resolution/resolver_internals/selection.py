"""Candidate satisfaction, selection, and provider filtering."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING

from cpip.core.errors import HashMismatch, HashMissing
from cpip.core.packaging import Requirement, SpecifierSet, canonicalize_name
from cpip.core.wheel import WheelCandidate, wheel_candidate
from cpip.index.candidate_materialization import CandidateStream, LazyWheelCandidate
from cpip.index.source_locations import looks_like_path_requirement
from cpip.resolution.algorithms import (
    exact_pinned_version,
)
from cpip.resolution.resolver_internals.state.agenda import PendingAgenda
from cpip.resolution.resolver_internals.state.domains import PackageDomain
from cpip.resolution.resolver_internals.state.plans import SatisfiedRequirement
from cpip.resolution.resolver_internals.state.requests import (
    SearchFrame,
    SearchRequest,
)

if TYPE_CHECKING:
    from cpip.resolution.resolver_internals.context import ResolverContext
    from cpip.core.metadata import InstalledDistribution
    from cpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


def iter_installed_distributions() -> list[InstalledDistribution]:
    from cpip.core.metadata import iter_installed_distributions as iterate

    return iterate()


class ResolverSelectionOperations:
    """Candidate satisfaction and selection operations for the resolver."""

    def search_with_satisfied(
        self: ResolverContext,
        requirement: Requirement,
        installed: InstalledDistribution,
        remaining: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> SearchFrame:
        previous = satisfied.get(requirement.canonical_name)
        satisfied[requirement.canonical_name] = SatisfiedRequirement(
            requirement=requirement,
            distribution=installed,
        )
        dependency_pending: list[Requirement] = []
        if not self.no_deps:
            dependencies = installed.dependencies(requirement.extras)
            graph.setdefault(requirement.canonical_name, set())
            for dependency in sorted(
                dependencies,
                key=lambda item: item.canonical_name,
            ):
                graph[requirement.canonical_name].add(dependency.canonical_name)
                dependency_pending.insert(0, dependency)
        branch_checkpoint = remaining.checkpoint()
        remaining.prepend(dependency_pending)
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
        if previous is None:
            satisfied.pop(requirement.canonical_name, None)
        else:
            satisfied[requirement.canonical_name] = previous
        return False

    def satisfied_dependencies_are_consistent(
        self: ResolverContext,
        selected: dict[str, WheelCandidate],
        satisfied: dict[str, SatisfiedRequirement],
    ) -> bool:
        for item in satisfied.values():
            for dependency in item.distribution.dependencies(item.requirement.extras):
                candidate = selected.get(dependency.canonical_name)
                if candidate is not None:
                    if not dependency.is_satisfied_by(
                        candidate.version,
                        allow_prereleases=self.allow_prereleases_internal(dependency),
                    ):
                        return False
                    continue
                existing = satisfied.get(dependency.canonical_name)
                if existing is not None:
                    if not dependency.is_satisfied_by(
                        existing.distribution.version,
                        allow_prereleases=self.allow_prereleases_internal(dependency),
                    ):
                        return False
                    continue
                installed = self.find_installed_internal(dependency.name)
                if installed is None or not dependency.is_satisfied_by(
                    installed.version,
                    allow_prereleases=self.allow_prereleases_internal(dependency),
                ):
                    return False
        return True

    def find_installed_internal(
        self: ResolverContext, name: str
    ) -> InstalledDistribution | None:
        if self.installed_by_name_internal is None:
            self.installed_by_name_internal = {
                distribution.canonical_name: distribution
                for distribution in iter_installed_distributions()
            }
        return self.installed_by_name_internal.get(canonicalize_name(name))

    def candidate_with_extras(
        self: ResolverContext,
        candidate: WheelCandidate,
        requirement: Requirement,
        extras: frozenset[str],
    ) -> WheelCandidate:
        if isinstance(candidate, LazyWheelCandidate):
            if (
                candidate.materializer_internal.dry_run
                and candidate.source_kind in {"sdist", "source-tree", "vcs"}
            ):
                return candidate
            candidate = candidate.materialize()
        try:
            enriched = wheel_candidate(candidate.path, set(extras))
        except (OSError, ValueError):
            enriched = None
        if enriched is not None and enriched.version == candidate.version:
            return candidate.copy_with(
                dependencies=enriched.dependencies,
                provided_extras=enriched.provided_extras,
            )
        extra_requirement = Requirement(
            name=candidate.name,
            specifier=SpecifierSet(f"=={candidate.version}"),
            extras=extras,
            url=requirement.url,
            marker=None,
            raw=requirement.raw,
        )
        for extra_candidate in self.find_candidates_internal(extra_requirement):
            if extra_candidate.version == candidate.version:
                return extra_candidate
        return candidate

    def active_requirements_for(
        self: ResolverContext,
        name: str,
        current: Requirement,
        remaining: Iterable[Requirement],
    ) -> list[Requirement]:
        relevant: list[Requirement] = [current]
        deferred: list[Requirement] = []
        for requirement in remaining:
            if requirement.canonical_name == name:
                relevant.append(requirement)
            else:
                deferred.append(requirement)
        domain = self.domains_internal.get(name)
        if domain is not None:
            relevant.extend(domain.requirements())
        unique: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None], Requirement
        ] = {}
        for requirement in relevant:
            key = (
                requirement.name,
                str(requirement.specifier),
                tuple(sorted(requirement.extras)),
                requirement.url,
                requirement.marker,
            )
            unique.setdefault(key, requirement)
        return list(unique.values()) + deferred

    def add_candidate_dependencies(
        self: ResolverContext, source: str, candidate: WheelCandidate
    ) -> None:
        dependencies_by_name: dict[str, list[Requirement]] = {}
        for dependency in candidate.dependencies:
            target = dependency.canonical_name
            if target == source:
                continue
            dependencies_by_name.setdefault(target, []).append(dependency)
        for target, dependencies in dependencies_by_name.items():
            domain = self.domains_internal.setdefault(target, PackageDomain())
            domain.set_incoming(source, tuple(dependencies))
            self.incoming_requirements[target] = domain.incoming

    def remove_candidate_dependencies(
        self: ResolverContext, source: str, candidate: WheelCandidate
    ) -> None:
        for target in {
            dependency.canonical_name
            for dependency in candidate.dependencies
            if dependency.canonical_name != source
        }:
            incoming = self.incoming_requirements.get(target)
            if incoming is None:
                continue
            domain = self.domains_internal[target]
            domain.remove_incoming(source)
            if not incoming:
                self.incoming_requirements.pop(target, None)
                if not domain.roots:
                    self.domains_internal.pop(target, None)

    def reconsideration_key(
        self: ResolverContext, name: str, requirements: list[Requirement]
    ) -> tuple[str, tuple[tuple[str, str, tuple[str, ...], str, str], ...]]:
        return (
            name,
            tuple(
                sorted(
                    (
                        requirement.name,
                        str(requirement.specifier),
                        tuple(sorted(requirement.extras)),
                        requirement.url or "",
                        requirement.marker or "",
                    )
                    for requirement in requirements
                )
            ),
        )

    def emit_backtracking_message(self: ResolverContext) -> None:
        self.backtrack_count += 1
        if self.metrics.enabled:
            self.metrics.backtracks += 1
        if self.backtrack_count in {1, 8}:
            print("This could take a while.", file=sys.stdout)
        if self.backtrack_count == 13:
            print("If you want to abort this run, press Ctrl + C.", file=sys.stdout)

    def apply_constraints(
        self: ResolverContext, requirement: Requirement
    ) -> Requirement:
        return self.constraint_store.apply(requirement)

    def choose_requirement(
        self: ResolverContext,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
    ) -> tuple[int, Requirement]:
        if len(pending) == 1:
            return pending.first()
        if len(pending.by_name) >= 8:
            first_unresolved: tuple[int, Requirement] | None = None
            direct: tuple[int, Requirement] | None = None
            best: tuple[int, Requirement] | None = None
            best_score: tuple[int, int, int] | None = None
            prefetch: list[Requirement] = []
            unresolved: list[tuple[str, int, Requirement, int, bool]] = []
            for name, entry_ids in pending.by_name.items():
                if name in selected:
                    continue
                entry_id = (
                    next(iter(entry_ids))
                    if len(entry_ids) == 1
                    else min(
                        entry_ids,
                        key=lambda item: pending.entries_internal[item].order,
                    )
                )
                requirement = pending.entries_internal[entry_id].requirement
                order = pending.entries_internal[entry_id].order
                is_direct = requirement.url is not None or looks_like_path_requirement(
                    requirement.raw
                )
                unresolved.append((name, entry_id, requirement, order, is_direct))
                if not is_direct:
                    prefetch.append(requirement)
            self.provider.prefetch_available_versions(tuple(prefetch))
            for name, entry_id, requirement, order, is_direct in unresolved:
                if first_unresolved is None:
                    first_unresolved = entry_id, requirement
                if is_direct:
                    if direct is None or (
                        order
                        < pending.entries_internal[direct[0]].order
                    ):
                        direct = entry_id, requirement
                    continue
                domain = self.domains_internal.get(name)
                if domain is None:
                    candidate_count = 10**9
                elif domain.decision_count is not None:
                    candidate_count = domain.decision_count
                else:
                    constrained = domain.constrained_internal
                    if constrained is None:
                        constrained = domain.constrained_requirements(
                            self.apply_constraints
                        )
                    candidate_count = (
                        10**9
                        if len(constrained) <= 1
                        else self.decision_candidate_count(requirement)
                    )
                score = (
                    candidate_count or 10**9,
                    -self.conflict_activity[self.package_id_internal(name)],
                    order,
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best = entry_id, requirement
            return direct or best or first_unresolved or pending.first()
        first_unresolved: tuple[int, Requirement] | None = None
        best: tuple[int, Requirement] | None = None
        best_score: tuple[int, int, int] | None = None
        queued_names: set[str] = set()
        for index, (entry_id, requirement) in enumerate(pending.iter_entries()):
            name = requirement.canonical_name
            if name in selected:
                continue
            if first_unresolved is None:
                first_unresolved = entry_id, requirement
            if requirement.url is not None or looks_like_path_requirement(
                requirement.raw
            ):
                return entry_id, requirement
            if name in queued_names:
                continue
            queued_names.add(name)
            domain = self.domains_internal.get(name)
            candidate_count = (
                domain.decision_count
                if domain is not None and domain.decision_count is not None
                else self.decision_candidate_count(requirement)
            )
            score = (
                candidate_count or 10**9,
                -self.conflict_activity[self.package_id_internal(name)],
                index,
            )
            if best_score is None or score < best_score:
                best = entry_id, requirement
                best_score = score
        return best or first_unresolved or pending.first()

    def decision_candidate_count(
        self: ResolverContext, requirement: Requirement
    ) -> int:
        domain = self.domains_internal.get(requirement.canonical_name)
        if domain is not None:
            if domain.decision_count is not None:
                return domain.decision_count
            active = domain.constrained_requirements(self.apply_constraints)
            if len(active) > 1:
                active_mask = self.domain_version_mask(domain)
                if active_mask is not None:
                    domain.decision_count = active_mask.bit_count()
                    return domain.decision_count
        cached = self.candidate_count_internal(self.apply_constraints(requirement))
        if domain is not None:
            domain.decision_count = cached
        return cached

    def candidate_count_internal(
        self: ResolverContext, requirement: Requirement
    ) -> int:
        allow_prereleases = self.allow_prereleases_internal(requirement)
        key = (
            requirement.canonical_name,
            requirement.specifier.text_internal,
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.marker,
            allow_prereleases,
        )
        cached = self.candidate_count_cache.get(key)
        if cached is not None:
            return cached
        exact_version = exact_pinned_version(requirement)
        if exact_version is not None:
            summaries = self.provider.available_versions_for(requirement, exact_version)
            count = sum(
                requirement.is_satisfied_by(
                    summary.version,
                    allow_prereleases=allow_prereleases,
                )
                for summary in summaries
            )
            if not count and not allow_prereleases:
                count = sum(
                    requirement.is_satisfied_by(
                        summary.version,
                        allow_prereleases=True,
                    )
                    for summary in summaries
                )
        else:
            summaries = self.provider.matching_versions(
                requirement,
                allow_prereleases=True,
            )
            count = (
                len(summaries)
                if allow_prereleases
                else sum(not summary.version.is_prerelease for summary in summaries)
            )
            if not count and not allow_prereleases:
                count = len(summaries)
        self.candidate_count_cache[key] = count
        return count

    def find_candidates_internal(
        self: ResolverContext,
        requirement: Requirement,
        *,
        source_requirements: dict[str, InstallRequirement] | None = None,
        source_requirements_by_url: dict[str, InstallRequirement] | None = None,
    ) -> CandidateStream:
        active_mask, allowed_versions = self.active_allowed_versions(requirement)
        source_req = (
            source_requirements.get(requirement.canonical_name)
            if source_requirements is not None
            else None
        )
        if source_req is None and source_requirements_by_url is not None:
            source_req = source_requirements_by_url.get(requirement.url or "")
        source_hash_key = (
            tuple(
                sorted(
                    (algorithm, tuple(sorted(digests)))
                    for algorithm, digests in source_req.hash_options.items()
                )
            )
            if source_req is not None
            else ()
        )
        provider_hashes = self.provider.hashes_by_name.get(requirement.canonical_name)
        provider_hash_key = (
            tuple(
                sorted(
                    (algorithm, tuple(sorted(digests)))
                    for algorithm, digests in provider_hashes.allowed_internal.items()
                )
            )
            if provider_hashes is not None
            else ()
        )
        key = (
            *self.candidate_cache_key(requirement),
            active_mask,
            source_hash_key,
            provider_hash_key,
        )
        if key not in self.candidate_cache:
            if self.metrics.enabled:
                self.metrics.candidate_cache_misses += 1
            logger.debug(
                f"candidate cache miss requirement={requirement.raw or requirement.name}"
            )
            if provider_hashes is not None and not provider_hashes.allowed_internal:
                if source_req is not None and "--hash" in str(source_req):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are "
                    "missing from some requirements."
                )
            # An empty active intersection is useful for conflict detection, but
            # must not discard every candidate before the resolver can explain
            # which requirement conflicts with the selected version.
            candidates = (
                self.provider.find_candidates(
                    requirement, allowed_versions=allowed_versions
                )
                if allowed_versions and requirement.url is None
                else self.provider.find_candidates(requirement)
            )
            if (
                source_req is not None
                and provider_hashes is not None
                and not source_req.hash_options
            ):
                if "--hash" in str(source_req):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are "
                    "missing from some requirements."
                )
            allowed = None
            if source_req is not None and source_req.hash_options:
                allowed = {
                    algorithm: {digest.lower() for digest in digests}
                    for algorithm, digests in source_req.hash_options.items()
                }
            if provider_hashes is not None:
                provider_allowed = {
                    algorithm: {digest.lower() for digest in digests}
                    for algorithm, digests in provider_hashes.allowed_internal.items()
                }
                if allowed is None:
                    allowed = provider_allowed
                else:
                    allowed = {
                        algorithm: values & provider_allowed.get(algorithm, set())
                        for algorithm, values in allowed.items()
                    }
                if not any(allowed.values()):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
            if allowed is not None:
                allowed_sha256 = allowed.get("sha256", set())

                def digest(candidate: WheelCandidate) -> str | None:
                    value = (candidate.source_hashes or {}).get("sha256")
                    return value.lower() if value is not None else None

                def keep(candidate: WheelCandidate) -> bool:
                    value = digest(candidate)
                    return value is not None and value in allowed_sha256

                def decisive(candidate: WheelCandidate) -> bool:
                    value = digest(candidate)
                    return value is not None and value in allowed_sha256

                if self.debug_internal:
                    materialized = list(candidates)
                    matches = sum(decisive(candidate) for candidate in materialized)
                    no_digest = sum(
                        digest(candidate) is None for candidate in materialized
                    )
                    discarded = [
                        candidate.source_url or str(candidate.path)
                        for candidate in materialized
                        if not keep(candidate)
                    ]
                    discarded_detail = (
                        f":\n  {chr(10).join(discarded)}" if discarded else ""
                    )
                    total_candidates = len(
                        self.provider.evaluate_links(requirement).accepted
                    )
                    discarded_count = max(total_candidates - matches - no_digest, 0)
                    discarded_text = (
                        "no candidates"
                        if not discarded_count
                        else f"{discarded_count} non-matches"
                    )
                    print(
                        "Checked %d links for project %r against %d hashes "
                        "(%d matches, %d no digest): discarding %s%s"
                        % (
                            total_candidates,
                            requirement.name,
                            len(allowed.get("sha256", set())),
                            matches,
                            no_digest,
                            discarded_text,
                            discarded_detail,
                        )
                    )
                    candidates = CandidateStream(iter(materialized)).prefer(
                        keep, decisive=decisive
                    )
                candidates = candidates.prefer(keep, decisive=decisive)
            self.candidate_cache[key] = candidates
        else:
            if self.metrics.enabled:
                self.metrics.candidate_cache_hits += 1
            logger.debug(
                f"candidate cache hit requirement={requirement.raw or requirement.name}"
            )
        candidates = self.candidate_cache[key]
        if source_req is not None and source_req.hash_options:
            allowed_sha256 = {
                digest.lower() for digest in source_req.hash_options.get("sha256", ())
            }

            def keep_hashed(candidate: WheelCandidate) -> bool:
                digest = (candidate.source_hashes or {}).get("sha256")
                return digest is not None and digest.lower() in allowed_sha256

            def decisive_hashed(candidate: WheelCandidate) -> bool:
                digest = (candidate.source_hashes or {}).get("sha256")
                return digest is not None and digest.lower() in allowed_sha256

            if allowed_sha256:
                candidates = candidates.prefer(keep_hashed, decisive=decisive_hashed)
        logger.debug(
            "candidate cache ready requirement=%s",
            requirement.raw or requirement.name,
        )
        if not self.ignore_requires_python:
            candidates = candidates.prefer(self.candidate_matches_python)
        seed = self.resolution_seed.get(requirement.canonical_name)
        if seed is not None:
            candidates = candidates.prefer(
                lambda candidate: self.candidate_matches_seed(candidate, seed)
            )
        return candidates

    @staticmethod
    def candidate_matches_seed(
        candidate: WheelCandidate, seed: tuple[str, str]
    ) -> bool:
        return (str(candidate.version), candidate.source_url or "") == seed
