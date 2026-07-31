"""Candidate backtracking for the fast wheelhouse path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpip.resolution.fast_wheelhouse.archive import WheelhouseUnavailable
from cpip.resolution.fast_wheelhouse.catalog import (
    matching_domain,
    preflight_exact_dependencies,
)
from cpip.resolution.fast_wheelhouse.cache import (
    Domain,
    DomainCache,
    MetadataCache,
    PreflightCache,
    RangeIndex,
    SharedPreflightCache,
)
from cpip.resolution.fast_wheelhouse.metadata import load_candidate
from cpip.resolution.fast_wheelhouse.metadata import parse_requirement
from cpip.resolution.fast_wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelVersion,
    dependencies_for_extras,
)

if TYPE_CHECKING:
    from cpip.index.metadata_cache import WheelMetadataCache


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
    checkpoint = len(trail)
    domain_checkpoint = len(domain_trail)

    def rollback() -> None:
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

    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker_applies():
            continue
        name = requirement.canonical_name
        package_constraints = constraints.setdefault(name, [])
        package_constraints.append(requirement)
        trail.append((name, requirement))
        previous_domain = domains.get(name)
        domain = matching_domain(
            name, requirement, exact_masks, range_index, matching_domains
        )
        if previous_domain is not None:
            domain &= previous_domain
        values = range_index[name][1]
        domains[name] = domain
        domain_trail.append((name, previous_domain))
        existing = selected.get(name)
        if existing is not None:
            if not all(
                constraint.is_satisfied_by(existing.version)
                for constraint in package_constraints
            ):
                rollback()
                return None
            continue
        if not domain:
            rollback()
            return None
        while domain:
            index = domain.bit_length() - 1
            domain &= ~(1 << index)
            path, version = values[index]
            if path not in loaded:
                try:
                    loaded[path] = load_candidate(
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
                python_requirement = parse_requirement(
                    f"python{candidate.requires_python}"
                )
                if python_requirement is None:
                    raise WheelhouseUnavailable
                if not python_requirement.specifier.contains(current_python):
                    continue
            dependencies = dependencies_for_extras(candidate, requirement.extras)
            if not preflight_exact_dependencies(
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
            result = search_candidates(
                records,
                [*pending, *dependencies],
                selected,
                constraints,
                domains,
                exact_index,
                exact_masks,
                range_index,
                matching_domains,
                preflight_cache,
                shared_preflight_cache,
                current_python,
                loaded,
                metadata_cache,
                persistent_cache,
                trail,
                domain_trail,
            )
            if result is not None:
                return result
            selected.pop(name, None)
        rollback()
        return None
    return selected
