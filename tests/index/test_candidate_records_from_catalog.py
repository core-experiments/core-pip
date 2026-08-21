"""Characterization tests for CandidateProvider.candidate_records_from_catalog.

These pin the current behavior of a dense, closure-heavy method before it is
refactored, so the refactor can be verified against real persistent-cache
round trips rather than by inspection alone.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cpip.core.versions import Version
from cpip.index.catalog_cache import (
    cache_key,
    catalog_generation,
    load_choices,
    save_links,
)
from cpip.index.links import Link
from cpip.index.provider import CandidateProvider
from cpip.index.source_models import CandidateRecord, MetadataFile
from cpip.network.cache import SafeFileCache


def _generation(cache: SafeFileCache, source_url: str) -> str:
    raw = cache.get_atomic(cache_key(source_url))
    assert raw is not None
    return catalog_generation(raw)


def _provider(cache: SafeFileCache) -> CandidateProvider:
    return CandidateProvider.from_options(
        no_index=True,
        session=SimpleNamespace(cache=cache),
    )


def test_single_wheel_record_is_materialized(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=aaa",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)

    provider = _provider(cache)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = provider.candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert len(result) == 1
    candidate = result[0]
    assert candidate.name == "demo"
    assert candidate.version == version
    assert candidate.wheel is not None
    assert candidate.link.url.startswith("https://files.example.test/demo-1.0.0")


def test_sdist_only_record_is_materialized_when_source_allowed(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0.tar.gz#sha256=bbb",
        source_url=source_url,
        text="demo-1.0.0.tar.gz",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)

    provider = _provider(cache)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    allowed = provider.candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )
    assert len(allowed) == 1
    assert allowed[0].wheel is None

    provider_no_source = _provider(cache)
    disallowed = provider_no_source.candidate_records_from_catalog(
        ("demo", True, False),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )
    assert disallowed == ()


def test_wheel_only_allowed_when_binary_requested(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=ccc",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    disallowed = _provider(cache).candidate_records_from_catalog(
        ("demo", False, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )
    assert disallowed == ()


def test_wheel_preferred_over_sdist_for_same_version(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    wheel_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=ddd",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    sdist_link = Link.from_url(
        "https://files.example.test/demo-1.0.0.tar.gz#sha256=eee",
        source_url=source_url,
        text="demo-1.0.0.tar.gz",
    )
    save_links(cache, source_url, [wheel_link, sdist_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert len(result) == 2
    wheels = [candidate for candidate in result if candidate.wheel is not None]
    assert len(wheels) == 1


def test_unsupported_wheel_falls_back_when_no_supported_artifact(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    incompatible_link = Link.from_url(
        (
            "https://files.example.test/"
            "demo-1.0.0-cp1-cp1-madeup_platform_x86_64.whl#sha256=fff"
        ),
        source_url=source_url,
        text="demo-1.0.0-cp1-cp1-madeup_platform_x86_64.whl",
    )
    save_links(cache, source_url, [incompatible_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert len(result) == 1
    assert result[0].tag_rank is None
    assert result[0].wheel is not None


def test_requires_python_mismatch_excludes_record(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=111",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
        requires_python="<2.0",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert result == ()


def test_yanked_and_unyanked_records_both_materialize_in_ordinary_path(
    tmp_path: Path,
) -> None:
    """The ordinary (non primary_only) path does not rank by yanked status;

    every tag-compatible, requires_python-eligible record is materialized and
    left for the resolver's own candidate evaluation to rank.
    """
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    yanked_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl?v=1#sha256=222",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
        yanked_reason="broken",
    )
    good_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl?v=2#sha256=333",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [yanked_link, good_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )
    urls = {candidate.link.url for candidate in result}
    assert any("?v=1" in url for url in urls)
    assert any("?v=2" in url for url in urls)


def test_multiple_sources_aggregate_records_for_one_version(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_a = "https://a.example.test/simple/demo/"
    source_b = "https://b.example.test/simple/demo/"
    link_a = Link.from_url(
        "https://files.example.test/a/demo-1.0.0-py3-none-any.whl#sha256=444",
        source_url=source_a,
        text="demo-1.0.0-py3-none-any.whl",
    )
    link_b = Link.from_url(
        "https://files.example.test/b/demo-1.0.0.tar.gz#sha256=555",
        source_url=source_b,
        text="demo-1.0.0.tar.gz",
    )
    save_links(cache, source_a, [link_a])
    save_links(cache, source_b, [link_b])
    generation_a = _generation(cache, source_a)
    generation_b = _generation(cache, source_b)
    version = Version("1.0.0")
    records_by_version = {
        version: ((source_a, generation_a), (source_b, generation_b)),
    }

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert len(result) == 2


def test_primary_only_prefers_unyanked_choice(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    yanked_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl?v=1#sha256=aaa1",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
        yanked_reason="broken",
    )
    good_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl?v=2#sha256=aaa2",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [yanked_link, good_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
        primary_only=True,
    )

    assert len(result) == 1
    assert "?v=2" in result[0].link.url


def test_primary_only_persists_choice_for_reuse(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=666",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    supported_tags, target_key = provider.catalog_target_internal()

    result = provider.candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
        primary_only=True,
    )

    assert len(result) == 1

    persisted = load_choices(
        cache,
        source_url,
        generation,
        target_key,
        True,
        True,
    )
    assert persisted is not None
    assert persisted.get("1.0.0") is not None


def test_records_by_version_none_falls_back_to_candidates_by_version() -> None:
    provider = CandidateProvider.from_options(no_index=True)
    version = Version("1.0.0")
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=777",
        source_url="https://example.test/simple/demo/",
        text="demo-1.0.0-py3-none-any.whl",
    )
    fallback_candidate = CandidateRecord(name="demo", version=version, link=link)
    catalog = SimpleNamespace(
        records_by_version=None,
        candidates_by_version={version: (fallback_candidate,)},
    )

    result = provider.candidate_records_from_catalog(
        ("demo", True, True),
        catalog,
        (version,),
    )

    assert result == (fallback_candidate,)


def test_link_with_metadata_file_still_materializes(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=888",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
        metadata_file=MetadataFile({"sha256": "999"}),
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    result = _provider(cache).candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(records_by_version=records_by_version),
        (version,),
    )

    assert len(result) == 1
