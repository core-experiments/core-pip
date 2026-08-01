"""Candidate backtracking for the fast wheelhouse path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpip.resolution.engine.sources.wheelhouse.archive import WheelhouseUnavailable
from cpip.resolution.engine.sources.wheelhouse.cache import (
    Domain,
    DomainCache,
    MetadataCache,
    PreflightCache,
    RangeIndex,
    SharedPreflightCache,
)
from cpip.resolution.engine.sources.wheelhouse.catalog import (
    matching_domain,
    preflight_exact_dependencies,
)
from cpip.resolution.engine.sources.wheelhouse.metadata import (
    load_candidate,
    parse_requirement,
)
from cpip.resolution.engine.sources.wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelVersion,
    dependencies_for_extras,
)

if TYPE_CHECKING:
    from cpip.index.metadata_cache import WheelMetadataCache


class _SearchFrame:
    __slots__ = (
        "checkpoint",
        "domain",
        "domain_checkpoint",
        "name",
        "pending",
        "requirement",
        "selected_name",
        "values",
    )

    def __init__(
        self,
        pending: list[LocalWheelRequirement],
        checkpoint: int,
        domain_checkpoint: int,
        selected_name: str | None = None,
        requirement: LocalWheelRequirement | None = None,
        name: str | None = None,
        domain: Domain = 0,
        values: tuple[tuple[str, LocalWheelVersion], ...] = (),
    ) -> None:
        self.pending = pending
        self.checkpoint = checkpoint
        self.domain_checkpoint = domain_checkpoint
        self.selected_name = selected_name
        self.requirement = requirement
        self.name = name
        self.domain = domain
        self.values = values


def search_candidates(
    records: dict[str, list[tuple[str, LocalWheelVersion]]],
    pending: list[LocalWheelRequirement],
    selected: dict[str, LocalWheelCandidate],
    constraints: dict[str, list[LocalWheelRequirement]],
    domains: dict[str, Domain],
    exact_index: dict[str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]],
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
    preflight_cache: PreflightCache,
    shared_preflight_cache: SharedPreflightCache,
    current_python: LocalWheelVersion,
    loaded: dict[str, LocalWheelCandidate | None],
    metadata_cache: MetadataCache | None,
    persistent_cache: WheelMetadataCache | None,
    trail: list[tuple[str, LocalWheelRequirement]],
    domain_trail: list[tuple[str, Domain | None]],
) -> dict[str, LocalWheelCandidate] | None:
    match_domain = matching_domain
    load = load_candidate
    dependencies = dependencies_for_extras
    preflight = preflight_exact_dependencies

    def rollback(checkpoint: int, domain_checkpoint: int) -> None:
        while len(domain_trail) > domain_checkpoint:
            domain_name, previous = domain_trail.pop()
            if previous is None:
                del domains[domain_name]
            else:
                domains[domain_name] = previous
        while len(trail) > checkpoint:
            constraint_name, _ = trail.pop()
            values = constraints[constraint_name]
            values.pop()
            if not values:
                del constraints[constraint_name]

    frames = [_SearchFrame(list(pending), len(trail), len(domain_trail))]
    while frames:
        frame = frames[-1]
        if frame.requirement is None:
            if not frame.pending:
                return selected
            requirement = frame.pending.pop()
            if requirement.marker is not None and not requirement.marker_applies():
                continue
            name = requirement.canonical_name
            package_constraints = constraints.setdefault(name, [])
            package_constraints.append(requirement)
            trail.append((name, requirement))
            previous_domain = domains.get(name)
            domain = match_domain(
                name,
                requirement,
                exact_masks,
                range_index,
                matching_domains,
            )
            if previous_domain is not None:
                domain &= previous_domain
            domains[name] = domain
            domain_trail.append((name, previous_domain))
            existing = selected.get(name)
            if existing is not None:
                # The selected candidate satisfied every constraint already
                # in the trail when it was chosen. Only this newly appended
                # requirement can invalidate it.
                if not requirement.is_satisfied_by(existing.version):
                    rollback(frame.checkpoint, frame.domain_checkpoint)
                    frames.pop()
                    if frame.selected_name is not None:
                        selected.pop(frame.selected_name, None)
                    continue
                continue
            if not domain:
                rollback(frame.checkpoint, frame.domain_checkpoint)
                frames.pop()
                if frame.selected_name is not None:
                    selected.pop(frame.selected_name, None)
                continue
            frame.requirement = requirement
            frame.name = name
            frame.domain = domain
            frame.values = range_index[name][1]
        if not frame.domain:
            rollback(frame.checkpoint, frame.domain_checkpoint)
            frames.pop()
            if frame.selected_name is not None:
                selected.pop(frame.selected_name, None)
            continue
        index = frame.domain.bit_length() - 1
        frame.domain &= ~(1 << index)
        name = frame.name
        requirement = frame.requirement
        assert name is not None and requirement is not None
        path, version = frame.values[index]
        if path not in loaded:
            try:
                loaded[path] = load(
                    path,
                    metadata_cache,
                    (name, version),
                    persistent_cache,
                    path_is_absolute=True,
                )
            except WheelhouseUnavailable:
                loaded[path] = None
        candidate = loaded[path]
        if candidate is None:
            continue
        if candidate.requires_python:
            python_requirement = parse_requirement(f"python{candidate.requires_python}")
            if python_requirement is None:
                raise WheelhouseUnavailable
            if not python_requirement.specifier.contains(current_python):
                continue
        candidate_dependencies = dependencies(candidate, requirement.extras)
        if not preflight(
            candidate,
            exact_index,
            exact_masks,
            range_index,
            matching_domains,
            preflight_cache,
            shared_preflight_cache,
            loaded,
            metadata_cache,
            persistent_cache,
            requirement.extras,
        ):
            continue
        selected[name] = candidate
        frames.append(
            _SearchFrame(
                pending=[*frame.pending, *candidate_dependencies],
                checkpoint=len(trail),
                domain_checkpoint=len(domain_trail),
                selected_name=name,
            ),
        )
