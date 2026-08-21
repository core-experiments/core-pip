"""Characterization tests for CandidateProvider.lazy_catalog_records.

These pin the current behavior of a dense, closure-heavy method before it is
refactored. ``lazy_catalog_records`` only reads *already-persisted* choices
(it never computes them), so every test warms the choice cache first via
``candidate_records_from_catalog(primary_only=True)``, mirroring the
production two-phase usage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cpip.core.packaging import parse_requirement
from cpip.core.versions import Version
from cpip.index.catalog_cache import (
    cache_key,
    catalog_generation,
    save_links,
)
from cpip.index.links import Link
from cpip.index.provider import CandidateProvider
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


def _warm_and_seed(
    provider: CandidateProvider,
    catalog_key: tuple[str, bool, bool],
    records_by_version: dict[Version, tuple[tuple[str, str], ...]],
    versions: tuple[Version, ...],
) -> None:
    """Populate the persisted choice cache and the package-catalog cache the

    way production does before ``lazy_catalog_records`` can read from them.
    """
    catalog = SimpleNamespace(records_by_version=records_by_version)
    provider.candidate_records_from_catalog(
        catalog_key,
        catalog,
        versions,
        primary_only=True,
    )
    provider.package_catalog_cache[catalog_key] = catalog


def test_lazy_catalog_records_yields_pinned_wheel(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=aaa",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    _warm_and_seed(provider, ("demo", True, True), records_by_version, (version,))

    requirement = parse_requirement("demo==1.0.0")
    records = provider.lazy_catalog_records(requirement)
    assert records is not None
    materialized = list(records)

    assert len(materialized) == 1
    assert materialized[0].version == version
    assert materialized[0].wheel is not None


def test_lazy_catalog_records_returns_none_for_url_requirement(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    provider = _provider(cache)
    requirement = parse_requirement(
        "demo @ https://files.example.test/demo-1.0.0-py3-none-any.whl",
    )

    assert provider.lazy_catalog_records(requirement) is None


def test_lazy_catalog_records_returns_none_when_find_links_configured(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    provider = CandidateProvider.from_options(
        no_index=True,
        find_links=[str(tmp_path)],
        session=SimpleNamespace(cache=cache),
    )
    requirement = parse_requirement("demo==1.0.0")

    assert provider.lazy_catalog_records(requirement) is None


def test_lazy_catalog_records_returns_none_when_choice_not_persisted(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=bbb",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    # Seed the package-catalog cache directly without warming the choice
    # cache first, so ``lazy_catalog_records`` sees a cache miss.
    provider.package_catalog_cache[("demo", True, True)] = SimpleNamespace(
        records_by_version=records_by_version,
    )

    requirement = parse_requirement("demo==1.0.0")
    assert provider.lazy_catalog_records(requirement) is None


def test_lazy_catalog_records_choice_prefers_wheel_over_sdist(
    tmp_path: Path,
) -> None:
    """The persisted per-version choice already picked a single winner

    (``_select_catalog_choice`` ranks WHEEL_RECORD above SDIST_RECORD), so when both
    artifact kinds exist for the same version, only the wheel is yielded.
    """
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    wheel_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=ccc",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    sdist_link = Link.from_url(
        "https://files.example.test/demo-1.0.0.tar.gz#sha256=ddd",
        source_url=source_url,
        text="demo-1.0.0.tar.gz",
    )
    save_links(cache, source_url, [wheel_link, sdist_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    _warm_and_seed(provider, ("demo", True, True), records_by_version, (version,))

    requirement = parse_requirement("demo==1.0.0")
    records = provider.lazy_catalog_records(requirement)
    assert records is not None
    materialized = list(records)

    assert len(materialized) == 1
    assert materialized[0].wheel is not None
