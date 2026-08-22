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
