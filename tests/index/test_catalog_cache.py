from __future__ import annotations

import hashlib
import marshal
import urllib.parse
from pathlib import Path

from cpip.core.packaging import Version
from cpip.index.catalog_cache import (
    LEGACY_PREFIX,
    LEGACY_SUMMARY_HEADER,
    LEGACY_SUMMARY_PREFIX,
    RECORD_WHEEL_IDENTITY,
    V6_PREFIX,
    WHEEL_RECORD,
    CatalogChoices,
    cache_key,
    load_catalog,
    load_choices,
    load_links,
    load_summary,
    save_choices,
    save_links,
    summary_key,
    wheel_file_from_record,
)
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
    catalog = load_catalog(cache, "https://example.test/simple/demo/")
    summary = load_summary(cache, "https://example.test/simple/demo/")

    assert loaded is not None
    assert catalog is not None
    assert summary is not None
    assert [
        (name, version, [kind for kind, _record in artifacts], facts)
        for name, version, artifacts, facts in catalog[0]
    ] == [
        (
            "demo",
            "1.2.3",
            [WHEEL_RECORD],
            [(WHEEL_RECORD, ">=3.9", "broken release")],
        ),
    ]
    assert catalog[1] == []
    assert [
        (name, version, str(Version.from_cache_state(version_state)), facts)
        for name, version, version_state, facts in summary[1]
    ] == [
        (
            "demo",
            "1.2.3",
            "1.2.3",
            [(WHEEL_RECORD, ">=3.9", "broken release")],
        ),
    ]
    assert loaded[0].url == original.url
    assert loaded[0].comes_from == original.comes_from
    assert loaded[0].hashes == original.hashes
    assert loaded[0].requires_python == original.requires_python
    assert loaded[0].yanked_reason == original.yanked_reason
    assert loaded[0].metadata_file == original.metadata_file


def test_catalog_summary_hands_off_decoded_catalog(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])
    cache.delete(summary_key(page_url))

    assert load_summary(cache, page_url) is not None
    pending = getattr(cache, "_cpip_pending_catalogs")
    assert page_url in pending
    assert load_catalog(cache, page_url) is not None
    assert page_url not in pending


def test_catalog_choices_are_scoped_to_generation(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])
    catalog = load_catalog(cache, page_url)
    summary = load_summary(cache, page_url)

    assert catalog is not None
    assert summary is not None
    record = catalog[0][0][2][0][1]
    choices: CatalogChoices = {"1.0": (record, WHEEL_RECORD, 0)}
    save_choices(cache, page_url, summary[0], "target", True, True, choices)

    embedded = load_summary(cache, page_url)
    assert embedded is not None
    assert embedded[3][("target", True, True)] == choices
    assert (
        load_choices(
            cache,
            page_url,
            summary[0],
            "target",
            True,
            True,
        )
        == choices
    )
    assert (
        load_choices(
            cache,
            page_url,
            "different-generation",
            "target",
            True,
            True,
        )
        == {}
    )


def test_catalog_summary_migrates_v2_in_version_order(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    groups = [
        (
            "demo",
            version,
            Version(version).cache_state_internal(),
            [(WHEEL_RECORD, None, None)],
        )
        for version in ("2.0", "1.0")
    ]
    body = marshal.dumps(("generation", groups, False))
    cache.set_atomic(
        LEGACY_SUMMARY_PREFIX + page_url,
        LEGACY_SUMMARY_HEADER + hashlib.sha256(body).digest() + body,
    )

    summary = load_summary(cache, page_url)

    assert summary is not None
    assert [group[1] for group in summary[1]] == ["1.0", "2.0"]
    assert cache.get_atomic(summary_key(page_url)) is not None


def test_catalog_cache_ignores_corrupt_entries(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    key = cache_key("https://example.test/simple/demo/")
    cache.set(key, b"not marshal")
    cache.set_body(key, b"1")

    assert load_links(cache, "https://example.test/simple/demo/") is None


def test_catalog_cache_migrates_v2_records_locally(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    artifact_url = "https://files.example.test/demo-1.2.3-py3-none-any.whl"
    parsed_url = urllib.parse.urlsplit(artifact_url)
    legacy_record = (
        artifact_url,
        tuple(parsed_url),
        page_url,
        "demo-1.2.3-py3-none-any.whl",
        {"sha256": "abc"},
        ">=3.9",
        "",
        {"sha256": "def"},
        None,
    )
    legacy_key = LEGACY_PREFIX + page_url
    cache.set(
        legacy_key,
        marshal.dumps(("cpip-index-catalog", 2, [legacy_record])),
    )
    cache.set_body(legacy_key, b"1")

    catalog = load_catalog(cache, page_url)

    assert catalog is not None
    assert [
        (name, version, facts) for name, version, _artifacts, facts in catalog[0]
    ] == [("demo", "1.2.3", [(WHEEL_RECORD, ">=3.9", "")])]
    assert cache.get_atomic(cache_key(page_url)) is not None
    loaded = load_links(cache, page_url)
    assert loaded is not None
    assert loaded[0].is_yanked


def test_catalog_cache_migrates_v6_wheels_with_identity(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    artifact_url = "https://files.example.test/demo-1.2.3-py3-none-any.whl"
    v6_record = (
        artifact_url,
        "demo-1.2.3-py3-none-any.whl",
        {"sha256": "abc"},
        ">=3.9",
        None,
        {"sha256": "def"},
        None,
    )
    v6_key = V6_PREFIX + page_url
    cache.set(
        v6_key,
        marshal.dumps(
            (
                "cpip-index-catalog",
                6,
                [
                    (
                        "demo",
                        "1.2.3",
                        [(WHEEL_RECORD, v6_record)],
                        [(WHEEL_RECORD, ">=3.9", None)],
                    )
                ],
                [],
            ),
        ),
    )
    cache.set_body(v6_key, b"1")

    catalog = load_catalog(cache, page_url)

    assert catalog is not None
    name, version, artifacts, facts = catalog[0][0]
    assert (name, version) == ("demo", "1.2.3")
    assert [kind for kind, _record in artifacts] == [WHEEL_RECORD]
    assert facts == [(WHEEL_RECORD, ">=3.9", None)]
    record = artifacts[0][1]
    assert len(record) == 8
    assert record[RECORD_WHEEL_IDENTITY] == (
        "demo",
        "1.2.3",
        None,
        (("py3", "none", "any"),),
    )
    assert cache.get_atomic(cache_key(page_url)) is not None
    wheel = wheel_file_from_record(
        record,
        name="demo",
        version=Version("1.2.3"),
    )
    assert wheel is not None
    assert wheel.name == "demo"
    assert str(wheel.version) == "1.2.3"
    assert [(tag.interpreter, tag.abi, tag.platform) for tag in wheel.tags] == [
        ("py3", "none", "any")
    ]
