"""Small, versioned persistent cache for parsed wheel metadata headers."""

from __future__ import annotations

import atexit
import marshal
import os
import sqlite3
from typing import TypeAlias, cast

MetadataHeaders: TypeAlias = dict[str, list[str]]
MetadataIdentity: TypeAlias = tuple[str, int, int]

_CACHE_VERSION = 2
_CACHE_NAME = "metadata-v2.sqlite"
_MAX_ENTRIES = 8_192
_CACHE_INSTANCES: dict[str, WheelMetadataCache] = {}


class WheelMetadataCache:
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = ("dirty", "entries", "path", "conn", "_pending_puts")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), _CACHE_NAME)
        # Create directories if they do not exist
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "path TEXT, size INTEGER, mtime INTEGER, headers BLOB, "
                "PRIMARY KEY (path, size, mtime))"
            )
        except sqlite3.Error:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.conn = sqlite3.connect(self.path)
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata ("
                "path TEXT, size INTEGER, mtime INTEGER, headers BLOB, "
                "PRIMARY KEY (path, size, mtime))"
            )
        
        self.entries: dict[MetadataIdentity, MetadataHeaders] = {}
        self._pending_puts: dict[MetadataIdentity, MetadataHeaders] = {}
        self.dirty = False
        atexit.register(self.flush)

    def load(self) -> None:
        # No-op in SQLite because we load rows on-demand during get()
        pass

    @staticmethod
    def valid_identity(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 3
            and isinstance(value[0], str)
            and isinstance(value[1], int)
            and isinstance(value[2], int)
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
            # Query SQLite
            cursor = self.conn.execute(
                "SELECT headers FROM metadata WHERE path = ? AND size = ? AND mtime = ?",
                identity,
            )
            row = cursor.fetchone()
            if row is not None:
                try:
                    value = marshal.loads(row[0])
                    if self.valid_headers(value):
                        if len(self.entries) >= _MAX_ENTRIES:
                            self.entries.pop(next(iter(self.entries)))
                        self.entries[identity] = value
                except Exception:
                    pass
        
        return (
            None
            if value is None
            else {name: list(values) for name, values in value.items()}
        )

    def get_reference(self, identity: MetadataIdentity) -> MetadataHeaders | None:
        """Return cached headers without copying for read-only hot paths."""
        value = self.entries.get(identity)
        if value is None:
            # Query SQLite
            cursor = self.conn.execute(
                "SELECT headers FROM metadata WHERE path = ? AND size = ? AND mtime = ?",
                identity,
            )
            row = cursor.fetchone()
            if row is not None:
                try:
                    value = marshal.loads(row[0])
                    if self.valid_headers(value):
                        if len(self.entries) >= _MAX_ENTRIES:
                            self.entries.pop(next(iter(self.entries)))
                        self.entries[identity] = value
                except Exception:
                    pass
        return value

    def put(self, identity: MetadataIdentity, headers: MetadataHeaders) -> None:
        if identity not in self.entries and len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        copied = {name: list(values) for name, values in headers.items()}
        self.entries[identity] = copied
        self._pending_puts[identity] = copied
        self.dirty = True

    def put_reference(
        self,
        identity: MetadataIdentity,
        headers: MetadataHeaders,
    ) -> None:
        """Store already-owned immutable headers without copying them."""
        if identity not in self.entries and len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self.entries[identity] = headers
        self._pending_puts[identity] = headers
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        
        try:
            # Batch insert/replace dirty entries
            items = [
                (identity[0], identity[1], identity[2], marshal.dumps(headers))
                for identity, headers in self._pending_puts.items()
            ]
            self.conn.executemany(
                "INSERT OR REPLACE INTO metadata (path, size, mtime, headers) VALUES (?, ?, ?, ?)",
                items,
            )
            self.conn.commit()
            self._pending_puts.clear()
            self.dirty = False
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.flush()
            self.conn.close()
        except Exception:
            pass


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
