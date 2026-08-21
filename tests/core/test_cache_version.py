"""Every persisted cache derives its storage name and format stamp from the
single cache-wide ``CACHE_VERSION``; no module may carry its own number."""

from __future__ import annotations

from cpip.cli import fast, fast_install
from cpip.core.utils import CACHE_INTERPRETER_TAG, CACHE_VERSION, CACHE_VERSION_TAG
from cpip.core.versions import VERSION_WIRE_FORMAT
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


def test_the_cache_system_is_version_zero() -> None:
    assert CACHE_VERSION == 0
    assert CACHE_VERSION_TAG == "v0"


def test_every_storage_name_carries_the_cache_version() -> None:
    tag = CACHE_VERSION_TAG
    assert HTTP_CACHE_BUCKET == f"http-{tag}"
    assert artifact_cache.ARTIFACT_CACHE_BUCKET == f"artifacts-{tag}"
    assert wheel_cache.WHEEL_CACHE_BUCKET == f"wheels-{tag}"
    assert fast.FAST_LOCK_PLAN_BUCKET == f"fast-lock-plan-{tag}"
    assert metadata_cache.NAME == f"metadata-{tag}.sqlite"
    assert candidate_metadata_cache.NAME == f"candidate-metadata-{tag}.sqlite"
    assert release_facts_cache.NAME == f"release-facts-{tag}.marshal"
    assert wheel_archive_cache.ARCHIVE_CACHE_BUCKET_FAMILY == f"archive-{tag}"
    assert (
        wheel_archive_cache.ARCHIVE_CACHE_BUCKET
        == f"archive-{tag}-{CACHE_INTERPRETER_TAG}"
    )
    assert wheel_install_plan_cache.RESOLUTION_CACHE_BUCKET_FAMILY == (
        f"resolution-{tag}"
    )
    assert wheel_install_plan_cache.REMOTE_EXACT_CONTEXT == f"remote-exact-{tag}"
    assert fast_install.NAME_FAMILY == f"fast-install-{tag}"
    assert fast_install.NAME == f"fast-install-{tag}-{CACHE_INTERPRETER_TAG}.marshal"
    assert fast_install.TREE_CACHE_BUCKET == f"fast-install-trees-{tag}"
    assert catalog_cache.PREFIX == f"cpip-index-catalog-{tag}:"
    assert catalog_cache.SUMMARY_PREFIX == f"cpip-index-summary-{tag}:"
    assert catalog_cache.CHOICE_PREFIX == f"cpip-index-choice-{tag}:"
    assert catalog_cache.SUMMARY_HEADER == f"cpip-index-summary-{tag}\0".encode()
    assert catalog_cache.CHOICE_HEADER == f"cpip-index-choice-{tag}\0".encode()


def test_built_wheels_live_under_the_versioned_bucket() -> None:
    entry = wheel_cache.wheel_cache_path("/root", "https://example.test/demo.tar.gz")
    assert entry.startswith(f"/root/{wheel_cache.WHEEL_CACHE_BUCKET}/")


def test_every_format_stamp_is_the_cache_version() -> None:
    assert VERSION_WIRE_FORMAT == CACHE_VERSION
    assert artifact_cache.ARTIFACT_CACHE_FORMAT == CACHE_VERSION
    assert wheel_archive_cache.ARCHIVE_CACHE_FORMAT == CACHE_VERSION
    assert wheel_install_plan_cache.RESOLUTION_CACHE_FORMAT == CACHE_VERSION
    assert release_facts_cache.VERSION == CACHE_VERSION
    assert fast_install.VERSION == CACHE_VERSION
    assert fast_install.TREE_CACHE_FORMAT == CACHE_VERSION
    assert catalog_cache.VERSION == CACHE_VERSION


def test_no_legacy_readers_remain() -> None:
    for module, names in (
        (
            catalog_cache,
            (
                "migrate_v6_catalog",
                "migrate_legacy_catalog",
                "V6_PREFIX",
                "LEGACY_PREFIX",
            ),
        ),
        (candidate_metadata_cache, ("V3_NAME", "LEGACY_NAME")),
    ):
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name}"
