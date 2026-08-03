"""Guarded finite-domain resolution for inexpensive wheel-only workloads.

This module intentionally owns no user-facing error formatting.  It either
returns a valid assignment or declines the workload so the generic resolver
can provide the authoritative result and diagnostics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from cpip.core.packaging import Requirement, Version, marker_applies, parse_requirement
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import LazyWheelCandidate
from cpip.index.source_locations import looks_like_path_requirement

if TYPE_CHECKING:
    from cpip.index.provider import CandidateProvider
    from cpip.resolution.engine.context import EngineContext
    from cpip.resolution.engine.input.models import RequirementInput
    from cpip.resolution.engine.sources.wheelhouse.cache import (
        CatalogRecords,
        CatalogSignatures,
    )
    from cpip.resolution.engine.sources.wheelhouse.models import (
        LocalWheelCandidate,
        LocalWheelRequirement,
    )


class KernelUnsupported(Exception):
    """The workload requires semantics owned by the generic resolver."""


class KernelUnsatisfiable(Exception):
    """The finite kernel found no assignment; generic diagnostics still win."""


_EXTRA_MARKER_RE = re.compile(r"\bextra\b", flags=re.IGNORECASE)
_KERNEL_FAILURE_CACHE_MAX = 128
_KERNEL_MAX_CONFLICTS = 4096
_KERNEL_MAX_ACTIVE_DEPENDENCIES = 64
_KERNEL_LARGE_GRAPH_DOMAINS = 384
_KERNEL_LARGE_GRAPH_MAX_CONFLICTS = 8
_KernelFailureKey = tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    bool,
    str,
]
_kernel_failure_cache: dict[
    _KernelFailureKey,
    CatalogSignatures,
] = {}


@dataclass(slots=True)
class KernelResult:
    selected: dict[str, WheelCandidate]
    graph: dict[str, set[str]]


@dataclass(slots=True)
class _Domain:
    candidates: tuple[WheelCandidate, ...]
    mask: int
    extras: frozenset[str]
    introducer: _Assignment | None
    restrictions: list[tuple[int, _Assignment | None]]
    extra_causes: set[_Assignment]


_Assignment = tuple[str, str, frozenset[str]]
_Clause = frozenset[_Assignment]
_PendingRequirement = tuple[Requirement, _Assignment | None]
_NogoodIndex = dict[tuple[str, str], set[_Clause]]


@dataclass(slots=True)
class _Decision:
    name: str
    choices: tuple[int, ...]
    next_choice: int
    parent_checkpoint: int
    candidate_checkpoint: int
    extras: frozenset[str]
    blockers: dict[int, _Clause] = field(default_factory=dict)


class FiniteDomainKernel:
    """Iterative trail-based search over metadata-lazy candidate catalogs."""

    def __init__(self, resolver: EngineContext) -> None:
        self.resolver = resolver
        self.domains: dict[str, _Domain] = {}
        self.catalogs: dict[str, tuple[WheelCandidate, ...]] = {}
        self.selected: dict[str, WheelCandidate] = {}
        self.selected_extras: dict[str, frozenset[str]] = {}
        self.pending: list[_PendingRequirement] = []
        self.trail: list[tuple[str, object, object]] = []
        self.decisions: list[_Decision] = []
        self.decision_positions: dict[str, int] = {}
        self.dependencies_cache: dict[
            tuple[str, str, frozenset[str]],
            tuple[Requirement, ...],
        ] = {}
        self.requirement_masks: dict[tuple[str, str, bool], int] = {}
        self.learned_nogoods: list[_Clause] = []
        self.learned_nogood_keys: set[_Clause] = set()
        self.pending_clause_checks: list[_Clause] = []
        self.unary_nogoods: _NogoodIndex = {}
        self.binary_nogoods: _NogoodIndex = {}
        self.long_nogoods: _NogoodIndex = {}

    def resolve(
        self,
        requirements: list[Requirement],
        *,
        source_requirements: Mapping[str, RequirementInput],
        source_requirements_by_url: Mapping[str, RequirementInput],
    ) -> KernelResult:
        del source_requirements, source_requirements_by_url
        active_requirements = self.active_requirements(requirements)
        self.pending.extend((requirement, None) for requirement in active_requirements)
        root_shape_checked = False

        while True:
            active_conflict = self.active_selected_nogood()
            if active_conflict is not None:
                conflict_name, clause = active_conflict
                self.fail(
                    len(self.trail),
                    clause,
                    conflict_name=conflict_name,
                )
                continue
            conflict = self.propagate()
            if conflict is not None:
                checkpoint, conflict_name, causes = conflict
                self.fail(checkpoint, causes, conflict_name=conflict_name)
                continue
            if not root_shape_checked:
                self.check_root_shape(active_requirements)
                root_shape_checked = True

            name = self.choose_domain()
            if name is None:
                return KernelResult(
                    self.selected,
                    self.build_graph(active_requirements),
                )
            checkpoint = len(self.trail)
            domain = self.domains[name]
            choices, blockers = self.available_indices(name, domain)
            if not choices:
                causes = self.domain_blocker_clause(name, domain, blockers)
                if causes is None:  # pragma: no cover - internal invariant
                    raise KernelUnsupported
                self.fail(
                    checkpoint,
                    causes,
                    conflict_name=name,
                )
                continue
            decision = _Decision(
                name=name,
                choices=choices,
                next_choice=0,
                parent_checkpoint=checkpoint,
                candidate_checkpoint=len(self.trail),
                extras=domain.extras,
                blockers=blockers,
            )
            self.decision_positions[name] = len(self.decisions)
            self.decisions.append(decision)
            if (clause := self.select(decision)) is not None:
                self.fail(
                    decision.candidate_checkpoint,
                    clause,
                    conflict_name=name,
                )

    def propagate(self) -> tuple[int, str, _Clause] | None:
        """Apply every queued requirement before making another decision."""
        while self.pending:
            checkpoint = len(self.trail)
            entry = self.pending.pop()
            requirement, source = entry
            self.trail.append(("restore_pending", entry, None))
            requirement = self.resolver.apply_constraints(requirement)
            domain = self.add_requirement(requirement, source)
            self.resolver.metrics.propagations += 1
            if not domain.mask:
                return (
                    checkpoint,
                    requirement.canonical_name,
                    self.domain_conflict_causes(domain),
                )

            name = requirement.canonical_name
            selected = self.selected.get(name)
            if selected is None:
                continue
            if not requirement.is_satisfied_by(
                selected.version,
                allow_prereleases=self.resolver.allow_prereleases_internal(
                    requirement,
                ),
            ):
                causes = {self.selected_term(name)}
                if source is not None:
                    causes.add(source)
                return checkpoint, name, frozenset(causes)
            violated = self.merge_selected_extras(name)
            if violated is not None:
                return checkpoint, name, violated
        self.note_trail_depth()
        return None

    def add_requirement(
        self,
        requirement: Requirement,
        source: _Assignment | None,
    ) -> _Domain:
        name = requirement.canonical_name
        domain = self.domains.get(name)
        if domain is None:
            candidates = self.load_candidates(requirement)
            mask = self.requirement_mask(requirement, candidates)
            universe = (1 << len(candidates)) - 1
            domain = _Domain(
                candidates,
                mask,
                frozenset(requirement.extras),
                source,
                [] if mask == universe else [(mask, source)],
                (
                    {source}
                    if source is not None and requirement.extras
                    else set()
                ),
            )
            self.trail.append(("domain", name, None))
            self.domains[name] = domain
            return domain

        merged_extras = domain.extras | frozenset(requirement.extras)
        old_mask = domain.mask
        requirement_mask = self.requirement_mask(requirement, domain.candidates)
        mask = old_mask & requirement_mask
        self.resolver.metrics.release_intersections += 1
        universe = (1 << len(domain.candidates)) - 1
        restriction = (requirement_mask, source)
        if requirement_mask != universe and restriction not in domain.restrictions:
            self.trail.append(("restriction", name, None))
            domain.restrictions.append(restriction)
        if (
            merged_extras != domain.extras
            and source is not None
            and source not in domain.extra_causes
        ):
            self.trail.append(("extra_cause", name, source))
            domain.extra_causes.add(source)
        if merged_extras != domain.extras:
            self.trail.append(("extras", name, domain.extras))
            domain.extras = merged_extras
        if mask != old_mask:
            self.trail.append(("mask", name, old_mask))
            domain.mask = mask
        return domain

    @staticmethod
    def domain_restriction_causes(domain: _Domain) -> _Clause:
        """Return a compact reason for the domain's current candidate mask."""
        universe = (1 << len(domain.candidates)) - 1
        by_source: dict[_Assignment | None, int] = {}
        for allowed, source in domain.restrictions:
            by_source[source] = by_source.get(source, universe) & allowed
        causes = set() if domain.introducer is None else {domain.introducer}
        remaining = universe
        ordered = sorted(
            by_source.items(),
            key=lambda item: (item[0] is not None, item[1].bit_count()),
        )
        for source, allowed in ordered:
            narrowed = remaining & allowed
            if narrowed == remaining:
                continue
            remaining = narrowed
            if source is not None:
                causes.add(source)
            if not remaining:
                break
        return frozenset(causes)

    def domain_conflict_causes(self, domain: _Domain) -> _Clause:
        """Return the assignments whose requirements emptied a domain."""
        if domain.mask:  # pragma: no cover - internal invariant
            raise AssertionError("domain is not empty")
        return self.domain_restriction_causes(domain)

    def requirement_mask(
        self,
        requirement: Requirement,
        candidates: tuple[WheelCandidate, ...],
    ) -> int:
        allow_prereleases = self.resolver.allow_prereleases_internal(requirement)
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            allow_prereleases,
        )
        cached = self.requirement_masks.get(key)
        if cached is not None:
            return cached
        mask = 0
        for index, candidate in enumerate(candidates):
            if requirement.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                mask |= 1 << index
        self.requirement_masks[key] = mask
        self.resolver.metrics.release_masks_built += 1
        return mask

    def load_candidates(self, requirement: Requirement) -> tuple[WheelCandidate, ...]:
        candidates = self.catalogs.get(requirement.canonical_name)
        if candidates is None:
            from cpip.index.candidate_materialization import LazyWheelCandidate

            provider = self.resolver.provider
            records = provider.find_candidate_records(requirement)
            materializer = provider.get_materializer_internal()
            candidates = tuple(
                LazyWheelCandidate(record, requirement, materializer)
                for record in records
            )
            # A release is the decision unit. Artifact ranking is owned by the
            # provider, so retain only its first (best) artifact per version.
            candidates_by_version: dict[Version, WheelCandidate] = {}
            for candidate in candidates:
                candidates_by_version.setdefault(candidate.version, candidate)
            candidates = tuple(candidates_by_version.values())
            self.catalogs[requirement.canonical_name] = candidates
        compatible: list[WheelCandidate] = []
        for candidate in candidates:
            if not self.is_supported_candidate(candidate):
                raise KernelUnsupported
            compatible.append(candidate)
        return tuple(compatible)

    @staticmethod
    def is_supported_candidate(candidate: WheelCandidate) -> bool:
        return candidate.source_kind in {"wheel", "sdist"}

    def select(self, decision: _Decision) -> _Clause | None:
        domain = self.domains[decision.name]
        candidate = domain.candidates[
            decision.choices[decision.next_choice]
        ]
        candidate = self.candidate_with_extras(candidate, domain.extras)
        candidate_term = self.assignment(decision.name, candidate, domain.extras)
        violated = self.violated_nogood(candidate_term)
        if violated is not None:
            return violated
        if (
            not self.resolver.ignore_requires_python
            and not self.resolver.candidate_matches_python(candidate)
        ):
            return frozenset((candidate_term,))
        self.resolver.validate_candidate_policy(candidate)
        self.resolver.validate_candidate_constraints(candidate)
        self.trail.append(("selected", decision.name, None))
        self.selected[decision.name] = candidate
        self.selected_extras[decision.name] = domain.extras
        self.resolver.metrics.decisions += 1
        dependencies = self.dependencies(candidate)
        self.append_pending(dependencies, candidate_term)
        self.note_trail_depth()
        return None

    def candidate_with_extras(
        self,
        candidate: WheelCandidate,
        extras: frozenset[str],
    ) -> WheelCandidate:
        if not isinstance(candidate, LazyWheelCandidate):
            if extras:
                raise KernelUnsupported
            return candidate
        if candidate.requirement_internal.extras == extras:
            return candidate
        requirement = candidate.requirement_internal.copy_with(extras=extras)
        record = candidate.record_internal.copy_with(
            metadata_loader=candidate.materializer_internal.metadata_loader(
                candidate.record_internal,
                requirement,
            ),
        )
        return LazyWheelCandidate(record, requirement, candidate.materializer_internal)

    def merge_selected_extras(
        self,
        name: str,
    ) -> _Clause | None:
        requested = self.selected_extras.get(name, frozenset())
        merged = self.domains[name].extras
        if merged == requested:
            return None
        candidate = self.selected[name]
        enriched = self.candidate_with_extras(candidate, merged)
        assignment = self.assignment(name, enriched, merged)
        violated = self.violated_nogood(assignment)
        if violated is not None:
            return violated
        self.trail.append(
            ("replace_selected", name, (candidate, requested)),
        )
        self.selected[name] = enriched
        self.selected_extras[name] = merged
        dependencies = self.dependencies(enriched)
        self.append_pending(
            dependencies,
            assignment,
        )
        return None

    def append_pending(
        self,
        dependencies: tuple[Requirement, ...],
        source: _Assignment,
    ) -> None:
        if not dependencies:
            return
        entries = tuple((dependency, source) for dependency in dependencies)
        self.trail.append(("pending", entries, None))
        self.pending.extend(entries)

    @staticmethod
    def assignment(
        name: str,
        candidate: WheelCandidate,
        extras: frozenset[str],
    ) -> _Assignment:
        return name, str(candidate.version), extras

    def selected_term(self, name: str) -> _Assignment:
        return self.assignment(
            name,
            self.selected[name],
            self.selected_extras.get(name, frozenset()),
        )

    def term_is_active(self, term: _Assignment) -> bool:
        selected = self.selected.get(term[0])
        return (
            selected is not None
            and str(selected.version) == term[1]
            and term[2] <= self.selected_extras.get(term[0], frozenset())
        )

    def clause_is_active(
        self,
        clause: _Clause,
        candidate_term: _Assignment,
    ) -> bool:
        for term in clause:
            if (
                term[:2] == candidate_term[:2]
                and term[2] <= candidate_term[2]
            ):
                continue
            if not self.term_is_active(term):
                return False
        return True

    def violated_nogood(self, candidate_term: _Assignment) -> _Clause | None:
        key = candidate_term[:2]
        for index in (
            self.unary_nogoods,
            self.binary_nogoods,
            self.long_nogoods,
        ):
            for clause in index.get(key, ()):
                if self.clause_is_active(clause, candidate_term):
                    return clause
        return None

    def active_selected_nogood(self) -> tuple[str, _Clause] | None:
        """Propagate clauses learned after their assignments were selected."""
        while self.pending_clause_checks:
            clause = self.pending_clause_checks.pop()
            if clause not in self.learned_nogood_keys:
                continue
            latest: tuple[int, str] | None = None
            for term in clause:
                if not self.term_is_active(term):
                    break
                position = self.decision_positions.get(term[0])
                if position is None:
                    break
                if latest is None or position > latest[0]:
                    latest = position, term[0]
            else:
                if latest is not None:
                    return latest[1], clause
        return None

    def dependencies(self, candidate: WheelCandidate) -> tuple[Requirement, ...]:
        requested_extras = (
            candidate.requirement_internal.extras
            if isinstance(candidate, LazyWheelCandidate)
            else frozenset()
        )
        key = (
            candidate.canonical_name,
            str(candidate.version),
            frozenset(requested_extras),
        )
        cached = self.dependencies_cache.get(key)
        if cached is not None:
            return cached
        dependencies = tuple(candidate.dependencies)
        active: list[Requirement] = []
        for dependency in dependencies:
            if dependency.url is not None:
                raise KernelUnsupported
            if dependency.marker is not None:
                has_extra_marker = (
                    _EXTRA_MARKER_RE.search(dependency.marker) is not None
                )
                if has_extra_marker and not isinstance(candidate, LazyWheelCandidate):
                    raise KernelUnsupported
                if not has_extra_marker and not self.marker_supported(
                    dependency.marker,
                ):
                    raise KernelUnsupported
                if not marker_applies(
                    dependency.marker,
                    extras=requested_extras,
                ):
                    continue
            active.append(dependency)
        dependencies = tuple(active)
        if len(dependencies) > _KERNEL_MAX_ACTIVE_DEPENDENCIES:
            # Wide candidate fan-out is propagation work rather than search.
            # The generic agenda maintains that shape with substantially less
            # trail and clause state, while the finite kernel is most useful
            # for narrow domains whose conflicts can amortize that machinery.
            raise KernelUnsupported
        self.dependencies_cache[key] = dependencies
        return dependencies

    def check_root_shape(self, requirements: list[Requirement]) -> None:
        """Probe selected root metadata before exploring a transitive branch.

        Metadata is cached by ``dependencies``, so a supported workload pays no
        duplicate parsing when the candidate is selected.  An unsupported wide
        graph can hand off before fail-first ordering explores a narrower root.
        """
        seen: set[str] = set()
        for requirement in requirements:
            name = requirement.canonical_name
            if name in seen:
                continue
            seen.add(name)
            domain = self.domains[name]
            mask = domain.mask
            while mask:
                bit = mask & -mask
                index = bit.bit_length() - 1
                candidate = self.candidate_with_extras(
                    domain.candidates[index],
                    domain.extras,
                )
                if self.resolver.ignore_requires_python or (
                    self.resolver.candidate_matches_python(candidate)
                ):
                    self.dependencies(candidate)
                    break
                mask ^= bit

    @staticmethod
    def marker_supported(marker: str) -> bool:
        return _EXTRA_MARKER_RE.search(marker) is None

    def active_requirements(self, requirements: list[Requirement]) -> list[Requirement]:
        active: list[Requirement] = []
        for requirement in requirements:
            if requirement.url is not None:
                raise KernelUnsupported
            if requirement.marker is not None:
                if not self.marker_supported(requirement.marker):
                    raise KernelUnsupported
                if not marker_applies(requirement.marker):
                    continue
            active.append(requirement)
        return active

    def choose_domain(self) -> str | None:
        best_name: str | None = None
        best_size = 0
        for name, domain in self.domains.items():
            if name in self.selected:
                continue
            size = domain.mask.bit_count()
            if best_name is None or size < best_size:
                best_name = name
                best_size = size
                if size == 1:
                    break
        return best_name

    def available_indices(
        self,
        name: str,
        domain: _Domain,
    ) -> tuple[tuple[int, ...], dict[int, _Clause]]:
        """Unit-propagate active incompatibilities into one candidate domain."""
        result: list[int] = []
        blockers: dict[int, _Clause] = {}
        mask = domain.mask
        while mask:
            bit = mask & -mask
            index = bit.bit_length() - 1
            candidate = domain.candidates[index]
            candidate_term = self.assignment(name, candidate, domain.extras)
            blocker = self.violated_nogood(candidate_term)
            if blocker is None:
                result.append(index)
            else:
                blockers[index] = blocker
            mask ^= bit
        return tuple(result), blockers

    def fail(
        self,
        checkpoint: int,
        causes: _Clause,
        *,
        conflict_name: str,
    ) -> None:
        causes = self.normalize_causes(causes)
        self.record_decision_blocker(causes)
        self.resolver.metrics.conflicts += 1
        conflict_count = self.resolver.metrics.conflicts
        if conflict_count > _KERNEL_MAX_CONFLICTS or (
            conflict_count > _KERNEL_LARGE_GRAPH_MAX_CONFLICTS
            and len(self.domains) > _KERNEL_LARGE_GRAPH_DOMAINS
        ):
            # This kernel is an optional accelerator. Bound adversarial or
            # unexpectedly complex searches, including very large dependency
            # graphs where exact-version clauses stop amortizing their state,
            # and let the authoritative generic resolver continue.
            raise KernelUnsupported
        if self.resolver.debug_internal and (
            conflict_count <= 10 or conflict_count in {100, 1000, 10_000, 100_000}
        ):
            print(
                "finite-domain conflict "
                f"{conflict_count}: package={conflict_name} "
                f"causes={len(causes)} decisions={len(self.decisions)} "
                f"domains={len(self.domains)} clauses={len(self.learned_nogoods)}",
                flush=True,
            )
        if not causes:
            self.rollback(checkpoint)
            raise KernelUnsatisfiable(
                f"{conflict_name}: conflict has no active assignments",
            )
        clause = self.learn_nogood(causes) or causes
        self.resolve_conflict(clause)

    def resolve_conflict(self, clause: _Clause) -> None:
        """Resolve exhausted decisions until an assignment can change.

        A conflict can be newest at a singleton decision. Merely backtracking
        chronologically from there explores unrelated descendants. Resolve the
        singleton's candidate blocker with the causes of its domain instead,
        then continue with the resulting package-level incompatibility.
        """
        while clause:
            target_index = self.latest_clause_decision(clause)
            if target_index is None:
                # At least one term is no longer assigned, so the learned
                # incompatibility has already been satisfied by a rollback.
                return
            target = self.decisions[target_index]
            self.record_decision_blocker(clause)
            skipped = len(self.decisions) - target_index - 1
            self.rollback(target.candidate_checkpoint)
            self.discard_decisions_from(target_index + 1)

            while target.next_choice + 1 < len(target.choices):
                target.next_choice += 1
                self.resolver.emit_backtracking_message()
                rejected = self.select(target)
                if rejected is None:
                    if skipped:
                        self.resolver.metrics.backjumps += 1
                    return
                rejected = self.normalize_causes(rejected)
                self.record_decision_blocker(rejected)
                self.learn_nogood(rejected)
                self.rollback(target.candidate_checkpoint)

            derived = self.exhausted_domain_clause(target)
            if derived is None:  # pragma: no cover - guarded search invariant
                raise KernelUnsupported
            derived = self.normalize_causes(derived)
            self.rollback(target.parent_checkpoint)
            self.discard_decisions_from(target_index)
            if skipped:
                self.resolver.metrics.backjumps += 1
            if not derived:
                raise KernelUnsatisfiable(
                    f"{target.name}: every root-compatible candidate is blocked",
                )
            clause = self.learn_nogood(derived) or derived

        raise KernelUnsatisfiable("conflict resolution produced an empty clause")

    def latest_clause_decision(self, clause: _Clause) -> int | None:
        latest = -1
        for term in clause:
            position = self.decision_positions.get(term[0])
            if position is None:
                return None
            decision = self.decisions[position]
            candidate = self.domains[decision.name].candidates[
                decision.choices[decision.next_choice]
            ]
            if (
                str(candidate.version) != term[1]
                or not term[2] <= self.domains[decision.name].extras
            ):
                return None
            latest = max(latest, position)
        return latest if latest >= 0 else None

    def discard_decisions_from(self, start: int) -> None:
        for decision in self.decisions[start:]:
            self.decision_positions.pop(decision.name, None)
        del self.decisions[start:]

    def record_decision_blocker(self, clause: _Clause) -> None:
        """Retain the clause that rejected one candidate in an active domain."""
        latest: tuple[int, _Decision] | None = None
        for term in clause:
            position = self.decision_positions.get(term[0])
            if position is None:
                continue
            decision = self.decisions[position]
            candidate = self.domains[decision.name].candidates[
                decision.choices[decision.next_choice]
            ]
            if (
                term[1] == str(candidate.version)
                and term[2] <= self.domains[decision.name].extras
                and (latest is None or position > latest[0])
            ):
                latest = position, decision
        if latest is not None:
            decision = latest[1]
            candidate_index = decision.choices[decision.next_choice]
            decision.blockers[candidate_index] = clause

    def normalize_causes(self, causes: _Clause) -> _Clause:
        """Resolve extras-expanded assignments to their decision reasons."""
        normalized: set[_Assignment] = set()
        pending = list(causes)
        seen: set[_Assignment] = set()
        while pending:
            term = pending.pop()
            if term in seen:
                continue
            seen.add(term)
            position = self.decision_positions.get(term[0])
            if position is None:
                normalized.add(term)
                continue
            decision = self.decisions[position]
            candidate = self.domains[decision.name].candidates[
                decision.choices[decision.next_choice]
            ]
            if str(candidate.version) != term[1] or term[2] <= decision.extras:
                normalized.add(term)
                continue
            normalized.add((term[0], term[1], decision.extras))
            for source in self.domains[term[0]].extra_causes:
                if source[:2] == term[:2]:
                    continue
                pending.append(source)
        return frozenset(normalized)

    def exhausted_domain_clause(self, decision: _Decision) -> _Clause | None:
        """Resolve candidate blockers into a package-level conflict clause."""
        domain = self.domains[decision.name]
        return self.domain_blocker_clause(decision.name, domain, decision.blockers)

    def domain_blocker_clause(
        self,
        name: str,
        domain: _Domain,
        blockers: Mapping[int, _Clause],
    ) -> _Clause | None:
        """Resolve blockers for every allowed release into one incompatibility."""
        causes = set(self.domain_restriction_causes(domain))
        mask = domain.mask
        while mask:
            bit = mask & -mask
            index = bit.bit_length() - 1
            candidate = domain.candidates[index]
            candidate_term = self.assignment(
                name,
                candidate,
                domain.extras,
            )
            blocker = blockers.get(index)
            if blocker is None:
                blocker = self.violated_nogood(candidate_term)
            if blocker is None:
                return None
            for term in blocker:
                if term[:2] == candidate_term[:2]:
                    continue
                causes.add(term)
            mask ^= bit
        return frozenset(causes)

    def learn_nogood(
        self,
        causes: _Clause,
    ) -> _Clause | None:
        """Record the assignments that introduced a contradictory domain."""
        if not causes or len(causes) > 32:
            return None
        clause = frozenset(causes)
        if clause in self.learned_nogood_keys:
            return clause
        if len(self.learned_nogoods) >= 1024:
            evicted = self.learned_nogoods.pop(0)
            self.learned_nogood_keys.remove(evicted)
            self.remove_indexed_nogood(evicted)
        self.learned_nogoods.append(clause)
        self.learned_nogood_keys.add(clause)
        self.index_nogood(clause)
        self.pending_clause_checks.append(clause)
        self.resolver.metrics.learned_incompatibilities += 1
        self.resolver.metrics.peak_learned_clauses = max(
            self.resolver.metrics.peak_learned_clauses,
            len(self.learned_nogoods),
        )
        return clause

    @staticmethod
    def nogood_index(
        clause: _Clause,
        unary: _NogoodIndex,
        binary: _NogoodIndex,
        long: _NogoodIndex,
    ) -> _NogoodIndex:
        if len(clause) == 1:
            return unary
        if len(clause) == 2:
            return binary
        return long

    def index_nogood(self, clause: _Clause) -> None:
        index = self.nogood_index(
            clause,
            self.unary_nogoods,
            self.binary_nogoods,
            self.long_nogoods,
        )
        for key in {term[:2] for term in clause}:
            index.setdefault(key, set()).add(clause)

    def remove_indexed_nogood(self, clause: _Clause) -> None:
        index = self.nogood_index(
            clause,
            self.unary_nogoods,
            self.binary_nogoods,
            self.long_nogoods,
        )
        for key in {term[:2] for term in clause}:
            indexed = index[key]
            indexed.remove(clause)
            if not indexed:
                index.pop(key)

    def rollback(self, checkpoint: int) -> None:
        while len(self.trail) > checkpoint:
            kind, key, value = self.trail.pop()
            if kind == "domain":
                self.domains.pop(cast("str", key), None)
            elif kind == "mask":
                self.domains[cast("str", key)].mask = cast("int", value)
            elif kind == "extras":
                self.domains[cast("str", key)].extras = cast(
                    "frozenset[str]",
                    value,
                )
            elif kind == "restriction":
                self.domains[cast("str", key)].restrictions.pop()
            elif kind == "extra_cause":
                self.domains[cast("str", key)].extra_causes.remove(
                    cast("_Assignment", value),
                )
            elif kind == "selected":
                self.selected.pop(key, None)
                self.selected_extras.pop(cast("str", key), None)
            elif kind == "replace_selected":
                candidate, extras = cast(
                    "tuple[WheelCandidate, frozenset[str]]",
                    value,
                )
                name = cast("str", key)
                self.selected[name] = candidate
                self.selected_extras[name] = extras
            elif kind == "pending":
                for entry in reversed(cast("tuple[_PendingRequirement, ...]", key)):
                    for index in range(len(self.pending) - 1, -1, -1):
                        if self.pending[index] is entry:
                            self.pending.pop(index)
                            break
            elif kind == "restore_pending":
                self.pending.append(cast("_PendingRequirement", key))
            else:  # pragma: no cover - an internal invariant failure
                raise AssertionError(f"unknown trail entry: {kind}")

    def note_trail_depth(self) -> None:
        self.resolver.metrics.peak_trail_depth = max(
            self.resolver.metrics.peak_trail_depth,
            len(self.trail),
        )

    def build_graph(self, requirements: list[Requirement]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {
            "<root>": {requirement.canonical_name for requirement in requirements},
        }
        for name, candidate in self.selected.items():
            graph[name] = {
                dependency.canonical_name for dependency in self.dependencies(candidate)
            }
        return graph


def local_wheelhouse_eligible(
    resolver: EngineContext,
    requirements: list[Requirement],
    *,
    source_requirements: Mapping[str, RequirementInput],
    source_requirements_by_url: Mapping[str, RequirementInput],
) -> bool:
    """Return whether the compact local-wheel kernel preserves semantics."""
    if resolver.no_deps or not resolver.ignore_installed:
        return False
    from cpip.index.provider import CandidateProvider

    provider = resolver.provider
    if type(provider) is not CandidateProvider:
        return False
    if (
        getattr(provider.find_candidates, "__func__", None)
        is not CandidateProvider.find_candidates
    ):
        return False
    if (
        resolver.require_hashes
        or resolver.allow_prereleases
        or resolver.ignore_requires_python
        or resolver.upgrade
    ):
        return False
    if (
        not provider.no_index
        or provider.index_urls
        or not provider.find_links
        or provider.target is not None
        or provider.locked_links
        or provider.uploaded_prior_to is not None
        or provider.hashes_by_name
        or provider.build_options
        or provider.build_constraints
        or provider.prefer_binary
    ):
        return False
    if provider.release_control is not None and provider.release_control.ordered_args:
        return False
    if provider.format_control is not None and provider.format_control.no_binary:
        return False
    if source_requirements or source_requirements_by_url:
        # InstallRequirement inputs carry hashes, links, and other policy that
        # the compact source intentionally does not interpret. String inputs
        # have no such side channel and are safe to retry through this kernel.
        return False
    return not any(
        requirement.url is not None or looks_like_path_requirement(requirement.raw)
        for requirement in requirements
    )


def requirement_from_local(requirement: LocalWheelRequirement) -> Requirement:
    """Convert one compact-wheel requirement into the canonical value type."""
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    specifier = ",".join(
        operator + str(getattr(expected, "text", expected))
        for operator, expected in requirement.specifier.values
    )
    marker_value = requirement.marker
    marker = (
        f"; extra {marker_value[0]} '{marker_value[1]}'"
        if marker_value is not None
        else ""
    )
    return parse_requirement(f"{requirement.name}{extras}{specifier}{marker}")


def kernel_failure_key(
    resolver: EngineContext,
    requirements: list[Requirement],
) -> _KernelFailureKey:
    """Identify one speculative-kernel dispatch without affecting semantics."""
    return (
        tuple(resolver.provider.find_links),
        tuple(requirement.raw for requirement in requirements),
        tuple(constraint.raw for constraint in resolver.constraints),
        resolver.compute_source_hashes,
        resolver.python_version,
    )


def seed_generic_wheelhouse_catalog(
    provider: CandidateProvider,
    records: CatalogRecords,
) -> None:
    """Reuse complete wheel-only discovery when the generic resolver takes over."""
    import os

    from cpip.index.candidates import InstallationCandidate
    from cpip.index.links import Link
    from cpip.resolution.engine.sources.wheelhouse.cache import artifact_identity_cache

    direct_sources: set[str] = set()
    directory_sources: dict[str, str] = {}
    for value in provider.find_links:
        absolute = value if os.path.isabs(value) else os.path.abspath(value)
        if absolute.endswith(".whl"):
            direct_sources.add(absolute)
        else:
            directory_sources[absolute] = value

    links: list[Link] = []
    grouped: dict[str, list[Link]] = {}
    for name, candidates in records.items():
        for path, _ in candidates:
            if path in direct_sources:
                source_url = None
            else:
                source_url = directory_sources.get(os.path.dirname(path))
                if source_url is None:
                    return
            identity = artifact_identity_cache.get(path)
            local_identity = (
                None
                if identity is None
                else f"stat-fast:{identity[0]}:{identity[1]}:{identity[2]}"
            )
            link = Link.from_path(
                path,
                source_url=source_url,
                is_dir=False,
                local_identity=local_identity,
            )
            try:
                parsed = InstallationCandidate.from_link(link, target=provider.target)
            except ValueError:
                return
            if not isinstance(parsed, InstallationCandidate):
                return
            provider.parsed_link_cache[link] = parsed
            links.append(link)
            grouped.setdefault(name, []).append(link)
    provider.find_links_cache = tuple(links)
    provider.find_links_by_name_cache = {
        name: tuple(candidate_links) for name, candidate_links in grouped.items()
    }


def try_resolve_local_wheelhouse(
    resolver: EngineContext,
    requirements: list[Requirement],
    *,
    source_requirements: Mapping[str, RequirementInput],
    source_requirements_by_url: Mapping[str, RequirementInput],
) -> KernelResult | None:
    """Resolve a supported local wheel catalog without index-layer objects."""
    if not local_wheelhouse_eligible(
        resolver,
        requirements,
        source_requirements=source_requirements,
        source_requirements_by_url=source_requirements_by_url,
    ):
        return None

    from cpip.resolution.engine.sources.wheelhouse.engine import resolve
    from cpip.resolution.engine.sources.wheelhouse.metadata import (
        parse_requirement as parse_local_requirement,
    )
    from cpip.resolution.engine.sources.wheelhouse.models import (
        dependencies_for_extras,
    )

    values = [requirement.raw for requirement in requirements]
    stats: dict[str, int] = {}
    compute_source_hashes = resolver.compute_source_hashes
    if compute_source_hashes:
        from cpip.resolution.engine.output import default_file_hashes, file_hashes

        # Hash instrumentation is a supported diagnostic seam. Let canonical
        # finalization call an overridden function instead of bypassing it.
        compute_source_hashes = file_hashes is default_file_hashes
    fallback_candidates = []
    fallback_catalog = []
    local_candidates = resolve(
        resolver.provider.find_links,
        values,
        stats=stats,
        compute_source_hashes=compute_source_hashes,
        constraints=[constraint.raw for constraint in resolver.constraints],
        fallback_candidates=fallback_candidates,
        fallback_catalog=fallback_catalog,
    )
    if local_candidates is None:
        if fallback_catalog:
            seed_generic_wheelhouse_catalog(
                resolver.provider,
                fallback_catalog[0],
            )
        if fallback_candidates:
            from functools import partial

            from cpip.core.wheel import (
                WheelResolutionMetadata,
                preload_wheel_metadata,
            )
            from cpip.resolution.engine.sources.wheelhouse.cache import (
                artifact_identity_cache,
            )

            def metadata_from_local(
                candidate: LocalWheelCandidate,
            ) -> WheelResolutionMetadata:
                return WheelResolutionMetadata(
                    name=candidate.name,
                    version=Version(str(candidate.version)),
                    dependencies=tuple(
                        requirement_from_local(dependency)
                        for dependency in candidate.dependencies
                    ),
                    provided_extras=candidate.provided_extras,
                    requires_python=candidate.requires_python,
                )

            for candidate in fallback_candidates:
                preload_wheel_metadata(
                    candidate.path,
                    partial(metadata_from_local, candidate),
                    identity=artifact_identity_cache.get(candidate.path),
                )
        return None
    resolver.backtrack_count += stats.get("backtracks", 0)
    resolver.release_frontier.metrics.catalogs_loaded += 1
    resolver.release_frontier.metrics.release_masks_built += len(local_candidates)
    resolver.release_frontier.metrics.release_intersections += len(requirements)

    selected_local = {
        candidate.canonical_name: candidate for candidate in local_candidates
    }
    selected = {
        name: WheelCandidate(
            name=candidate.name,
            version=Version(str(candidate.version)),
            path=candidate.path,
            dependencies=tuple(
                requirement_from_local(dependency)
                for dependency in candidate.dependencies
            ),
            provided_extras=candidate.provided_extras,
            requires_python=candidate.requires_python,
            source_url=candidate.source_url,
            source_hashes=candidate.source_hashes,
            source_kind="wheel",
            source_vcs=candidate.source_vcs,
            from_cache=candidate.from_cache,
            yanked_reason=candidate.yanked_reason,
        )
        for name, candidate in selected_local.items()
    }

    graph: dict[str, set[str]] = {
        "<root>": {requirement.canonical_name for requirement in requirements},
    }
    pending = [
        local
        for requirement in requirements
        if (local := parse_local_requirement(requirement.raw)) is not None
    ]
    seen_contexts: set[tuple[str, frozenset[str]]] = set()
    while pending:
        requirement = pending.pop()
        name = requirement.canonical_name
        context = (name, requirement.extras)
        if context in seen_contexts:
            continue
        seen_contexts.add(context)
        candidate = selected_local.get(name)
        if candidate is None:
            continue
        dependencies = dependencies_for_extras(candidate, requirement.extras)
        graph.setdefault(name, set()).update(
            dependency.canonical_name
            for dependency in dependencies
            if dependency.canonical_name in selected_local
        )
        pending.extend(dependencies)
    for name in selected:
        graph.setdefault(name, set())
    return KernelResult(selected, graph)


def eligible(
    resolver: EngineContext,
    requirements: list[Requirement],
    *,
    source_requirements: Mapping[str, RequirementInput],
    source_requirements_by_url: Mapping[str, RequirementInput],
) -> bool:
    """Return whether the guarded kernel can preserve resolver semantics."""
    if resolver.no_deps or not resolver.ignore_installed:
        return False
    from cpip.index.provider import CandidateProvider

    if (
        getattr(resolver.provider.find_candidates, "__func__", None)
        is not CandidateProvider.find_candidates
    ):
        # Provider instrumentation is part of the resolver's supported
        # diagnostic seam; leave those runs on the generic implementation.
        return False
    if resolver.require_hashes:
        return False
    if any(
        requirement.url is not None
        or (
            requirement.marker is not None
            and not FiniteDomainKernel.marker_supported(requirement.marker)
        )
        or looks_like_path_requirement(requirement.raw)
        for requirement in requirements
    ):
        return False
    if any(source_req.hash_options for source_req in source_requirements.values()):
        return False
    if source_requirements_by_url:
        return False
    if len(requirements) > 64:
        # Very wide root sets are dominated by linear propagation.  The
        # generic agenda has lower per-requirement overhead there; reserve
        # the finite kernel for workloads where its branching machinery can
        # amortize the setup cost.
        return False
    if len(requirements) < 32:
        largest_root_catalog = max(
            (
                len(resolver.provider.available_versions(requirement))
                for requirement in requirements
                if requirement.url is None
            ),
            default=0,
        )
        if largest_root_catalog < 96:
            return False
    return True


def try_resolve(
    resolver: EngineContext,
    requirements: list[Requirement],
    *,
    source_requirements: Mapping[str, RequirementInput],
    source_requirements_by_url: Mapping[str, RequirementInput],
) -> KernelResult | None:
    failure_key: _KernelFailureKey | None = None
    if _kernel_failure_cache and resolver.provider.find_links:
        from cpip.resolution.engine.sources.wheelhouse.catalog import source_signatures

        failure_key = kernel_failure_key(resolver, requirements)
        failed_signatures = _kernel_failure_cache.get(failure_key)
        if (
            failed_signatures is not None
            and source_signatures(resolver.provider.find_links) == failed_signatures
        ):
            # Both optional kernels declined this unchanged workload before.
            # Generic resolution remains authoritative for result and diagnostics.
            return None
        if failed_signatures is not None:
            _kernel_failure_cache.pop(failure_key, None)

    # The existing finite-domain kernel is specifically tuned and covered for
    # medium-width root sets. Other shapes should probe the compact wheelhouse
    # source first so its eligibility check does not trigger index catalog work.
    prefer_finite_domain = 32 <= len(requirements) <= 64
    if not prefer_finite_domain:
        local_result = try_resolve_local_wheelhouse(
            resolver,
            requirements,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if local_result is not None:
            return local_result
    finite_domain_eligible = eligible(
        resolver,
        requirements,
        source_requirements=source_requirements,
        source_requirements_by_url=source_requirements_by_url,
    )
    if resolver.debug_internal:
        print(
            f"finite-domain kernel eligible: {finite_domain_eligible}",
            flush=True,
        )
    if finite_domain_eligible:
        try:
            result = FiniteDomainKernel(resolver).resolve(
                requirements,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
        except (KernelUnsupported, KernelUnsatisfiable) as exc:
            if resolver.debug_internal:
                detail = f": {exc}" if str(exc) else ""
                print(
                    f"finite-domain kernel declined: {type(exc).__name__}{detail}",
                    flush=True,
                )
        else:
            return result
    if prefer_finite_domain:
        local_result = try_resolve_local_wheelhouse(
            resolver,
            requirements,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if local_result is not None:
            return local_result

    if resolver.provider.find_links:
        from cpip.resolution.engine.sources.wheelhouse.catalog import source_signatures

        signatures = source_signatures(resolver.provider.find_links)
        if signatures is not None:
            failure_key = failure_key or kernel_failure_key(resolver, requirements)
            _kernel_failure_cache[failure_key] = signatures
            if len(_kernel_failure_cache) > _KERNEL_FAILURE_CACHE_MAX:
                _kernel_failure_cache.pop(next(iter(_kernel_failure_cache)))
    return None


__all__ = [
    "FiniteDomainKernel",
    "KernelResult",
    "eligible",
    "local_wheelhouse_eligible",
    "try_resolve",
    "try_resolve_local_wheelhouse",
]
