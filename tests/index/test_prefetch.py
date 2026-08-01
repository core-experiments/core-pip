from __future__ import annotations

import threading
import time

import pytest
from cpip.index.prefetch import Prefetcher, PrefetchPolicy


def test_prefetch_policy_prefers_fast_high_yield_sources() -> None:
    policy = PrefetchPolicy()
    policy.observe("slow", 1.0, 10)
    policy.observe("fast", 0.1, 10)

    assert policy.priority("fast") > policy.priority("slow")


def test_prefetcher_deduplicates_and_overlaps_work() -> None:
    lock = threading.Lock()
    active = 0
    maximum = 0
    calls: list[str] = []
    started = threading.Event()

    def load(value: str) -> str:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(value)
        started.set()
        time.sleep(0.05)
        with lock:
            active -= 1
        return value.upper()

    prefetcher = Prefetcher(load, max_workers=2)
    try:
        prefetcher.submit("first", "first")
        prefetcher.submit("first", "duplicate")
        prefetcher.submit("second", "second")
        assert started.wait(1)
        assert prefetcher.take("first").result() == "FIRST"
        assert prefetcher.take("second").result() == "SECOND"
        assert calls == ["first", "second"]
        assert maximum == 2
    finally:
        prefetcher.close()


def test_prefetcher_propagates_loader_errors() -> None:
    def load(value: str) -> str:
        raise ValueError(value)

    prefetcher = Prefetcher(load, max_workers=1)
    try:
        prefetcher.submit("failure", "broken")
        with pytest.raises(ValueError, match="broken"):
            prefetcher.take("failure").result()
    finally:
        prefetcher.close()
