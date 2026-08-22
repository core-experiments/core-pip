"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

import json
import marshal
import os
import sqlite3
from typing import cast

from cpip.core.packaging import Requirement, parse_requirement
from cpip.core.versions import Version
from cpip.index.source_models import CandidateMetadata
from cpip.index.sqlite_cache import SqliteBackedCache

NAME = "candidate-metadata.sqlite"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
CacheKey = tuple[str, str, tuple[str, ...], str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache(SqliteBackedCache):
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = ("_pending_deletes", "_pending_puts", "decoded", "entries")

    SCHEMA = "CREATE TABLE IF NOT EXISTS candidate_metadata (key TEXT PRIMARY KEY, value BLOB);"

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))

        # Every value in ``entries`` has passed ``valid_value``: it came from
        # ``put`` or from ``_load``.
        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}

        self._pending_puts: dict[CacheKey, CacheValue] = {}
        self._pending_deletes: set[CacheKey] = set()

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

    def _load(self, key: CacheKey) -> CacheValue | None:
        """Read one row out of the database, validate it and memoize it."""
        with self.lock:
            try:
                conn = self._reader()
                row = (
                    None
                    if conn is None
                    else conn.execute(
                        "SELECT value FROM candidate_metadata WHERE key = ?",
                        (json.dumps(key),),
                    ).fetchone()
                )
            except sqlite3.Error:
                return None
        if row is None:
            return None
        try:
            loaded = marshal.loads(row[0])
        except Exception:  # noqa: BLE001
            loaded = None
        if not self.valid_value(loaded):
            # A row of the wrong shape is a miss; delete it on the next
            # flush so no later process reads and rejects it again.
            self._discard(key)
            return None
        value = cast("CacheValue", loaded)
        self._evict()
        self.entries[key] = value
        return value

    def _discard(self, key: CacheKey) -> None:
        """Forget an entry that failed decoding, in memory and on disk."""
        self.entries.pop(key, None)
        self.decoded.pop(key, None)
        self._pending_puts.pop(key, None)
        self._pending_deletes.add(key)
        self.dirty = True

    def _evict(self) -> None:
        """Make room for one more entry."""
        if len(self.entries) >= MAX_ENTRIES:
            evicted = next(iter(self.entries))
            self.entries.pop(evicted, None)
            self.decoded.pop(evicted, None)

    def get(self, key: CacheKey) -> CandidateMetadata | None:
        decoded = self.decoded.get(key)
        if decoded is not None:
            return decoded

        value = self.entries.get(key)
        if value is None:
            value = self._load(key)
            if value is None:
                return None

        dependencies: list[Requirement] = []
        for raw in value[2]:
            requirement = self.decode_requirement(raw)
            if requirement is None:
                self._discard(key)
                return None
            dependencies.append(requirement)

        version = self.decode_version(value[1])
        if version is None:
            self._discard(key)
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

    @staticmethod
    def decode_requirement(raw: str) -> Requirement | None:
        # parse_requirement interns by text, so a dependency line repeated
        # across entries costs one lookup per process.
        try:
            return parse_requirement(raw)
        except ValueError:
            return None

    @staticmethod
    def decode_version(raw: str) -> Version | None:
        try:
            return Version(raw)
        except ValueError:
            return None

    def contains(self, key: CacheKey) -> bool:
        """Check for cached metadata without decoding its requirements."""
        return key in self.entries or self._load(key) is not None

    def put(self, key: CacheKey, metadata: CandidateMetadata) -> None:
        if key not in self.entries:
            self._evict()

        value = (
            metadata.name,
            str(metadata.version),
            tuple(dependency.raw for dependency in metadata.dependencies),
            tuple(sorted(metadata.provided_extras)),
            metadata.requires_python,
        )
        self.entries[key] = value
        self.decoded[key] = metadata
        self._pending_puts[key] = value
        self._pending_deletes.discard(key)
        self.dirty = True

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        if self._pending_deletes:
            conn.executemany(
                "DELETE FROM candidate_metadata WHERE key = ?",
                [(json.dumps(key),) for key in self._pending_deletes],
            )
        items = [
            (json.dumps(key), marshal.dumps(value))
            for key, value in self._pending_puts.items()
        ]
        if items:
            conn.executemany(
                "INSERT OR REPLACE INTO candidate_metadata (key, value) VALUES (?, ?)",
                items,
            )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()
        self._pending_deletes.clear()


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = INSTANCES.setdefault(key, CandidateMetadataCache(key))
    return cache
