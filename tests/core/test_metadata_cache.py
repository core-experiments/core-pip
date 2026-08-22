from __future__ import annotations

from pathlib import Path

from cpip.index.metadata_cache import NAME, WheelMetadataCache, metadata_identity


def test_metadata_cache_round_trips_versioned_headers(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"wheel")
    identity = metadata_identity(artifact)
    assert identity is not None
    headers = {"name": ["demo"], "requires-dist": ["child>=1"]}

    cache = WheelMetadataCache(tmp_path / "cache")
    cache.put(identity, headers)
    cache.flush()

    restored = WheelMetadataCache(tmp_path / "cache")
    assert restored.get(identity) == headers


def test_metadata_cache_defers_database_creation_until_a_write(tmp_path: Path) -> None:
    """A cold cache that only misses must not pay to create the database."""
    cache_dir = tmp_path / "cache"
    database = cache_dir / NAME

    cache = WheelMetadataCache(cache_dir)
    assert cache.get(("/wheel.whl", 1, 2)) is None
    assert not database.exists()

    cache.put(("/wheel.whl", 1, 2), {"Name": ["demo"]})
    cache.flush()
    assert database.is_file()


def test_metadata_cache_ignores_corrupt_snapshots(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / NAME
    cache_path.parent.mkdir()
    cache_path.write_bytes(b"not a sqlite database")

    cache = WheelMetadataCache(tmp_path / "cache")

    assert cache.entries == {}
    assert cache.get(("/wheel.whl", 1, 2)) is None

    cache.put(("/wheel.whl", 1, 2), {"Name": ["demo"]})
    cache.flush()

    reopened = WheelMetadataCache(tmp_path / "cache")
    assert reopened.get(("/wheel.whl", 1, 2)) == {"Name": ["demo"]}


def test_metadata_cache_identity_changes_when_artifact_changes(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"one")
    first = metadata_identity(artifact)
    assert first is not None

    artifact.write_bytes(b"two-two")
    second = metadata_identity(artifact)
    assert second is not None
    assert first != second


def test_metadata_cache_round_trips_the_file_digest(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"wheel")
    identity = metadata_identity(artifact)
    assert identity is not None
    digest = "ab" * 32

    cache = WheelMetadataCache(tmp_path / "cache")
    assert cache.get_digest(identity) is None
    cache.put_digest(identity, digest)
    cache.flush()

    restored = WheelMetadataCache(tmp_path / "cache")
    assert restored.get_digest(identity) == digest
    assert restored.get(identity) is None


def test_metadata_cache_prefetches_digests_in_one_query(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    identities = []
    for index in range(3):
        artifact = tmp_path / f"demo-{index}.whl"
        artifact.write_bytes(b"wheel" * (index + 1))
        identity = metadata_identity(artifact)
        assert identity is not None
        identities.append(identity)
    cache = WheelMetadataCache(cache_dir)
    for index, identity in enumerate(identities):
        cache.put_digest(identity, f"{index:02x}" * 32)
    cache.flush()

    restored = WheelMetadataCache(cache_dir)
    stale = (identities[0][0], identities[0][1], identities[0][2] + 1)
    restored.prefetch_digests([*identities, stale])

    assert restored.digests == {
        identity: f"{index:02x}" * 32 for index, identity in enumerate(identities)
    }
    assert restored.get_digest(stale) is None


def test_one_cache_instance_per_directory_across_threads(tmp_path: Path) -> None:
    """Threads preparing a batch must share one instance, or the entries
    put through a losing instance are never flushed."""
    import threading

    from cpip.index import metadata_cache

    metadata_cache._CACHE_INSTANCES.clear()
    cache_dir = str(tmp_path / "cache")
    seen: list[WheelMetadataCache] = []
    barrier = threading.Barrier(8)

    def acquire() -> None:
        barrier.wait()
        seen.append(metadata_cache.get_wheel_metadata_cache(cache_dir))

    threads = [threading.Thread(target=acquire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(cache) for cache in seen}) == 1
    assert seen[0] is metadata_cache.get_wheel_metadata_cache(cache_dir)


def test_metadata_cache_rejects_malformed_persisted_digests(tmp_path: Path) -> None:
    """A stored value that is 64 characters but not hexadecimal is a miss in
    both read paths, so the archive cache is never keyed on a non-hash."""
    import sqlite3

    cache_dir = tmp_path / "cache"
    good = tmp_path / "good.whl"
    bad = tmp_path / "bad.whl"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    good_identity = metadata_identity(good)
    bad_identity = metadata_identity(bad)
    assert good_identity is not None
    assert bad_identity is not None

    cache = WheelMetadataCache(cache_dir)
    cache.put_digest(good_identity, "cd" * 32)
    cache.flush()

    # Plant a 64-character non-hex value directly, as on-disk corruption would.
    with sqlite3.connect(cache_dir / NAME) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO digests (path, size, mtime, sha256) "
            "VALUES (?, ?, ?, ?)",
            (*bad_identity, "z" * 64),
        )
        connection.commit()

    # get_digest: the corrupt row is a miss; the valid one is returned.
    reader = WheelMetadataCache(cache_dir)
    assert reader.get_digest(bad_identity) is None
    assert reader.get_digest(good_identity) == "cd" * 32

    # prefetch_digests: same, and the corrupt value is not memoized.
    prefetcher = WheelMetadataCache(cache_dir)
    prefetcher.prefetch_digests([good_identity, bad_identity])
    assert prefetcher.digests == {good_identity: "cd" * 32}
    assert prefetcher.get_digest(bad_identity) is None
