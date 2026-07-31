"""Bounded background work used by the resolver's catalog prefetcher."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Callable, Generic, Hashable, TypeVar

T = TypeVar("T")
V = TypeVar("V")


class Prefetcher(Generic[T, V]):
    """Submit each keyed task once and consume it deterministically."""

    def __init__(self, loader: Callable[[V], T], max_workers: int) -> None:
        self.loader = loader
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.futures: dict[Hashable, Future[T]] = {}
        self.lock = RLock()
        self.closed = False

    def submit(self, key: Hashable, value: V) -> None:
        with self.lock:
            if self.closed or key in self.futures:
                return
            self.futures[key] = self.executor.submit(self.loader, value)

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
