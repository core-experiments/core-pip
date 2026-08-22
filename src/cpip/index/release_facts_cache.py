"""Persistent cache for deterministic release-level rejection facts."""

from __future__ import annotations

import atexit
import os
from typing import cast

from cpip.core.utils import load_snapshot, save_snapshot

NAME = "release-facts.marshal"
MAX_ENTRIES = 32_768
INSTANCES: dict[str, ReleaseFactsCache] = {}
FactKey = tuple[str, str, str]


class ReleaseFactsCache:
    """Atomic, versioned cache for facts that are safe to reuse."""

    __slots__ = ("dirty", "entries", "path")

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), NAME)
        self.entries: dict[FactKey, str] = {}
        self.dirty = False
        self.load()
        atexit.register(self.flush)

    def load(self) -> None:
        payload = load_snapshot(self.path)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or payload[0] != "cpip-release-facts"
            or not isinstance(payload[1], dict)
        ):
            return
        for key, value in payload[1].items():
            if self.valid_key(key) and isinstance(value, str):
                self.entries[cast("FactKey", key)] = value

    @staticmethod
    def valid_key(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 3
            and all(isinstance(item, str) for item in value)
        )

    def get(self, key: FactKey) -> str | None:
        return self.entries.get(key)

    def put(self, key: FactKey, reason: str) -> None:
        if key not in self.entries and len(self.entries) >= MAX_ENTRIES:
            self.entries.pop(next(iter(self.entries)))
        self.entries[key] = reason
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        if save_snapshot(
            self.path,
            ("cpip-release-facts", self.entries),
        ):
            self.dirty = False


def get_release_facts_cache(
    cache_dir: str | os.PathLike[str],
) -> ReleaseFactsCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = INSTANCES.setdefault(key, ReleaseFactsCache(key))
    return cache
