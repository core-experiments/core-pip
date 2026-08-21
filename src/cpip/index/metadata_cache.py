"""Small, versioned persistent cache for parsed wheel metadata headers."""

from __future__ import annotations

import marshal
import os
import sqlite3
from typing import TypeAlias

from cpip.index.sqlite_cache import SqliteBackedCache

MetadataHeaders: TypeAlias = dict[str, list[str]]
MetadataIdentity: TypeAlias = tuple[str, int, int]

NAME = "metadata.sqlite"
_MAX_ENTRIES = 8_192
_CACHE_INSTANCES: dict[str, WheelMetadataCache] = {}


class WheelMetadataCache(SqliteBackedCache):
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = ("_pending_puts", "entries")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))
        self.entries: dict[MetadataIdentity, MetadataHeaders] = {}
        self._pending_puts: dict[MetadataIdentity, MetadataHeaders] = {}

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS metadata ("
        "path TEXT, size INTEGER, mtime INTEGER, headers BLOB, "
        "PRIMARY KEY (path, size, mtime))"
    )

    @staticmethod
    def valid_headers(value: object) -> bool:
        return isinstance(value, dict) and all(
            isinstance(name, str)
            and isinstance(values, list)
            and all(isinstance(item, str) for item in values)
            for name, values in value.items()
        )

    def get(self, identity: MetadataIdentity) -> MetadataHeaders | None:
        value = self.entries.get(identity)
        if value is None:
            value = self._load(identity)

        return (
            None
            if value is None
            else {name: list(values) for name, values in value.items()}
        )

    def get_reference(self, identity: MetadataIdentity) -> MetadataHeaders | None:
        """Return cached headers without copying for read-only hot paths."""
        value = self.entries.get(identity)
        if value is None:
            value = self._load(identity)
        return value

    def _load(self, identity: MetadataIdentity) -> MetadataHeaders | None:
        """Read one row out of the database and memoize it."""
        with self.lock:
            try:
                conn = self._reader()
                row = (
                    None
                    if conn is None
                    else conn.execute(
                        "SELECT headers FROM metadata "
                        "WHERE path = ? AND size = ? AND mtime = ?",
                        identity,
                    ).fetchone()
                )
            except sqlite3.Error:
                return None
        if row is None:
            return None
        try:
            value = marshal.loads(row[0])
        except Exception:
            return None
        if not self.valid_headers(value):
            return None
        if len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self.entries[identity] = value
        return value

    def put(self, identity: MetadataIdentity, headers: MetadataHeaders) -> None:
        if identity not in self.entries and len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        copied = {name: list(values) for name, values in headers.items()}
        self.entries[identity] = copied
        self._pending_puts[identity] = copied
        self.dirty = True

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        # Batch insert/replace dirty entries
        items = [
            (identity[0], identity[1], identity[2], marshal.dumps(headers))
            for identity, headers in self._pending_puts.items()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO metadata (path, size, mtime, headers) VALUES (?, ?, ?, ?)",
            items,
        )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()


def metadata_identity(path: str | os.PathLike[str]) -> MetadataIdentity | None:
    """Return a cheap invalidation key for a local artifact."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(os.fspath(path)), stat.st_size, stat.st_mtime_ns)


def get_wheel_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> WheelMetadataCache:
    """Return one cache instance per process and cache directory."""
    key = os.path.abspath(os.fspath(cache_dir))
    cache = _CACHE_INSTANCES.get(key)
    if cache is None:
        cache = WheelMetadataCache(key)
        _CACHE_INSTANCES[key] = cache
    return cache
