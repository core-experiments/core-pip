"""Persistent cache of what a local wheel file yields: its parsed metadata
headers and its SHA-256, both keyed by the file's path, size and mtime."""

from __future__ import annotations

import marshal
import os
import sqlite3
from collections.abc import Iterable
from typing import TypeAlias

from cpip.index.sqlite_cache import SqliteBackedCache

MetadataHeaders: TypeAlias = dict[str, list[str]]
MetadataIdentity: TypeAlias = tuple[str, int, int]

_HEX_DIGITS = "0123456789abcdefABCDEF"


def _valid_sha256(value: object) -> bool:
    """A 64-character hex string, the shape put_digest writes."""
    return isinstance(value, str) and len(value) == 64 and not value.strip(_HEX_DIGITS)


NAME = "metadata.sqlite"
_MAX_ENTRIES = 8_192
_CACHE_INSTANCES: dict[str, WheelMetadataCache] = {}


class WheelMetadataCache(SqliteBackedCache):
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = ("_pending_digests", "_pending_puts", "digests", "entries")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))
        self.entries: dict[MetadataIdentity, MetadataHeaders] = {}
        self.digests: dict[MetadataIdentity, str] = {}
        self._pending_puts: dict[MetadataIdentity, MetadataHeaders] = {}
        self._pending_digests: dict[MetadataIdentity, str] = {}

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS metadata ("
        "path TEXT, size INTEGER, mtime INTEGER, headers BLOB, "
        "PRIMARY KEY (path, size, mtime));"
        "CREATE TABLE IF NOT EXISTS digests ("
        "path TEXT, size INTEGER, mtime INTEGER, sha256 TEXT, "
        "PRIMARY KEY (path, size, mtime));"
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

    def prefetch(self, identities: Iterable[MetadataIdentity]) -> None:
        """Load the headers of many files in one query, so that the per-file
        ``get_reference`` that follows reads memory, not the database."""
        wanted = {identity for identity in identities if identity not in self.entries}
        if not wanted:
            return
        paths = list({identity[0] for identity in wanted})
        rows: list[tuple[str, int, int, bytes]] = []
        with self.lock:
            try:
                conn = self._reader()
                if conn is None:
                    return
                for start in range(0, len(paths), 500):
                    chunk = paths[start : start + 500]
                    rows.extend(
                        conn.execute(
                            "SELECT path, size, mtime, headers FROM metadata "
                            f"WHERE path IN ({','.join('?' * len(chunk))})",
                            chunk,
                        ).fetchall(),
                    )
            except sqlite3.Error:
                return
        for path, size, mtime, blob in rows:
            identity = (path, size, mtime)
            if identity not in wanted:
                continue
            try:
                value = marshal.loads(blob)
            except Exception:
                continue
            if not self.valid_headers(value):
                continue
            if len(self.entries) >= _MAX_ENTRIES:
                self.entries.pop(next(iter(self.entries)))
            self.entries[identity] = value

    def put(self, identity: MetadataIdentity, headers: MetadataHeaders) -> None:
        if identity not in self.entries and len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        copied = {name: list(values) for name, values in headers.items()}
        self.entries[identity] = copied
        self._pending_puts[identity] = copied
        self.dirty = True

    def get_digest(self, identity: MetadataIdentity) -> str | None:
        """The SHA-256 recorded for a file, or ``None`` when it was never hashed."""
        digest = self.digests.get(identity)
        if digest is not None:
            return digest
        with self.lock:
            try:
                conn = self._reader()
                row = (
                    None
                    if conn is None
                    else conn.execute(
                        "SELECT sha256 FROM digests "
                        "WHERE path = ? AND size = ? AND mtime = ?",
                        identity,
                    ).fetchone()
                )
            except sqlite3.Error:
                return None
        if row is None or not _valid_sha256(row[0]):
            # A malformed persisted value is a miss, not a digest: returning
            # it would key the archive cache on a non-hash.
            return None
        self.digests[identity] = row[0]
        return row[0]

    def prefetch_digests(self, identities: Iterable[MetadataIdentity]) -> None:
        """Load the recorded digests of many files in one query, so that the
        per-file ``get_digest`` that follows reads memory, not the database."""
        wanted = {identity for identity in identities if identity not in self.digests}
        if not wanted:
            return
        paths = list({identity[0] for identity in wanted})
        rows: list[tuple[str, int, int, str]] = []
        with self.lock:
            try:
                conn = self._reader()
                if conn is None:
                    return
                for start in range(0, len(paths), 500):
                    chunk = paths[start : start + 500]
                    rows.extend(
                        conn.execute(
                            "SELECT path, size, mtime, sha256 FROM digests "
                            f"WHERE path IN ({','.join('?' * len(chunk))})",
                            chunk,
                        ).fetchall(),
                    )
            except sqlite3.Error:
                return
        for path, size, mtime, digest in rows:
            identity = (path, size, mtime)
            if identity in wanted and _valid_sha256(digest):
                self.digests[identity] = digest

    def put_digest(self, identity: MetadataIdentity, digest: str) -> None:
        self.digests[identity] = digest
        self._pending_digests[identity] = digest
        self.dirty = True

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        if self._pending_puts:
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (path, size, mtime, headers) "
                "VALUES (?, ?, ?, ?)",
                [
                    (identity[0], identity[1], identity[2], marshal.dumps(headers))
                    for identity, headers in self._pending_puts.items()
                ],
            )
        if self._pending_digests:
            conn.executemany(
                "INSERT OR REPLACE INTO digests (path, size, mtime, sha256) "
                "VALUES (?, ?, ?, ?)",
                [
                    (identity[0], identity[1], identity[2], digest)
                    for identity, digest in self._pending_digests.items()
                ],
            )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()
        self._pending_digests.clear()


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
        # Threads preparing a batch may all ask at once; setdefault is one
        # atomic step, so a loser's instance is dropped before it holds any
        # entry that would otherwise never be flushed.
        cache = _CACHE_INSTANCES.setdefault(key, WheelMetadataCache(key))
    return cache
