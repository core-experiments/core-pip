"""Shared SQLite connection/transaction lifecycle for cpip's persistent
metadata caches.

``WheelMetadataCache`` (metadata_cache.py) and ``CandidateMetadataCache``
(candidate_metadata_cache.py) each need the same thing underneath very
different schemas and payloads: lazily open a WAL-mode SQLite database (an
absent database reads as empty rather than being created, so a run that
only ever misses pays nothing), retry once from scratch if the file turns
out to be corrupt, and gate writes behind a dirty flag with a
commit-or-rollback transaction. That plumbing lived as two byte-for-byte
copies -- drift here is a real bug, not just untidy: a fix to one (say, a
change to the corrupt-file retry, or the busy_timeout) landing in only one
cache would silently be a bug in the other, with no test surface that
would notice, since both caches degrade the same way either way (a read
that should hit disk just counts as a miss instead).
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import threading


class SqliteBackedCache:
    """Base class for a process-local cache backed by an incremental
    SQLite database.

    Subclasses provide the schema (``SCHEMA``, DDL run on every open) and
    what a flush writes (``_flush_pending``, given an open connection to
    write through but not commit; ``_clear_pending``, called only once
    ``flush`` has confirmed the commit succeeded, to reset whatever
    containers ``_flush_pending`` drained).
    """

    __slots__ = ("_db_exists", "conn", "dirty", "lock", "path")

    SCHEMA = ""
    """``CREATE TABLE IF NOT EXISTS ...;`` for each of this cache's tables."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn: sqlite3.Connection | None = None
        self._db_exists = os.path.isfile(self.path)
        self.dirty = False
        atexit.register(self.flush)

    def _reader(self) -> sqlite3.Connection | None:
        """Return the connection, or ``None`` while no database exists yet.

        Creating the file costs a WAL journal plus the schema statements,
        which a run that only ever misses should not pay.  An absent
        database reads as empty instead of being brought into existence.
        """
        if self.conn is None and not self._db_exists:
            return None
        return self._writer()

    def _writer(self) -> sqlite3.Connection:
        """Return the connection, opening the database on first real use."""
        if self.conn is not None:
            return self.conn

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            conn = self._open()
        except sqlite3.Error:
            # A corrupt file is worth one retry from scratch; a second
            # failure is the caller's to handle.
            try:
                os.remove(self.path)
            except OSError:
                pass
            conn = self._open()

        self.conn = conn
        self._db_exists = True
        return conn

    def _open(self) -> sqlite3.Connection:
        """Open a WAL-mode connection and ensure this cache's schema exists."""
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(self.SCHEMA)
        return conn

    def flush(self) -> None:
        if not self.dirty:
            return

        with self.lock:
            try:
                conn = self._writer()
                self._flush_pending(conn)
                conn.commit()
                self._clear_pending()
                self.dirty = False
            except (sqlite3.Error, ValueError, TypeError, OSError):
                if self.conn is not None:
                    try:
                        self.conn.rollback()
                    except sqlite3.Error:
                        pass

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        """Write pending entries through ``conn`` without committing.

        Must not clear whatever "pending" state it wrote from -- ``flush``
        only calls ``_clear_pending`` after ``conn.commit()`` succeeds, so
        an exception raised here leaves pending state intact for the next
        flush to retry.
        """
        raise NotImplementedError

    def _clear_pending(self) -> None:
        """Clear whatever pending-state containers ``_flush_pending`` wrote
        from. Called only once ``flush`` has confirmed the commit succeeded.
        """
        raise NotImplementedError

    def __del__(self) -> None:
        try:
            self.flush()
            with self.lock:
                if self.conn is not None:
                    self.conn.close()
        except Exception:
            pass
