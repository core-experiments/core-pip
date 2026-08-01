"""Guarded finite-domain resolution for inexpensive wheel-only workloads.

This module intentionally owns no user-facing error formatting.  It either
returns a valid assignment or declines the workload so the generic resolver
can provide the authoritative result and diagnostics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from cpip.core.packaging import Requirement, marker_applies
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import LazyWheelCandidate
from cpip.index.source_locations import looks_like_path_requirement

if TYPE_CHECKING:
    from cpip.resolution.engine.context import EngineContext
    from cpip.resolution.engine.input.models import RequirementInput


class KernelUnsupported(Exception):
    """The workload requires semantics owned by the generic resolver."""


class KernelUnsatisfiable(Exception):
    """The finite kernel found no assignment; generic diagnostics still win."""


_EXTRA_MARKER_RE = re.compile(r"\bextra\b", flags=re.IGNORECASE)


@dataclass(slots=True)
class KernelResult:
    selected: dict[str, WheelCandidate]
    graph: dict[str, set[str]]


@dataclass(slots=True)
class _Domain:
    candidates: tuple[WheelCandidate, ...]
    mask: int


TrailKey = str | tuple[Requirement, ...] | tuple[int, Requirement]
TrailValue = _Domain | int | None


@dataclass(slots=True)
class _Decision:
    name: str
    choices: tuple[int, ...]
    next_choice: int
    parent_checkpoint: int
    candidate_checkpoint: int


class _KernelConflict(Exception):
    """The current decision violates a learned assignment nogood."""


class FiniteDomainKernel:
    """Iterative trail-based search over a finite local candidate catalog."""

    def __init__(self, resolver: EngineContext) -> None:
        self.resolver = resolver
        self.domains: dict[str, _Domain] = {}
        self.catalogs: dict[str, tuple[WheelCandidate, ...]] = {}
        self.selected: dict[str, WheelCandidate] = {}
        self.pending: list[Requirement] = []
        self.trail: list[tuple[str, object, object]] = []
        self.decisions: list[_Decision] = []
        self.dependencies_cache: dict[int, tuple[Requirement, ...]] = {}
        self.learned_nogoods: list[frozenset[tuple[str, str]]] = []
        self.learned_nogood_keys: set[frozenset[tuple[str, str]]] = set()
        self.root_names: set[str] = set()

    def resolve(
        self,
        requirements: list[Requirement],
        *,
        source_requirements: Mapping[str, RequirementInput],
        source_requirements_by_url: Mapping[str, RequirementInput],
    ) -> KernelResult:
        del source_requirements, source_requirements_by_url
        active_requirements = self.active_requirements(requirements)
        self.root_names = {
            requirement.canonical_name for requirement in active_requirements
        }
        self.pending.extend(reversed(active_requirements))

        while True:
            if not self.pending:
                return KernelResult(
                    self.selected,
                    self.build_graph(active_requirements),
                )

            requirement = self.pop_pending()
            checkpoint = len(self.trail)
            domain = self.add_requirement(requirement)
            name = requirement.canonical_name
            selected = self.selected.get(name)
            if selected is not None:
                if not requirement.is_satisfied_by(
                    selected.version,
                    allow_prereleases=self.resolver.allow_prereleases_internal(
                        requirement,
                    ),
                ):
                    self.fail(checkpoint, conflict_name=name)
                continue

            if not domain.mask:
                self.fail(checkpoint, conflict_name=name)
                continue

            choices = tuple(
                index
                for index in range(len(domain.candidates))
                if domain.mask & (1 << index)
            )
            if len(choices) == 1:
                # A singleton domain cannot branch.  Avoid allocating a
                # decision frame, while retaining the trail entries needed
                # to unwind if a later dependency proves the workload
                # unsupported or unsatisfiable.
                singleton = _Decision(
                    name=name,
                    choices=choices,
                    next_choice=0,
                    parent_checkpoint=checkpoint,
                    candidate_checkpoint=len(self.trail),
                )
                if not self.select(singleton):
                    self.fail(checkpoint)
                continue
            decision = _Decision(
                name=name,
                choices=choices,
                next_choice=0,
                parent_checkpoint=checkpoint,
                candidate_checkpoint=len(self.trail),
            )
            self.decisions.append(decision)
            if not self.select(decision):
                self.fail(decision.candidate_checkpoint)

    def add_requirement(self, requirement: Requirement) -> _Domain:
        name = requirement.canonical_name
        domain = self.domains.get(name)
        if domain is None:
            candidates = self.load_candidates(requirement)
            mask = sum(
                1 << index
                for index, candidate in enumerate(candidates)
                if requirement.is_satisfied_by(
                    candidate.version,
                    allow_prereleases=self.resolver.allow_prereleases_internal(
                        requirement,
                    ),
                )
            )
            domain = _Domain(candidates, mask)
            self.trail.append(("domain", name, None))
            self.domains[name] = domain
            return domain

        old_mask = domain.mask
        allow_prereleases = self.resolver.allow_prereleases_internal(requirement)
        mask = old_mask
        for index, candidate in enumerate(domain.candidates):
            if not requirement.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                mask &= ~(1 << index)
        if mask != old_mask:
            self.trail.append(("mask", name, old_mask))
            domain.mask = mask
        return domain

    def load_candidates(self, requirement: Requirement) -> tuple[WheelCandidate, ...]:
        candidates = self.catalogs.get(requirement.canonical_name)
        if candidates is None:
            stream = self.resolver.provider.find_candidates(requirement)
            candidates = tuple(stream)
            self.catalogs[requirement.canonical_name] = candidates
        if not candidates:
            raise KernelUnsatisfiable

        compatible: list[WheelCandidate] = []
        for candidate in candidates:
            if not self.is_local_wheel(candidate):
                raise KernelUnsupported
            if (
                not self.resolver.ignore_requires_python
                and not self.resolver.candidate_matches_python(candidate)
            ):
                continue
            compatible.append(candidate)
        if not compatible:
            raise KernelUnsupported
        return tuple(compatible)

    @staticmethod
    def is_local_wheel(candidate: WheelCandidate) -> bool:
        if candidate.source_kind != "wheel":
            return False
        if isinstance(candidate, LazyWheelCandidate):
            return candidate.record_internal.link.is_file
        return candidate.path.is_file()

    def select(self, decision: _Decision) -> bool:
        candidate = self.domains[decision.name].candidates[
            decision.choices[decision.next_choice]
        ]
        if self.violates_nogood(decision.name, candidate):
            return False
        self.resolver.validate_candidate_policy(candidate)
        self.resolver.validate_candidate_constraints(candidate)
        self.trail.append(("selected", decision.name, None))
        self.selected[decision.name] = candidate
        dependencies = self.dependencies(candidate)
        self.trail.append(("pending", dependencies, None))
        self.pending.extend(dependencies)
        return True

    def violates_nogood(self, name: str, candidate: WheelCandidate) -> bool:
        assignment = {
            (selected_name, str(selected.version))
            for selected_name, selected in self.selected.items()
        }
        assignment.add((name, str(candidate.version)))
        for nogood in self.learned_nogoods:
            if nogood.issubset(assignment):
                return True
        return False

    def dependencies(self, candidate: WheelCandidate) -> tuple[Requirement, ...]:
        key = id(candidate)
        cached = self.dependencies_cache.get(key)
        if cached is not None:
            return cached
        dependencies = tuple(candidate.dependencies)
        active: list[Requirement] = []
        requested_extras = (
            candidate.requirement_internal.extras
            if isinstance(candidate, LazyWheelCandidate)
            else None
        )
        for dependency in dependencies:
            if dependency.extras or dependency.url is not None:
                raise KernelUnsupported
            if dependency.marker is not None:
                has_extra_marker = (
                    _EXTRA_MARKER_RE.search(dependency.marker) is not None
                )
                if has_extra_marker and requested_extras is None:
                    raise KernelUnsupported
                if not has_extra_marker and not self.marker_supported(
                    dependency.marker,
                ):
                    raise KernelUnsupported
                if not marker_applies(
                    dependency.marker,
                    extras=requested_extras or (),
                ):
                    continue
            active.append(dependency)
        dependencies = tuple(active)
        self.dependencies_cache[key] = dependencies
        return dependencies

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

    def pop_pending(self) -> Requirement:
        # Minimum-remaining-values ordering turns a broad chronological
        # search into a conflict-first search.  Crucially, it only uses
        # catalogs already loaded by propagation; choosing a requirement must
        # never trigger extra I/O or metadata work.
        if len(self.pending) > 32:
            # Avoid repeatedly scanning a wide independent root set.  The
            # conflict frontier is normally small after the first dependency
            # expansion, where the MRV pass provides its benefit.
            choice = len(self.pending) - 1
        else:
            choice = max(
                range(len(self.pending)),
                key=lambda index: self.pending_priority(self.pending[index], index),
            )
        requirement = self.pending.pop(choice)
        self.trail.append(("restore_pending", (choice, requirement), None))
        return requirement

    def pending_priority(
        self,
        requirement: Requirement,
        index: int,
    ) -> tuple[int, int, int]:
        name = requirement.canonical_name
        domain = self.domains.get(name)
        if domain is not None:
            # Smaller domains are more urgent.  Negating the count lets the
            # max() call select the minimum remaining value.
            return (2, -domain.mask.bit_count(), -index)
        catalog = self.catalogs.get(name)
        if catalog is not None:
            return (1, -len(catalog), -index)
        # Preserve the old LIFO behavior for unseen packages.  This keeps the
        # common linear case cheap while allowing already-constrained domains
        # to jump ahead of it.
        return (0, 0, index)

    def fail(self, checkpoint: int, conflict_name: str | None = None) -> None:
        clause = self.learn_nogood(conflict_name)
        if clause is not None and self.try_backjump(clause):
            return
        self.rollback(checkpoint)
        while self.decisions:
            decision = self.decisions[-1]
            self.rollback(decision.candidate_checkpoint)
            if decision.next_choice + 1 < len(decision.choices):
                decision.next_choice += 1
                self.resolver.emit_backtracking_message()
                if self.select(decision):
                    return
                self.rollback(decision.candidate_checkpoint)
            self.rollback(decision.parent_checkpoint)
            self.decisions.pop()
        raise KernelUnsatisfiable

    def learn_nogood(
        self,
        conflict_name: str | None = None,
    ) -> frozenset[tuple[str, str]] | None:
        """Remember the smallest conservative clause available at conflict."""
        relevant: set[tuple[str, str]] = set()
        if conflict_name is not None and conflict_name not in self.root_names:
            for decision in self.decisions:
                candidate = self.selected.get(decision.name)
                if candidate is not None and any(
                    dependency.canonical_name == conflict_name
                    for dependency in self.dependencies(candidate)
                ):
                    relevant.add((decision.name, str(candidate.version)))
        if not relevant:
            relevant = {
                (decision.name, str(self.selected[decision.name].version))
                for decision in self.decisions
                if decision.name in self.selected
            }
        if not relevant:
            relevant = {
                (name, str(candidate.version))
                for name, candidate in self.selected.items()
            }
        if not relevant or len(relevant) > 32:
            return None
        clause = frozenset(relevant)
        if clause in self.learned_nogood_keys:
            return clause
        if len(self.learned_nogoods) >= 1024:
            evicted = self.learned_nogoods.pop(0)
            self.learned_nogood_keys.remove(evicted)
        self.learned_nogoods.append(clause)
        self.learned_nogood_keys.add(clause)
        return clause

    def try_backjump(self, clause: frozenset[tuple[str, str]]) -> bool:
        """Jump over decisions absent from a learned conflict clause."""
        decision_indices = {
            (decision.name, str(self.selected[decision.name].version)): index
            for index, decision in enumerate(self.decisions)
            if decision.name in self.selected
        }
        clause_indices = [
            decision_indices[term] for term in clause if term in decision_indices
        ]
        if len(clause_indices) != len(clause):
            return False
        target_index = max(clause_indices)
        if target_index >= len(self.decisions) - 1:
            return False
        target = self.decisions[target_index]
        if target.next_choice + 1 >= len(target.choices):
            return False
        self.rollback(target.candidate_checkpoint)
        del self.decisions[target_index + 1 :]
        target.next_choice += 1
        self.resolver.emit_backtracking_message()
        if not self.select(target):
            self.rollback(target.candidate_checkpoint)
            return False
        return True

    def rollback(self, checkpoint: int) -> None:
        while len(self.trail) > checkpoint:
            kind, key, value = self.trail.pop()
            if kind == "domain":
                if value is None:
                    self.domains.pop(cast("str", key), None)
                else:
                    self.domains[cast("str", key)] = cast("_Domain", value)
            elif kind == "mask":
                self.domains[cast("str", key)].mask = cast("int", value)
            elif kind == "selected":
                self.selected.pop(key, None)
            elif kind == "pending":
                for dependency in reversed(cast("tuple[Requirement, ...]", key)):
                    for index in range(len(self.pending) - 1, -1, -1):
                        if self.pending[index] is dependency:
                            self.pending.pop(index)
                            break
            elif kind == "restore_pending":
                index, requirement = cast("tuple[int, Requirement]", key)
                self.pending.insert(index, requirement)
            else:  # pragma: no cover - an internal invariant failure
                raise AssertionError(f"unknown trail entry: {kind}")

    def build_graph(self, requirements: list[Requirement]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {
            "<root>": {requirement.canonical_name for requirement in requirements},
        }
        for name, candidate in self.selected.items():
            graph[name] = {
                dependency.canonical_name for dependency in self.dependencies(candidate)
            }
        return graph


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
    if resolver.constraints or resolver.require_hashes:
        return False
    if resolver.provider.index_urls:
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
    # Eagerly materializing a finite domain only pays off when there is enough
    # branching to amortize the catalog setup.  Small graphs stay on the lazy
    # generic resolver, which is faster for one-shot linear propagation.
    if len(requirements) < 32:
        largest_root_catalog = max(
            (
                len(resolver.provider.catalog_links(requirement))
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
    if not eligible(
        resolver,
        requirements,
        source_requirements=source_requirements,
        source_requirements_by_url=source_requirements_by_url,
    ):
        return None
    try:
        return FiniteDomainKernel(resolver).resolve(
            requirements,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
    except (KernelUnsupported, KernelUnsatisfiable):
        return None


__all__ = [
    "FiniteDomainKernel",
    "KernelResult",
    "eligible",
    "try_resolve",
]
