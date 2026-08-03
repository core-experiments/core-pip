"""Catalog indexing and dependency preflight for the fast wheelhouse path."""

from __future__ import annotations

import os
import stat
from bisect import bisect_left, bisect_right
from functools import lru_cache
from typing import TYPE_CHECKING, cast

from cpip.core.marshal_cache import load_snapshot, save_snapshot
from cpip.index.directory_index import local_source_snapshot
from cpip.resolution.engine.sources.wheelhouse.archive import WheelhouseUnavailable
from cpip.resolution.engine.sources.wheelhouse.cache import (
    _CATALOG_CACHE_VERSION,
    CatalogIndexes,
    CatalogRecords,
    CatalogSignatures,
    Domain,
    DomainCache,
    ExactIndex,
    ExactMasks,
    MetadataCache,
    MetadataCacheIdentity,
    PreflightCache,
    RangeIndex,
    SharedPreflightCache,
    artifact_identity_cache,
    cache_preflight_result,
    catalog_indexes_cache,
    catalog_snapshot_store,
    metadata_cache_dirty,
    metadata_cache_paths,
    metadata_cache_store,
)
from cpip.resolution.engine.sources.wheelhouse.metadata import (
    load_candidate,
    parse_version,
    parse_wheel_filename,
    wheel_name,
)
from cpip.resolution.engine.sources.wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelVersion,
)

if TYPE_CHECKING:
    from cpip.index.metadata_cache import WheelMetadataCache


def preflight_exact_dependencies(
    candidate: LocalWheelCandidate,
    exact_index: dict[str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]],
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
    preflight_cache: PreflightCache,
    shared_preflight_cache: SharedPreflightCache,
    loaded: dict[str, LocalWheelCandidate | None],
    metadata_cache: MetadataCache | None,
    persistent_cache: WheelMetadataCache | None,
    extras: frozenset[str],
    compute_source_hashes: bool = False,
) -> bool:
    """Reject exact dependency fan-outs with an impossible shared domain.

    This is deliberately conservative: if any dependency is not a unique
    exact candidate, the normal resolver remains authoritative.
    """
    cache_key = (candidate.path, extras)
    cached = preflight_cache.get(cache_key)
    if cached is not None:
        return cached
    shared_key = (id(exact_index), id(candidate), extras)
    shared = shared_preflight_cache.get(shared_key)
    if shared is not None and shared[0] is exact_index and shared[1] is candidate:
        preflight_cache[cache_key] = shared[2]
        return shared[2]

    emitted_domains: dict[str, Domain] = {}
    for dependency in candidate.dependencies:
        if dependency.marker is not None and not dependency.marker_applies(extras):
            continue
        values = dependency.specifier.values
        if (
            len(values) != 1
            or values[0][0] != "=="
            or not isinstance(values[0][1], LocalWheelVersion)
        ):
            preflight_cache[cache_key] = True
            cache_preflight_result(
                shared_preflight_cache,
                exact_index,
                candidate,
                extras,
                True,
            )
            return True
        versions = exact_index.get(dependency.canonical_name)
        entries = (
            versions.get(values[0][1]._normalized, ()) if versions is not None else ()
        )
        if not entries:
            preflight_cache[cache_key] = False
            cache_preflight_result(
                shared_preflight_cache,
                exact_index,
                candidate,
                extras,
                False,
            )
            return False
        if len(entries) != 1:
            preflight_cache[cache_key] = True
            cache_preflight_result(
                shared_preflight_cache,
                exact_index,
                candidate,
                extras,
                True,
            )
            return True
        path, version = entries[0]
        if path not in loaded:
            try:
                loaded[path] = load_candidate(
                    path,
                    metadata_cache,
                    (dependency.canonical_name, version),
                    persistent_cache,
                    path_is_absolute=True,
                    compute_source_hashes=compute_source_hashes,
                )
            except WheelhouseUnavailable:
                loaded[path] = None
        child = loaded[path]
        if child is None:
            preflight_cache[cache_key] = False
            cache_preflight_result(
                shared_preflight_cache,
                exact_index,
                candidate,
                extras,
                False,
            )
            return False
        for child_dependency in child.dependencies:
            if (
                child_dependency.marker is not None
                and not child_dependency.marker_applies(dependency.extras)
            ):
                continue
            name = child_dependency.canonical_name
            matching = matching_domain(
                name,
                child_dependency,
                exact_masks,
                range_index,
                matching_domains,
            )
            domain = emitted_domains.get(name)
            if domain is None:
                if not matching:
                    preflight_cache[cache_key] = False
                    cache_preflight_result(
                        shared_preflight_cache,
                        exact_index,
                        candidate,
                        extras,
                        False,
                    )
                    return False
                emitted_domains[name] = matching
            else:
                domain &= matching
                if not domain:
                    preflight_cache[cache_key] = False
                    cache_preflight_result(
                        shared_preflight_cache,
                        exact_index,
                        candidate,
                        extras,
                        False,
                    )
                    return False
    preflight_cache[cache_key] = True
    cache_preflight_result(shared_preflight_cache, exact_index, candidate, extras, True)
    return True


def matching_domain(
    name: str,
    requirement: LocalWheelRequirement,
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
) -> Domain:
    cache_key = (name, requirement.specifier.values)
    cached = matching_domains.get(cache_key)
    if cached is not None:
        return cached
    range_values = range_index.get(name)
    if range_values is None:
        matching_domains[cache_key] = 0
        return 0
    keys, values = range_values
    operator_values = requirement.specifier.values
    if (
        len(operator_values) == 1
        and operator_values[0][0] == "=="
        and isinstance(operator_values[0][1], LocalWheelVersion)
    ):
        domain = exact_masks.get(name, {}).get(operator_values[0][1]._normalized, 0)
        matching_domains[cache_key] = domain
        return domain
    all_versions = (1 << len(values)) - 1
    domain = all_versions
    for index, (operator, expected) in enumerate(requirement.specifier.values):
        if isinstance(expected, LocalWheelVersion):
            if operator == "==":
                matching = exact_masks.get(name, {}).get(expected._normalized, 0)
            elif operator == "!=":
                matching = all_versions & ~exact_masks.get(name, {}).get(
                    expected._normalized,
                    0,
                )
            elif operator == "===":
                matching = 0
                for index, (_, version) in enumerate(values):
                    if version.text == expected.text:
                        matching |= 1 << index
            elif operator in {"<", "<=", ">", ">=", "~="}:
                if operator == ">=":
                    start, end = bisect_left(keys, expected._normalized), len(values)
                elif operator == ">":
                    start, end = bisect_right(keys, expected._normalized), len(values)
                elif operator == "<=":
                    start, end = 0, bisect_right(keys, expected._normalized)
                elif operator == "<":
                    start, end = 0, bisect_left(keys, expected._normalized)
                else:
                    upper = requirement.specifier._compatible_upper[index]
                    assert upper is not None
                    start = bisect_left(keys, expected._normalized)
                    end = bisect_left(keys, upper)
                matching = ((1 << end) - 1) ^ ((1 << start) - 1)
            else:
                matching = 0
        elif isinstance(expected, str) and operator in {"==", "!="}:
            prefix = expected[:-2]
            matching = 0
            for index, (_, version) in enumerate(values):
                matches = version.text == prefix or version.text.startswith(
                    prefix + ".",
                )
                if matches == (operator == "=="):
                    matching |= 1 << index
        else:
            matching = 0
        domain &= matching
        if not domain:
            matching_domains[cache_key] = 0
            return 0
    matching_domains[cache_key] = domain
    return domain


def load_metadata_cache(
    cache_dir: str | None = None,
) -> tuple[str | None, MetadataCache | None]:
    root = cache_dir if cache_dir is not None else os.environ.get("CPIP_CACHE_DIR")
    if not root:
        return None, None
    path = os.path.join(root, "fast-lock-metadata-v1.marshal")
    try:
        stat = os.stat(path)
        identity: MetadataCacheIdentity | None = (
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError:
        identity = None
    cached = metadata_cache_store.get(path)
    if cached is not None and cached[0] == identity:
        return path, cached[1]
    cache_payload = load_snapshot(path)
    cache: MetadataCache = (
        cast("MetadataCache", cache_payload) if isinstance(cache_payload, dict) else {}
    )
    metadata_cache_store[path] = (identity, cache)
    metadata_cache_paths[id(cache)] = path
    metadata_cache_dirty.discard(path)
    if len(metadata_cache_store) > 16:
        evicted_path = next(iter(metadata_cache_store))
        evicted = metadata_cache_store.pop(evicted_path)
        metadata_cache_paths.pop(id(evicted[1]), None)
        metadata_cache_dirty.discard(evicted_path)
    return path, cache


def save_metadata_cache(path: str | None, cache: MetadataCache | None) -> None:
    if path is None or cache is None:
        return
    if path not in metadata_cache_dirty:
        return
    if not save_snapshot(path, cache):
        return
    try:
        stat = os.stat(path)
        identity: MetadataCacheIdentity | None = (
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError:
        identity = None
    metadata_cache_store[path] = (identity, cache)
    metadata_cache_paths[id(cache)] = path
    metadata_cache_dirty.discard(path)
    if len(metadata_cache_store) > 16:
        evicted_path = next(iter(metadata_cache_store))
        evicted = metadata_cache_store.pop(evicted_path)
        metadata_cache_paths.pop(id(evicted[1]), None)
        metadata_cache_dirty.discard(evicted_path)


def cache_root(cache_dir: str | None) -> str | None:
    return cache_dir if cache_dir is not None else os.environ.get("CPIP_CACHE_DIR")


def source_signatures(find_links: list[str]) -> CatalogSignatures | None:
    signatures: list[tuple[str, str, int, int, int]] = []
    for value in find_links:
        path = os.path.abspath(value)
        try:
            source_stat = os.stat(path)
        except OSError:
            return None
        kind = "directory" if stat.S_ISDIR(source_stat.st_mode) else "file"
        signatures.append(
            (
                kind,
                path,
                source_stat.st_mtime_ns,
                source_stat.st_size,
                source_stat.st_ino,
            ),
        )
    return tuple(signatures)


def catalog_cache_path(cache_dir: str | None) -> str | None:
    root = cache_root(cache_dir)
    if root is None:
        return None
    return os.path.join(root, "fast-wheelhouse-catalog-v1.marshal")


@lru_cache(maxsize=16)
def load_catalog_snapshot(
    path: str,
    signatures: tuple[tuple[str, str, int, int, int], ...],
    cache_identity: tuple[int, int, int],
) -> CatalogRecords | None:
    """Load one unchanged catalog snapshot for repeated resolutions.

    Catalog records are consumed read-only by the resolver. Keeping the
    validated representation in the process avoids repeating marshal decode,
    type checks, and version parsing on warm resolutions. The file identity is
    part of the key so an external rewrite or corruption cannot be hidden by
    this optimization.
    """
    del cache_identity
    try:
        payload = load_snapshot(path)
    except (EOFError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _CATALOG_CACHE_VERSION:
        return None
    if payload.get("sources") != signatures:
        return None
    raw_records = payload.get("records")
    if not isinstance(raw_records, dict):
        return None
    # The source signatures above invalidate the snapshot when a wheel is
    # added, removed, or replaced. Do not stat and realpath every cached
    # candidate again on the warm path; the catalog was produced from these
    # absolute paths and the metadata cache separately validates wheel content.
    records: CatalogRecords = {}
    for name, raw_values in raw_records.items():
        if not isinstance(name, str) or not isinstance(raw_values, list):
            return None
        values: list[tuple[str, LocalWheelVersion]] = []
        for raw_value in raw_values:
            if (
                not isinstance(raw_value, tuple)
                or len(raw_value) != 2
                or not isinstance(raw_value[0], str)
                or not isinstance(raw_value[1], str)
                or not raw_value[1]
                or not raw_value[0].endswith(".whl")
                or not os.path.isabs(raw_value[0])
            ):
                return None
            version = parse_version(raw_value[1])
            if version is None:
                return None
            values.append((raw_value[0], version))
        records[name] = values
    return records


def load_catalog(
    cache_dir: str | None,
    find_links: list[str],
) -> tuple[str | None, CatalogRecords | None]:
    # Identities captured by a directory scan are valid only for that
    # discovery pass. Warm catalog loads must re-stat artifacts so rewrites
    # remain visible to the metadata cache.
    artifact_identity_cache.clear()
    path = catalog_cache_path(cache_dir)
    if path is None:
        return path, None
    try:
        stat = os.stat(path)
    except OSError:
        return path, None
    signatures = source_signatures(find_links)
    if signatures is None:
        return path, None
    identity = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = catalog_snapshot_store.get(path)
    if cached is not None and cached[0] == identity and cached[1] == signatures:
        return path, cached[2]
    records = load_catalog_snapshot(
        path,
        signatures,
        identity,
    )
    if records is not None:
        catalog_snapshot_store[path] = (identity, signatures, records)
        if len(catalog_snapshot_store) > 16:
            catalog_snapshot_store.pop(next(iter(catalog_snapshot_store)))
    return path, records


def save_catalog(
    path: str | None,
    find_links: list[str],
    records: CatalogRecords,
) -> None:
    if path is None:
        return
    signatures = source_signatures(find_links)
    if signatures is None:
        return
    payload = {
        "version": _CATALOG_CACHE_VERSION,
        "sources": signatures,
        "records": {
            name: [(candidate_path, version.text) for candidate_path, version in values]
            for name, values in records.items()
        },
    }
    if save_snapshot(path, payload):
        load_catalog_snapshot.cache_clear()
        try:
            stat = os.stat(path)
            identity: tuple[int, int, int] | None = (
                stat.st_ino,
                stat.st_mtime_ns,
                stat.st_size,
            )
        except OSError:
            identity = None
        catalog_snapshot_store[path] = (identity, signatures, records)
        if len(catalog_snapshot_store) > 16:
            catalog_snapshot_store.pop(next(iter(catalog_snapshot_store)))


def scan_catalog(find_links: list[str]) -> CatalogRecords | None:
    artifact_identity_cache.clear()
    records: CatalogRecords = {}
    for value in find_links:
        directory = value if os.path.isabs(value) else os.path.abspath(value)
        if directory.endswith(".whl"):
            try:
                source_stat = os.stat(directory)
            except OSError:
                return None
            if not stat.S_ISREG(source_stat.st_mode):
                return None
            parsed = wheel_name(directory)
            if parsed is None:
                return None
            artifact_identity_cache[directory] = (
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
            )
            records.setdefault(parsed[0], []).append((directory, parsed[1]))
            continue
        snapshot = local_source_snapshot(directory, suffixes=(".whl",))
        if snapshot is not None:
            for item in snapshot.entries:
                filename = os.path.basename(item.path)
                if not filename.endswith(".whl"):
                    continue
                path = item.path
                artifact_identity_cache[path] = item.stat_identity
                parsed = parse_wheel_filename(filename)
                if parsed is None:
                    return None
                records.setdefault(parsed[0], []).append((path, parsed[1]))
            continue
        return None
    if not records:
        return None
    for values in records.values():
        values.sort(key=lambda item: item[1]._normalized)
    return records


def build_catalog_indexes(records: CatalogRecords) -> CatalogIndexes:
    """Return read-only search indexes for one catalog snapshot."""
    cache_key = id(records)
    cached = catalog_indexes_cache.get(cache_key)
    if cached is not None and cached[0] is records:
        return cached[1]
    exact_index: ExactIndex = {}
    exact_masks: ExactMasks = {}
    range_index: dict[str, RangeIndex] = {}
    for name, values_for_name in records.items():
        versions = exact_index.setdefault(name, {})
        masks = exact_masks.setdefault(name, {})
        keys: list[tuple[int, ...]] = []
        values: list[tuple[str, LocalWheelVersion]] = []
        for index, item in enumerate(values_for_name):
            normalized_key = item[1]._normalized
            versions.setdefault(normalized_key, []).append(item)
            masks[normalized_key] = masks.get(normalized_key, 0) | (1 << index)
            keys.append(normalized_key)
            values.append(item)
        range_index[name] = (
            tuple(keys),
            tuple(values),
        )
    indexes = (exact_index, exact_masks, range_index)
    catalog_indexes_cache[cache_key] = (records, indexes)
    if len(catalog_indexes_cache) > 16:
        catalog_indexes_cache.pop(next(iter(catalog_indexes_cache)))
    return indexes
