"""Small, versioned persistent cache for parsed wheel metadata headers."""

from __future__ import annotations

import atexit
import marshal
import os
from pathlib import Path
from typing import TypeAlias

MetadataHeaders: TypeAlias = dict[str, list[str]]
MetadataIdentity: TypeAlias = tuple[str, int, int]

_CACHE_VERSION = 2
_CACHE_NAME = "metadata-v2.marshal"
_MAX_ENTRIES = 8_192
_CACHE_INSTANCES: dict[str, WheelMetadataCache] = {}


class WheelMetadataCache:
    """Process-local metadata cache backed by an atomic marshal snapshot."""

    __slots__ = ("entries", "path", "dirty")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = Path(cache_dir) / _CACHE_NAME
        self.entries: dict[MetadataIdentity, MetadataHeaders] = {}
        self.dirty = False
        self._load()
        atexit.register(self.flush)

    def _load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                payload = marshal.load(stream)
        except (EOFError, OSError, TypeError, ValueError):
            return
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "core-pip-metadata"
            or payload[1] != _CACHE_VERSION
            or not isinstance(payload[2], dict)
        ):
            return
        for key, value in payload[2].items():
            if not self._valid_identity(key) or not self._valid_headers(value):
                continue
            self.entries[key] = value

    @staticmethod
    def _valid_identity(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 3
            and isinstance(value[0], str)
            and isinstance(value[1], int)
            and isinstance(value[2], int)
        )

    @staticmethod
    def _valid_headers(value: object) -> bool:
        return isinstance(value, dict) and all(
            isinstance(name, str)
            and isinstance(values, list)
            and all(isinstance(item, str) for item in values)
            for name, values in value.items()
        )

    def get(self, identity: MetadataIdentity) -> MetadataHeaders | None:
        value = self.entries.get(identity)
        return None if value is None else {name: list(values) for name, values in value.items()}

    def put(self, identity: MetadataIdentity, headers: MetadataHeaders) -> None:
        if identity not in self.entries and len(self.entries) >= _MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self.entries[identity] = {name: list(values) for name, values in headers.items()}
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as stream:
                marshal.dump(
                    ("core-pip-metadata", _CACHE_VERSION, self.entries),
                    stream,
                )
            os.replace(temporary, self.path)
            self.dirty = False
        except OSError:
            try:
                temporary.unlink()
            except OSError:
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
