from __future__ import annotations

from pathlib import Path

from cpip.core.packaging import Version, parse_requirement
from cpip.index.candidate_metadata_cache import get_candidate_metadata_cache
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

    cache.put(key, metadata)
    cache.flush()
    loaded = get_candidate_metadata_cache(tmp_path).get(key)

    assert loaded is not None
    assert loaded.name == "demo"
    assert loaded.version == Version("1.2.3")
    assert loaded.dependencies[0].raw == "requests>=2"
    assert loaded.provided_extras == frozenset(("docs",))
    assert loaded.requires_python == ">=3.9"
