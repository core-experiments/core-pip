"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

import json
import marshal
import os
import sqlite3
from typing import cast

from cpip.core.utils import load_snapshot
from cpip.core.packaging import Requirement, Version, parse_requirement
from cpip.index.source_models import CandidateMetadata
from cpip.index.sqlite_cache import SqliteBackedCache

VERSION = 3
NAME = "candidate-metadata-v3.sqlite"
LEGACY_VERSION = 2
LEGACY_NAME = "candidate-metadata-v2.marshal"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
CacheKey = tuple[str, str, tuple[str, ...], str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache(SqliteBackedCache):
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = (
        "_pending_puts",
        "_pending_req_states",
        "_pending_ver_states",
        "decoded",
        "decoded_requirements",
        "entries",
        "requirement_states",
        "validated",
        "version_states",
    )

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))

        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}
        self.decoded_requirements: dict[str, Requirement] = {}
        self.requirement_states: dict[str, tuple[object, ...]] = {}
        self.version_states: dict[str, tuple[object, ...]] = {}
        self.validated: set[CacheKey] = set()

        self._pending_puts: dict[CacheKey, CacheValue] = {}
        self._pending_req_states: dict[str, tuple[object, ...]] = {}
        self._pending_ver_states: dict[str, tuple[object, ...]] = {}

        # Check if the file at self.path exists and is a legacy marshal snapshot
        legacy_payload = None
        if self._db_exists:
            try:
                payload = load_snapshot(self.path)
                if (
                    payload
                    and isinstance(payload, tuple)
                    and len(payload) >= 3
                    and payload[0] == "cpip-candidate-metadata"
                ):
                    legacy_payload = payload
            except Exception:
                pass

        if legacy_payload is not None:
            with self.lock:
                # It's a legacy marshal file. Rename it so the database can
                # take its place; the migrating flush creates the database.
                temp_path = self.path + ".migration"
                try:
                    os.rename(self.path, temp_path)
                    self._db_exists = False
                    self.migrate_payload(legacy_payload)
                    os.remove(temp_path)
                except OSError:
                    pass

        # Run other legacy migrations if SQLite is still empty
        self.load_other_legacy()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS candidate_metadata ("
            "key TEXT PRIMARY KEY, value BLOB)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS requirement_states ("
            "raw TEXT PRIMARY KEY, state BLOB)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS version_states ("
            "raw TEXT PRIMARY KEY, state BLOB)"
        )
        return conn

    def migrate_payload(self, payload: tuple) -> None:
        try:
            if len(payload) >= 3 and payload[1] == LEGACY_VERSION:
                entries = cast("dict[CacheKey, CacheValue]", payload[2])
                for k, v in entries.items():
                    if self.valid_key(k) and self.valid_value(v):
                        self._pending_puts[k] = v
                self.dirty = True
                self.flush()
            elif len(payload) >= 5 and payload[1] == VERSION:
                entries = cast("dict[CacheKey, CacheValue]", payload[2])
                req_states = cast("dict[str, tuple[object, ...]]", payload[3])
                ver_states = cast("dict[str, tuple[object, ...]]", payload[4])

                for k, v in entries.items():
                    if self.valid_key(k) and self.valid_value(v):
                        self._pending_puts[k] = v
                for raw, state in req_states.items():
                    self._pending_req_states[raw] = state
                for raw, state in ver_states.items():
                    self._pending_ver_states[raw] = state
                self.dirty = True
                self.flush()
        except Exception:
            pass

    def load_other_legacy(self) -> None:
        # Checked before the database is consulted: with no snapshot to import
        # there is nothing to decide, and a cold run never opens a connection.
        legacy_path = os.path.join(os.path.dirname(self.path), LEGACY_NAME)
        if not os.path.isfile(legacy_path):
            return

        try:
            conn = self._reader()
            if (
                conn is not None
                and conn.execute(
                    "SELECT 1 FROM candidate_metadata LIMIT 1",
                ).fetchone()
                is not None
            ):
                return
        except sqlite3.Error:
            return

        payload = load_snapshot(legacy_path)
        if isinstance(payload, tuple):
            self.migrate_payload(payload)

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
                except Exception:
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

    def decode_requirement(self, raw: str) -> Requirement | None:
        decoded = self.decoded_requirements.get(raw)
        if decoded is not None:
            return decoded

        state = self.requirement_states.get(raw)
        if state is None:
            # Query SQLite
            with self.lock:
                try:
                    conn = self._reader()
                    row = (
                        None
                        if conn is None
                        else conn.execute(
                            "SELECT state FROM requirement_states WHERE raw = ?",
                            (raw,),
                        ).fetchone()
                    )
                except sqlite3.Error:
                    row = None
            if row is not None:
                try:
                    state = marshal.loads(row[0])
                    self.requirement_states[raw] = state
                except Exception:
                    pass

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

        state = requirement.cache_state_internal()
        self.requirement_states[raw] = state
        self._pending_req_states[raw] = state
        self.decoded_requirements[raw] = requirement
        self.dirty = True
        return requirement

    def decode_version(self, raw: str) -> Version | None:
        state = self.version_states.get(raw)
        if state is None:
            # Query SQLite
            with self.lock:
                try:
                    conn = self._reader()
                    row = (
                        None
                        if conn is None
                        else conn.execute(
                            "SELECT state FROM version_states WHERE raw = ?",
                            (raw,),
                        ).fetchone()
                    )
                except sqlite3.Error:
                    row = None
            if row is not None:
                try:
                    state = marshal.loads(row[0])
                    self.version_states[raw] = state
                except Exception:
                    pass

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

        state = version.cache_state_internal()
        self.version_states[raw] = state
        self._pending_ver_states[raw] = state
        self.dirty = True
        return version

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
                except Exception:
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

        ver_state = metadata.version.cache_state_internal()
        self.version_states[str(metadata.version)] = ver_state
        self._pending_ver_states[str(metadata.version)] = ver_state

        for dependency in metadata.dependencies:
            req_state = dependency.cache_state_internal()
            self.requirement_states[dependency.raw] = req_state
            self._pending_req_states[dependency.raw] = req_state
            self.decoded_requirements[dependency.raw] = dependency

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

        # Batch insert pending requirement states
        req_items = [
            (raw, marshal.dumps(state))
            for raw, state in self._pending_req_states.items()
        ]
        if req_items:
            conn.executemany(
                "INSERT OR REPLACE INTO requirement_states (raw, state) VALUES (?, ?)",
                req_items,
            )

        # Batch insert pending version states
        ver_items = [
            (raw, marshal.dumps(state))
            for raw, state in self._pending_ver_states.items()
        ]
        if ver_items:
            conn.executemany(
                "INSERT OR REPLACE INTO version_states (raw, state) VALUES (?, ?)",
                ver_items,
            )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()
        self._pending_req_states.clear()
        self._pending_ver_states.clear()


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = CandidateMetadataCache(key)
        INSTANCES[key] = cache
    return cache
