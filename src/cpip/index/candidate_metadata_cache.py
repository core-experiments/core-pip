"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

import atexit
import os
from typing import cast

from cpip.core.marshal_cache import load_snapshot, save_snapshot
from cpip.core.packaging import Requirement, Version, parse_requirement
from cpip.index.source_models import CandidateMetadata

VERSION = 3
NAME = "candidate-metadata-v3.marshal"
LEGACY_VERSION = 2
LEGACY_NAME = "candidate-metadata-v2.marshal"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
CacheKey = tuple[str, str, tuple[str, ...], str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache:
    """Process-local metadata cache backed by an atomic marshal snapshot."""

    __slots__ = (
        "decoded",
        "decoded_requirements",
        "dirty",
        "entries",
        "path",
        "requirement_states",
        "validated",
        "version_states",
    )

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), NAME)
        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}
        self.decoded_requirements: dict[str, Requirement] = {}
        self.requirement_states: dict[str, tuple[object, ...]] = {}
        self.version_states: dict[str, tuple[object, ...]] = {}
        self.validated: set[CacheKey] = set()
        self.dirty = False
        self.load()
        atexit.register(self.flush)

    def load(self) -> None:
        payload = load_snapshot(self.path)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 5
            or payload[0] != "cpip-candidate-metadata"
            or payload[1] != VERSION
            or not isinstance(payload[2], dict)
            or not isinstance(payload[3], dict)
            or not isinstance(payload[4], dict)
        ):
            legacy_path = os.path.join(os.path.dirname(self.path), LEGACY_NAME)
            payload = load_snapshot(legacy_path)
            if (
                not isinstance(payload, tuple)
                or len(payload) != 3
                or payload[0] != "cpip-candidate-metadata"
                or payload[1] != LEGACY_VERSION
                or not isinstance(payload[2], dict)
            ):
                return
            self.entries = cast("dict[CacheKey, CacheValue]", payload[2])
            self.dirty = True
            return
        # Marshal produces a new, process-local object graph, so retaining the
        # decoded mapping is safe. Validate entries when they are requested
        # instead of making resolver startup proportional to the full cache.
        self.entries = cast("dict[CacheKey, CacheValue]", payload[2])
        self.requirement_states = cast(
            "dict[str, tuple[object, ...]]",
            payload[3],
        )
        self.version_states = cast(
            "dict[str, tuple[object, ...]]",
            payload[4],
        )

    @staticmethod
    def valid_key(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 4
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], tuple)
            and all(isinstance(item, str) for item in value[2])
            and isinstance(value[3], str)
        )

    @staticmethod
    def valid_value(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 5
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], tuple)
            and all(isinstance(item, str) for item in value[2])
            and isinstance(value[3], tuple)
            and all(isinstance(item, str) for item in value[3])
            and (value[4] is None or isinstance(value[4], str))
        )

    def get(
        self,
        key: tuple[str, str, tuple[str, ...], str],
    ) -> CandidateMetadata | None:
        decoded = self.decoded.get(key)
        if decoded is not None:
            return decoded
        value = self.entries.get(key)
        if value is None or (key not in self.validated and not self.valid_value(value)):
            if value is not None:
                self.entries.pop(key, None)
                self.dirty = True
            return None
        self.validated.add(key)
        dependencies: list[Requirement] = []
        for raw in value[2]:
            requirement = self.decode_requirement(raw)
            if requirement is None:
                self.entries.pop(key, None)
                self.validated.discard(key)
                self.dirty = True
                return None
            dependencies.append(requirement)
        version = self.decode_version(value[1])
        if version is None:
            self.entries.pop(key, None)
            self.validated.discard(key)
            self.dirty = True
            return None
        metadata = CandidateMetadata(
            name=value[0],
            version=version,
            dependencies=tuple(dependencies),
            provided_extras=frozenset(value[3]),
            requires_python=value[4],
        )
        self.decoded[key] = metadata
        return metadata

    def decode_requirement(self, raw: str) -> Requirement | None:
        decoded = self.decoded_requirements.get(raw)
        if decoded is not None:
            return decoded
        state = self.requirement_states.get(raw)
        if state is not None:
            try:
                requirement = Requirement.from_cache_state(state)
            except (IndexError, TypeError, ValueError):
                requirement = None
            if requirement is not None and requirement.raw == raw:
                self.decoded_requirements[raw] = requirement
                return requirement
        try:
            requirement = parse_requirement(raw)
        except ValueError:
            return None
        self.requirement_states[raw] = requirement.cache_state_internal()
        self.decoded_requirements[raw] = requirement
        self.dirty = True
        return requirement

    def decode_version(self, raw: str) -> Version | None:
        state = self.version_states.get(raw)
        if state is not None:
            try:
                version = Version.from_cache_state(state)
            except (IndexError, TypeError, ValueError):
                version = None
            if version is not None and str(version) == raw:
                return version
        try:
            version = Version(raw)
        except ValueError:
            return None
        self.version_states[raw] = version.cache_state_internal()
        self.dirty = True
        return version

    def contains(self, key: tuple[str, str, tuple[str, ...], str]) -> bool:
        """Check for cached metadata without decoding its requirements."""
        value = self.entries.get(key)
        if value is None:
            return False
        if key in self.validated or self.valid_value(value):
            self.validated.add(key)
            return True
        self.entries.pop(key, None)
        self.dirty = True
        return False

    def put(
        self,
        key: tuple[str, str, tuple[str, ...], str],
        metadata: CandidateMetadata,
    ) -> None:
        if key not in self.entries and len(self.entries) >= MAX_ENTRIES:
            evicted = next(iter(self.entries))
            self.entries.pop(evicted)
            self.decoded.pop(evicted, None)
            self.validated.discard(evicted)
        self.entries[key] = (
            metadata.name,
            str(metadata.version),
            tuple(dependency.raw for dependency in metadata.dependencies),
            tuple(sorted(metadata.provided_extras)),
            metadata.requires_python,
        )
        self.version_states.setdefault(
            str(metadata.version),
            metadata.version.cache_state_internal(),
        )
        for dependency in metadata.dependencies:
            self.requirement_states.setdefault(
                dependency.raw,
                dependency.cache_state_internal(),
            )
            self.decoded_requirements.setdefault(dependency.raw, dependency)
        self.decoded[key] = metadata
        self.validated.add(key)
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.entries = {
            key: value
            for key, value in self.entries.items()
            if self.valid_key(key) and self.valid_value(value)
        }
        self.validated.intersection_update(self.entries)
        if save_snapshot(
            self.path,
            (
                "cpip-candidate-metadata",
                VERSION,
                self.entries,
                self.requirement_states,
                self.version_states,
            ),
        ):
            self.dirty = False


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = CandidateMetadataCache(key)
        INSTANCES[key] = cache
    return cache
