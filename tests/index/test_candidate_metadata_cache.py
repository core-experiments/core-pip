from __future__ import annotations

from pathlib import Path

from cpip.core.packaging import parse_requirement
from cpip.core.versions import Version
from cpip.core.utils import save_snapshot
from cpip.index.candidate_metadata_cache import (
    LEGACY_NAME,
    LEGACY_VERSION,
    NAME,
    V3_NAME,
    VERSION,
    CandidateMetadataCache,
    get_candidate_metadata_cache,
)
from cpip.index.source_models import CandidateMetadata


def test_candidate_metadata_cache_roundtrip(tmp_path: Path) -> None:
    cache = get_candidate_metadata_cache(tmp_path)
    key = (
        "https://files.example.test/demo.whl",
        "1.2.3",
        ("docs",),
        "sha256:abc",
    )
    metadata = CandidateMetadata(
        name="demo",
        version=Version("1.2.3"),
        dependencies=(parse_requirement("requests>=2"),),
        provided_extras=frozenset(("docs",)),
        requires_python=">=3.9",
    )

    assert not cache.contains(key)
    cache.put(key, metadata)
    assert cache.contains(key)
    cache.flush()
    loaded = CandidateMetadataCache(tmp_path).get(key)

    assert loaded is not None
    assert loaded.name == "demo"
    assert loaded.version == Version("1.2.3")
    assert loaded.dependencies[0].raw == "requests>=2"
    assert loaded.provided_extras == frozenset(("docs",))
    assert loaded.requires_python == ">=3.9"


def test_candidate_metadata_cache_defers_database_creation(tmp_path: Path) -> None:
    """A resolve that only misses must not pay to create the database."""
    key = ("https://example.test/cold.whl", "1", (), "sha256:cold")

    cache = CandidateMetadataCache(tmp_path)
    assert not cache.contains(key)
    assert cache.get(key) is None
    assert not (tmp_path / NAME).exists()

    cache.put(
        key,
        CandidateMetadata(
            name="cold",
            version=Version("1"),
            dependencies=(),
            provided_extras=frozenset(),
            requires_python=None,
        ),
    )
    cache.flush()
    assert (tmp_path / NAME).is_file()


def test_candidate_metadata_cache_validates_entries_lazily(tmp_path: Path) -> None:
    valid_key = ("https://example.test/valid.whl", "1", (), "sha256:valid")
    invalid_key = ("https://example.test/invalid.whl", "1", (), "sha256:bad")
    entries = {
        valid_key: ("valid", "1", (), (), None),
        invalid_key: ("invalid", "1", (42,), (), None),
    }
    assert save_snapshot(
        tmp_path / NAME,
        ("cpip-candidate-metadata", VERSION, entries, {}, {}),
    )

    cache = CandidateMetadataCache(tmp_path)

    assert cache.contains(valid_key)
    assert not cache.contains(invalid_key)
    assert invalid_key not in cache.entries


def test_candidate_metadata_cache_migrates_v2_snapshot(tmp_path: Path) -> None:
    key = ("https://example.test/demo.whl", "2.0", (), "sha256:legacy")
    entries = {key: ("demo", "2.0", ("requests>=2",), (), ">=3.9")}
    assert save_snapshot(
        tmp_path / LEGACY_NAME,
        ("cpip-candidate-metadata", LEGACY_VERSION, entries),
    )

    cache = CandidateMetadataCache(tmp_path)
    metadata = cache.get(key)
    cache.flush()

    assert metadata is not None
    assert metadata.dependencies[0].raw == "requests>=2"
    assert (tmp_path / NAME).is_file()
    reloaded = CandidateMetadataCache(tmp_path).get(key)
    assert reloaded is not None
    assert reloaded.name == metadata.name
    assert reloaded.version == metadata.version
    assert reloaded.dependencies == metadata.dependencies


def test_candidate_metadata_cache_imports_v3_database(tmp_path: Path) -> None:
    """The v3 store's metadata rows have the same shape; they are copied so
    an upgrade does not re-fetch every candidate's metadata."""
    import json
    import marshal
    import sqlite3

    key = ("https://example.test/demo.whl", "3.0", ("x",), "sha256:v3")
    value = ("demo", "3.0", ("requests>=2",), ("x",), ">=3.9")
    source = sqlite3.connect(tmp_path / V3_NAME)
    source.execute("CREATE TABLE candidate_metadata (key TEXT PRIMARY KEY, value BLOB)")
    source.execute(
        "CREATE TABLE version_states (raw TEXT PRIMARY KEY, state BLOB)",
    )
    source.execute(
        "INSERT INTO candidate_metadata (key, value) VALUES (?, ?)",
        (json.dumps(key), marshal.dumps(value)),
    )
    source.execute(
        "INSERT INTO candidate_metadata (key, value) VALUES (?, ?)",
        ("not json", marshal.dumps(value)),
    )
    source.commit()
    source.close()

    cache = CandidateMetadataCache(tmp_path)
    metadata = cache.get(key)
    assert metadata is not None
    assert metadata.version == Version("3.0")
    assert metadata.dependencies[0].raw == "requests>=2"
    assert (tmp_path / NAME).is_file()
    assert CandidateMetadataCache(tmp_path).get(key) is not None
