"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

import json
import marshal
import os
import sqlite3

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

    __slots__ = ("_pending_puts", "decoded", "entries", "validated")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))

        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}
        self.validated: set[CacheKey] = set()

        self._pending_puts: dict[CacheKey, CacheValue] = {}

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS candidate_metadata ("
            "key TEXT PRIMARY KEY, value BLOB)"
        )
        return conn

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
            # Query SQLite
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
                    row = None
            if row is not None:
                try:
                    value = marshal.loads(row[0])
                    if self.valid_value(value):
                        if len(self.entries) >= MAX_ENTRIES:
                            evicted = next(iter(self.entries))
                            self.entries.pop(evicted, None)
                            self.decoded.pop(evicted, None)
                            self.validated.discard(evicted)
                        self.entries[key] = value
                except Exception:  # noqa: BLE001, S110
                    pass

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

    def contains(self, key: tuple[str, str, tuple[str, ...], str]) -> bool:
        """Check for cached metadata without decoding its requirements."""
        value = self.entries.get(key)
        if value is None:
            # Query SQLite
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
                    row = None
            if row is not None:
                try:
                    value = marshal.loads(row[0])
                    if self.valid_value(value):
                        if len(self.entries) >= MAX_ENTRIES:
                            evicted = next(iter(self.entries))
                            self.entries.pop(evicted, None)
                            self.decoded.pop(evicted, None)
                            self.validated.discard(evicted)
                        self.entries[key] = value
                except Exception:  # noqa: BLE001, S110
                    pass

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

        value = (
            metadata.name,
            str(metadata.version),
            tuple(dependency.raw for dependency in metadata.dependencies),
            tuple(sorted(metadata.provided_extras)),
            metadata.requires_python,
        )
        self.entries[key] = value
        self._pending_puts[key] = value

        self.decoded[key] = metadata
        self.validated.add(key)
        self.dirty = True

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        # Batch insert pending candidate metadata
        items = [
            (json.dumps(k), marshal.dumps(v))
            for k, v in self._pending_puts.items()
            if self.valid_key(k) and self.valid_value(v)
        ]
        if items:
            conn.executemany(
                "INSERT OR REPLACE INTO candidate_metadata (key, value) VALUES (?, ?)",
                items,
            )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = CandidateMetadataCache(key)
        INSTANCES[key] = cache
    return cache
