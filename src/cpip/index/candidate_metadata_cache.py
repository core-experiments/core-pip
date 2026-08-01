"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

import atexit
import os
from typing import cast

from cpip.core.marshal_cache import load_snapshot, save_snapshot
from cpip.core.packaging import Version, parse_requirement
from cpip.index.source_models import CandidateMetadata

VERSION = 2
NAME = "candidate-metadata-v2.marshal"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
CacheKey = tuple[str, str, tuple[str, ...], str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache:
    """Process-local metadata cache backed by an atomic marshal snapshot."""

    __slots__ = ("decoded", "dirty", "entries", "path")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), NAME)
        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}
        self.dirty = False
        self.load()
        atexit.register(self.flush)

    def load(self) -> None:
        payload = load_snapshot(self.path)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "cpip-candidate-metadata"
            or payload[1] != VERSION
            or not isinstance(payload[2], dict)
        ):
            return
        for key, value in payload[2].items():
            if self.valid_key(key) and self.valid_value(value):
                self.entries[cast("CacheKey", key)] = cast("CacheValue", value)

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
        if value is None:
            return None
        dependencies = tuple(
            requirement
            for raw in value[2]
            if (requirement := parse_requirement(raw)) is not None
        )
        if len(dependencies) != len(value[2]):
            return None
        metadata = CandidateMetadata(
            name=value[0],
            version=Version(value[1]),
            dependencies=dependencies,
            provided_extras=frozenset(value[3]),
            requires_python=value[4],
        )
        self.decoded[key] = metadata
        return metadata

    def put(
        self,
        key: tuple[str, str, tuple[str, ...], str],
        metadata: CandidateMetadata,
    ) -> None:
        if key not in self.entries and len(self.entries) >= MAX_ENTRIES:
            evicted = next(iter(self.entries))
            self.entries.pop(evicted)
            self.decoded.pop(evicted, None)
        self.entries[key] = (
            metadata.name,
            str(metadata.version),
            tuple(dependency.raw for dependency in metadata.dependencies),
            tuple(sorted(metadata.provided_extras)),
            metadata.requires_python,
        )
        self.decoded[key] = metadata
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        if save_snapshot(
            self.path,
            ("cpip-candidate-metadata", VERSION, self.entries),
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
