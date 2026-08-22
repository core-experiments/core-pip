"""Bounded background work used by the resolver's catalog prefetcher."""

from __future__ import annotations

from collections.abc import Hashable
from threading import RLock
from typing import Callable, Generic, TypeVar

TYPE_CHECKING = False

if TYPE_CHECKING:
    from concurrent.futures import Future

T = TypeVar("T")
V = TypeVar("V")


class PrefetchPolicy:
    """Small adaptive scorer for independent background catalog work."""

    __slots__ = ("latency", "yield_count")

    def __init__(self) -> None:
        self.latency: dict[Hashable, float] = {}
        self.yield_count: dict[Hashable, float] = {}

    def observe(self, key: Hashable, elapsed: float, result_count: int) -> None:
        previous_latency = self.latency.get(key)
        previous_yield = self.yield_count.get(key)
        if previous_latency is None:
            self.latency[key] = elapsed
            self.yield_count[key] = float(result_count)
            return
        self.latency[key] = previous_latency * 0.75 + elapsed * 0.25
        self.yield_count[key] = (previous_yield or 0.0) * 0.75 + result_count * 0.25

    def priority(self, key: Hashable) -> float:
        latency = self.latency.get(key)
        if latency is None:
            return 0.0
        return (self.yield_count.get(key, 0.0) + 1.0) / max(latency, 1e-6)


class Prefetcher(Generic[T, V]):
    """Submit each keyed task once and consume it deterministically."""

    def __init__(self, loader: Callable[[V], T], max_workers: int) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.loader = loader
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.futures: dict[Hashable, Future[T]] = {}
        self.lock = RLock()
        self.closed = False

    def submit(self, key: Hashable, value: V) -> bool:
        with self.lock:
            if self.closed or key in self.futures:
                return False
            self.futures[key] = self.executor.submit(self.loader, value)
            return True

    def take(self, key: Hashable) -> Future[T] | None:
        with self.lock:
            return self.futures.pop(key, None)

    def pending(self, key: Hashable) -> bool:
        with self.lock:
            return key in self.futures

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
