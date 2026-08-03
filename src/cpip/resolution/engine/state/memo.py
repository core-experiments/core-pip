"""Bounded, collision-safe memoization for failed resolver states."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from cpip.resolution.engine.metrics import ResolutionMetrics

StateKey: TypeAlias = tuple[object, ...]
StateFingerprint: TypeAlias = tuple[int, int, int, int, int, int]


class FailedStateMemo:
    """An LRU of exact failed states indexed by a cheap fingerprint.

    Fingerprints are only a negative lookup accelerator. A hit is accepted only
    after comparing the exact state key, so hash collisions cannot change the
    resolver result.
    """

    __slots__ = (
        "buckets",
        "entries",
        "max_entries",
        "max_tokens",
        "metrics",
        "tokens",
    )

    def __init__(
        self,
        metrics: ResolutionMetrics,
        *,
        max_entries: int = 4096,
        max_tokens: int = 1_000_000,
    ) -> None:
        self.metrics = metrics
        self.max_entries = max_entries
        self.max_tokens = max_tokens
        self.entries: OrderedDict[
            StateKey, tuple[StateFingerprint, int]
        ] = OrderedDict()
        self.buckets: dict[StateFingerprint, set[StateKey]] = {}
        self.tokens = 0

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def clear(self) -> None:
        self.entries.clear()
        self.buckets.clear()
        self.tokens = 0

    def contains(
        self,
        fingerprint: StateFingerprint,
        key_factory: Callable[[], StateKey],
    ) -> tuple[bool, StateKey | None]:
        """Return a verified hit and any exact key materialized for the lookup."""
        self.metrics.state_memo_lookups += 1
        bucket = self.buckets.get(fingerprint)
        if not bucket:
            return False, None
        self.metrics.state_key_builds += 1
        key = key_factory()
        if key not in bucket:
            return False, key
        self.entries.move_to_end(key)
        self.metrics.state_memo_hits += 1
        return True, key

    def add(
        self,
        fingerprint: StateFingerprint,
        key: StateKey,
        *,
        tokens: int,
    ) -> None:
        existing = self.entries.get(key)
        if existing is not None:
            self.entries.move_to_end(key)
            return
        self.entries[key] = fingerprint, tokens
        self.buckets.setdefault(fingerprint, set()).add(key)
        self.tokens += tokens
        while (
            len(self.entries) > self.max_entries or self.tokens > self.max_tokens
        ):
            old_key, (old_fingerprint, old_tokens) = self.entries.popitem(last=False)
            bucket = self.buckets[old_fingerprint]
            bucket.remove(old_key)
            if not bucket:
                self.buckets.pop(old_fingerprint)
            self.tokens -= old_tokens
            self.metrics.state_memo_evictions += 1
        self.metrics.state_memo_entries = max(
            self.metrics.state_memo_entries,
            len(self.entries),
        )
        self.metrics.state_memo_tokens = max(
            self.metrics.state_memo_tokens,
            self.tokens,
        )


def state_token_count(key: StateKey) -> int:
    """Estimate retained state size using canonical leaf-token counts."""
    total = 0
    stack: list[object] = [key]
    while stack:
        value = stack.pop()
        if isinstance(value, tuple):
            stack.extend(value)
        else:
            total += 1
    return total


__all__ = [
    "FailedStateMemo",
    "StateFingerprint",
    "StateKey",
    "state_token_count",
]
