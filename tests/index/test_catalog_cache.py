from __future__ import annotations

from pathlib import Path

from cpip.index.catalog_cache import load_links, save_links
from cpip.index.links import Link
from cpip.index.source_models import MetadataFile
from cpip.network.cache import SafeFileCache


def test_catalog_cache_roundtrip(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    original = Link.from_url(
        "https://files.example.test/demo-1.2.3-py3-none-any.whl#sha256=abc",
        source_url="https://example.test/simple/demo/",
        text="demo-1.2.3-py3-none-any.whl",
        requires_python=">=3.9",
        yanked_reason="broken release",
        metadata_file=MetadataFile({"sha256": "def"}),
    )

    save_links(cache, "https://example.test/simple/demo/", [original])
    loaded = load_links(cache, "https://example.test/simple/demo/")

    assert loaded is not None
    assert loaded[0].url == original.url
    assert loaded[0].comes_from == original.comes_from
    assert loaded[0].hashes == original.hashes
    assert loaded[0].requires_python == original.requires_python
    assert loaded[0].yanked_reason == original.yanked_reason
    assert loaded[0].metadata_file == original.metadata_file


def test_catalog_cache_ignores_corrupt_entries(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    key = "cpip-index-catalog-v2:https://example.test/simple/demo/"
    cache.set(key, b"not marshal")
    cache.set_body(key, b"1")

    assert load_links(cache, "https://example.test/simple/demo/") is None
