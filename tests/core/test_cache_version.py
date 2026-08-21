"""The cache system is versioned once, by the ``v<N>`` directory every cache
lives under; no storage name or payload carries a version of its own."""

from __future__ import annotations

import os

import pytest
from cpip.cli import fast, fast_install
from cpip.core.appdirs import (
    cache_root,
    configured_cache_dir,
    resolve_cache_dir,
    versioned_cache_dir,
)
from cpip.core.utils import CACHE_VERSION, CACHE_VERSION_TAG
from cpip.index import (
    artifact_cache,
    cache as wheel_cache,
    candidate_metadata_cache,
    catalog_cache,
    metadata_cache,
    release_facts_cache,
)
from cpip.install import wheel_archive_cache, wheel_install_plan_cache
from cpip.network.cache import HTTP_CACHE_BUCKET

STORAGE_NAMES = (
    HTTP_CACHE_BUCKET,
    artifact_cache.ARTIFACT_CACHE_BUCKET,
    wheel_cache.WHEEL_CACHE_BUCKET,
    metadata_cache.NAME,
    candidate_metadata_cache.NAME,
    release_facts_cache.NAME,
    fast.FAST_LOCK_PLAN_BUCKET,
    fast_install.NAME_FAMILY,
    fast_install.TREE_CACHE_BUCKET,
    wheel_archive_cache.ARCHIVE_CACHE_BUCKET_FAMILY,
    wheel_install_plan_cache.RESOLUTION_CACHE_BUCKET_FAMILY,
    wheel_install_plan_cache.REMOTE_EXACT_CONTEXT,
    catalog_cache.PREFIX,
    catalog_cache.SUMMARY_PREFIX,
    catalog_cache.CHOICE_PREFIX,
    catalog_cache.SUMMARY_HEADER.decode(),
    catalog_cache.CHOICE_HEADER.decode(),
)


def test_the_cache_system_is_version_zero() -> None:
    assert CACHE_VERSION == 0
    assert CACHE_VERSION_TAG == "v0"


def test_every_writer_lands_under_the_versioned_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert versioned_cache_dir("/root") == os.path.join("/root", "v0")
    assert resolve_cache_dir("/explicit") == os.path.join("/explicit", "v0")
    monkeypatch.setenv("CPIP_CACHE_DIR", "/configured")
    assert cache_root() == "/configured"
    assert resolve_cache_dir() == os.path.join("/configured", "v0")
    assert configured_cache_dir() == os.path.join("/configured", "v0")
    monkeypatch.delenv("CPIP_CACHE_DIR")
    assert configured_cache_dir() is None
    assert resolve_cache_dir() == versioned_cache_dir(cache_root())


@pytest.mark.parametrize("name", STORAGE_NAMES)
def test_no_storage_name_carries_its_own_version(name: str) -> None:
    assert "-v" not in name
    assert not any(character.isdigit() for character in name)


def test_no_legacy_readers_remain() -> None:
    for module, names in (
        (catalog_cache, ("migrate_v6_catalog", "migrate_legacy_catalog")),
        (candidate_metadata_cache, ("migrate_payload", "load_other_legacy")),
    ):
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name}"
