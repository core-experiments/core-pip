from pathlib import Path

from cpip.index.release_facts_cache import NAME, get_release_facts_cache


def test_release_facts_cache_roundtrip(tmp_path: Path) -> None:
    key = ("demo", "1.0", "sha256:abc")
    cache = get_release_facts_cache(tmp_path)
    cache.put(key, "invalid wheel metadata")
    cache.flush()

    loaded = get_release_facts_cache(tmp_path)
    assert loaded.get(key) == "invalid wheel metadata"


def test_release_facts_cache_ignores_old_schema(tmp_path: Path) -> None:
    Path(tmp_path / NAME).write_bytes(b"not a valid snapshot")
    cache = get_release_facts_cache(tmp_path)

    assert cache.get(("demo", "1.0", "sha256:abc")) is None
