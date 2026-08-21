from __future__ import annotations

import hashlib
import marshal
from pathlib import Path

from cpip.core.versions import VERSION_WIRE_FORMAT, Version, is_version_wire
from cpip.index.catalog_cache import (
    CHOICE_HEADER,
    SUMMARY_HEADER,
    VERSION as CATALOG_VERSION,
    WHEEL_RECORD,
    CatalogChoices,
    cache_key,
    choice_key,
    encode_checked_payload,
    decode_summary,
    load_catalog,
    load_choices,
    load_links,
    load_summary,
    save_choices,
    save_links,
    save_summary_value,
    summary_key,
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
        (name, version, str(Version.from_wire(version_state)), facts)
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


def test_malformed_choices_are_a_miss(tmp_path: Path) -> None:
    """A choice of the wrong arity never reaches the provider's unpack, and a
    summary carrying one is recompiled rather than re-served on every run."""
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
    generation = summary[0]
    malformed = {"1.0": (record, WHEEL_RECORD)}

    cache.set_atomic(
        choice_key(page_url, "target", True, True),
        encode_checked_payload(CHOICE_HEADER, (generation, malformed)),
    )
    assert load_choices(cache, page_url, generation, "target", True, True) == {}

    save_summary_value(
        cache,
        page_url,
        (generation, summary[1], summary[2], {("target", True, True): malformed}),  # ty:ignore[invalid-argument-type]
    )
    recompiled = load_summary(cache, page_url)
    assert recompiled is not None
    assert recompiled[3] == {}


def test_summary_payload_carries_the_version_wire_format(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])
    raw = cache.get_atomic(summary_key(page_url))
    assert raw is not None
    assert raw.startswith(SUMMARY_HEADER)
    payload = marshal.loads(raw[len(SUMMARY_HEADER) + hashlib.sha256().digest_size :])
    assert payload[4] == VERSION_WIRE_FORMAT
    assert is_version_wire(payload[1][0][2])

    # A payload with the wrong wire format is a miss, not a wrong answer.
    body = marshal.dumps((*payload[:4], VERSION_WIRE_FORMAT + 1))
    cache.set_atomic(
        summary_key(page_url),
        SUMMARY_HEADER + hashlib.sha256(body).digest() + body,
    )
    assert decode_summary(cache.get_atomic(summary_key(page_url))) is None
    recompiled = load_summary(cache, page_url)
    assert recompiled is not None
    assert recompiled[1][0][1] == "1.0"


def test_catalog_cache_ignores_corrupt_entries(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    key = cache_key("https://example.test/simple/demo/")
    cache.set(key, b"not marshal")
    cache.set_body(key, b"1")

    assert load_links(cache, "https://example.test/simple/demo/") is None


def test_catalog_with_an_unparseable_version_is_a_miss(tmp_path: Path) -> None:
    """A semantically corrupt catalog must not raise out of load_summary."""
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    record = (None,) * 8
    groups = [
        ("demo", "1.0", [(WHEEL_RECORD, record)], []),
        ("demo", "not a version", [(WHEEL_RECORD, record)], []),
    ]
    cache.set_atomic(
        cache_key(page_url),
        marshal.dumps(("cpip-index-catalog", CATALOG_VERSION, groups, [])),
    )

    assert load_catalog(cache, page_url) is None
    assert load_summary(cache, page_url) is None
