from __future__ import annotations

from pathlib import Path

from cpip.index.metadata_cache import WheelMetadataCache, metadata_identity


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


def test_metadata_cache_ignores_corrupt_snapshots(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "metadata-v2.marshal"
    cache_path.parent.mkdir()
    cache_path.write_bytes(b"not a marshal snapshot")

    cache = WheelMetadataCache(tmp_path / "cache")

    assert cache.entries == {}


def test_metadata_cache_identity_changes_when_artifact_changes(tmp_path: Path) -> None:
    artifact = tmp_path / "demo.whl"
    artifact.write_bytes(b"one")
    first = metadata_identity(artifact)
    assert first is not None

    artifact.write_bytes(b"two-two")
    second = metadata_identity(artifact)
    assert second is not None
    assert first != second
