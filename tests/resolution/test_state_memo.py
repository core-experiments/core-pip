from __future__ import annotations

from cpip.resolution.engine.metrics import ResolutionMetrics
from cpip.resolution.engine.state.memo import FailedStateMemo


def test_failed_state_memo_verifies_fingerprint_collisions() -> None:
    metrics = ResolutionMetrics()
    memo = FailedStateMemo(metrics)
    fingerprint = (1, 2, 3, 4, 5, 6)
    memo.add(fingerprint, ("first",), tokens=1)

    hit, key = memo.contains(fingerprint, lambda: ("second",))

    assert not hit
    assert key == ("second",)
    assert metrics.state_memo_hits == 0


def test_failed_state_memo_evicts_oldest_entries() -> None:
    metrics = ResolutionMetrics()
    memo = FailedStateMemo(metrics, max_entries=2, max_tokens=10)
    first = (1, 1, 1, 1, 1, 1)
    second = (2, 2, 2, 2, 2, 2)
    third = (3, 3, 3, 3, 3, 3)
    memo.add(first, ("first",), tokens=1)
    memo.add(second, ("second",), tokens=1)
    memo.add(third, ("third",), tokens=1)

    first_hit, _ = memo.contains(first, lambda: ("first",))
    third_hit, _ = memo.contains(third, lambda: ("third",))

    assert not first_hit
    assert third_hit
    assert len(memo) == 2
    assert metrics.state_memo_evictions == 1
