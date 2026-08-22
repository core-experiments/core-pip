"""A local wheel is hashed once per file, not once per install."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cpip.core.wheel import wheel_candidate
from cpip.index import metadata_cache
from cpip.install import wheel_archive_cache
from cpip.install.wheel_archive_cache import prepare_cached_wheel, wheel_digest

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import make_wheel  # noqa: E402


@pytest.fixture
def hashed(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record every full-file hash wheel_digest computes."""
    calls: list[None] = []
    original = hashlib.sha256

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        return original(*args, **kwargs)

    # Replace the module's reference, not hashlib itself: make_wheel hashes
    # RECORD entries through its own import and must not be counted.
    monkeypatch.setattr(
        wheel_archive_cache,
        "hashlib",
        SimpleNamespace(sha256=counting),
    )
    return calls


def _forget_process_state() -> None:
    """What a new process sees: the on-disk store only."""
    metadata_cache._CACHE_INSTANCES.clear()


def test_wheel_digest_is_hashed_once_per_unchanged_file(
    tmp_path: Path,
    hashed: list[None],
) -> None:
    wheel = make_wheel(tmp_path, "demo", "1.0.0")
    candidate = wheel_candidate(wheel)
    assert not (candidate.source_hashes or {}).get("sha256")
    cache_dir = str(tmp_path / "cache")
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()

    assert wheel_digest(candidate, cache_dir) == expected
    assert len(hashed) == 1

    metadata_cache.get_wheel_metadata_cache(cache_dir).flush()
    _forget_process_state()

    assert wheel_digest(candidate, cache_dir) == expected
    assert len(hashed) == 1, "the second install re-hashed an unchanged wheel"


def test_wheel_digest_is_recomputed_when_the_file_changes(
    tmp_path: Path,
    hashed: list[None],
) -> None:
    wheel = make_wheel(tmp_path, "demo", "1.0.0")
    candidate = wheel_candidate(wheel)
    cache_dir = str(tmp_path / "cache")
    first = wheel_digest(candidate, cache_dir)
    metadata_cache.get_wheel_metadata_cache(cache_dir).flush()
    _forget_process_state()

    wheel.write_bytes(wheel.read_bytes() + b"\0")

    assert wheel_digest(candidate, cache_dir) != first
    assert len(hashed) == 2


def test_wheel_digest_without_a_cache_directory_always_hashes(
    tmp_path: Path,
    hashed: list[None],
) -> None:
    wheel = make_wheel(tmp_path, "demo", "1.0.0")
    candidate = wheel_candidate(wheel)

    assert wheel_digest(candidate) == wheel_digest(candidate)
    assert len(hashed) == 2


def test_prepare_cached_wheel_reuses_the_recorded_digest(
    tmp_path: Path,
    hashed: list[None],
) -> None:
    """The archive-cache lookup on a warm install reads no wheel bytes."""
    wheel = make_wheel(tmp_path, "demo", "1.0.0")
    candidate = wheel_candidate(wheel)
    cache_dir = str(tmp_path / "cache")

    first = prepare_cached_wheel(candidate, cache_dir)
    metadata_cache.get_wheel_metadata_cache(cache_dir).flush()
    _forget_process_state()
    hashed.clear()

    second = prepare_cached_wheel(candidate, cache_dir)

    assert second.digest == first.digest
    assert second.tree == first.tree
    assert hashed == []


def test_prepare_cached_wheels_reads_the_digests_once(
    tmp_path: Path,
    hashed: list[None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm batch costs one digest query, not one per wheel."""
    from cpip.install.wheel_archive_cache import prepare_cached_wheels

    wheels = [make_wheel(tmp_path, f"demo-{index}", "1.0.0") for index in range(4)]
    candidates = tuple(wheel_candidate(str(wheel)) for wheel in wheels)
    cache_dir = str(tmp_path / "cache")
    prepare_cached_wheels(candidates, cache_dir)
    metadata_cache.get_wheel_metadata_cache(cache_dir).flush()
    _forget_process_state()
    hashed.clear()

    queries: list[str] = []
    original = metadata_cache.WheelMetadataCache._reader

    def counting(self: metadata_cache.WheelMetadataCache):  # type: ignore[no-untyped-def]
        queries.append("reader")
        return original(self)

    monkeypatch.setattr(metadata_cache.WheelMetadataCache, "_reader", counting)

    archives = prepare_cached_wheels(candidates, cache_dir)

    assert len(archives) == 4
    assert hashed == []
    assert len(queries) == 1, queries


def test_threaded_preparation_records_every_digest(
    tmp_path: Path,
    hashed: list[None],
) -> None:
    """The thread pool preparing a batch must not lose digests to a racing
    cache instance; the next install then hashes nothing."""
    from cpip.install.wheel_archive_cache import prepare_cached_wheels

    wheels = [make_wheel(tmp_path, f"demo-{index}", "1.0.0") for index in range(12)]
    candidates = tuple(wheel_candidate(str(wheel)) for wheel in wheels)
    cache_dir = str(tmp_path / "cache")
    for _ in range(3):
        _forget_process_state()
        prepare_cached_wheels(candidates, cache_dir)
        metadata_cache.get_wheel_metadata_cache(cache_dir).flush()

    _forget_process_state()
    hashed.clear()
    for candidate in candidates:
        candidate.wheel_layout = None
    prepare_cached_wheels(candidates, cache_dir)

    assert hashed == []
