"""Artifact hash and built-wheel cache operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord

logger = logging.getLogger(__name__)


def source_hashes_for_link(link: Link) -> dict[str, str]:
    from cpip.index.artifacts import ArtifactLocator

    hashes = dict(link.hashes)
    if hashes:
        return hashes
    local = ArtifactLocator().local_path(link.url)
    if local is not None and local.is_file():
        try:
            return {"sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
        except OSError:
            return {}
    return {}


def cache_identity(url: str) -> str:
    """Return the stable cache key for an artifact URL."""
    from cpip.index.vcs import is_immutable_vcs_link, vcs_reference

    if is_immutable_vcs_link(url):
        reference = vcs_reference(url)
        return f"{reference.vcs}+{reference.repo_url}@{reference.requested_revision}"
    return url


def cached_wheel_for_link(
    wheel_cache_dir: Path | None, url: str
) -> tuple[Path, dict[str, str] | None] | None:
    from cpip.index.cache import origin_hashes, wheel_cache_path

    if wheel_cache_dir is None:
        return None
    entry_dir = wheel_cache_path(wheel_cache_dir, cache_identity(url))
    if not entry_dir.is_dir():
        return None
    wheels = sorted(entry_dir.glob("*.whl"))
    if not wheels:
        return None
    return wheels[0], origin_hashes(entry_dir / "origin.json")


def cache_built_wheel(
    wheel_cache_dir: Path | None, candidate: CandidateRecord, wheel: Path
) -> None:
    from cpip.index.cache import wheel_cache_path

    if wheel_cache_dir is None:
        return
    entry_dir = wheel_cache_path(wheel_cache_dir, cache_identity(candidate.link.url))
    entry_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel, entry_dir / wheel.name)
    origin = {"archive_info": {"hashes": source_hashes_for_link(candidate.link)}}
    (entry_dir / "origin.json").write_text(json.dumps(origin), encoding="utf-8")


def emit_build_message(message: str) -> None:
    if not os.environ.get("CPIP_QUIET"):
        print(message)
