"""Fast local wheelhouse resolver orchestration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cpip.core.python import CURRENT_PYTHON_VERSION_FULL, CURRENT_PYTHON_VERSION_INFO
from cpip.resolution.engine.sources.wheelhouse.cache import (
    CatalogRecords,
    DomainCache,
    PreflightCache,
    shared_preflight_cache,
)
from cpip.resolution.engine.sources.wheelhouse.catalog import (
    build_catalog_indexes,
    load_catalog,
    load_metadata_cache,
    save_catalog,
    save_metadata_cache,
    scan_catalog,
)
from cpip.resolution.engine.sources.wheelhouse.metadata import (
    parse_requirement,
    quote_path,
)
from cpip.resolution.engine.sources.wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelVersion,
)
from cpip.resolution.engine.sources.wheelhouse.search import search_candidates

if TYPE_CHECKING:
    from cpip.index.metadata_cache import WheelMetadataCache


def resolve(
    find_links: list[str],
    values: list[str],
    *,
    cache_dir: str | None = None,
    stats: dict[str, int] | None = None,
    compute_source_hashes: bool = False,
    constraints: list[str] | None = None,
    fallback_candidates: list[LocalWheelCandidate] | None = None,
    fallback_catalog: list[CatalogRecords] | None = None,
) -> list[LocalWheelCandidate] | None:
    requirements: list[LocalWheelRequirement] = []
    for value in values:
        requirement = parse_requirement(value)
        if requirement is None or (
            requirement.marker is not None and not requirement.marker_applies()
        ):
            return None
        requirements.append(requirement)
    global_constraints: dict[str, list[LocalWheelRequirement]] = {}
    for value in constraints or ():
        constraint = parse_requirement(value)
        if constraint is None or constraint.marker is not None or constraint.extras:
            return None
        global_constraints.setdefault(constraint.canonical_name, []).append(constraint)
    catalog_path, records = load_catalog(cache_dir, find_links)
    if records is None:
        records = scan_catalog(find_links)
        if records is None:
            return None
        save_catalog(catalog_path, find_links, records)
    exact_index, exact_masks, range_index = build_catalog_indexes(records)
    loaded: dict[str, LocalWheelCandidate | None] = {}
    matching_domains: DomainCache = {}
    preflight_cache: PreflightCache = {}
    current_python = LocalWheelVersion(
        tuple(CURRENT_PYTHON_VERSION_INFO[:3]),
        CURRENT_PYTHON_VERSION_FULL,
    )
    cache_path, metadata_cache = load_metadata_cache(cache_dir)
    persistent_cache: WheelMetadataCache | None = None
    if cache_dir is not None:
        from cpip.index.metadata_cache import get_wheel_metadata_cache

        persistent_cache = get_wheel_metadata_cache(cache_dir)
    try:
        selected = search_candidates(
            records,
            requirements,
            {},
            {},
            {
                name: tuple(package_constraints)
                for name, package_constraints in global_constraints.items()
            },
            {},
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
            [],
            [],
            stats,
            compute_source_hashes,
        )
    finally:
        save_metadata_cache(cache_path, metadata_cache)
    if selected is None:
        if fallback_candidates is not None:
            fallback_candidates.extend(
                candidate for candidate in loaded.values() if candidate is not None
            )
        if fallback_catalog is not None:
            fallback_catalog.append(records)
        return None
    for candidate in selected.values():
        candidate.source_url = "file://" + quote_path(os.path.abspath(candidate.path))
    return list(selected.values())
