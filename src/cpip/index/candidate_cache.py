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
    if local is not None and os.path.isfile(local):
        try:
            with open(local, "rb") as file:
                return {"sha256": hashlib.sha256(file.read()).hexdigest()}
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
    entry_dir_text = os.fspath(entry_dir)
    if not os.path.isdir(entry_dir_text):
        return None
    with os.scandir(entry_dir_text) as entries:
        wheels = sorted(
            Path(entry.path)
            for entry in entries
            if entry.name.endswith(".whl") and entry.is_file()
        )
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
    entry_dir_text = os.fspath(entry_dir)
    os.makedirs(entry_dir_text, exist_ok=True)
    shutil.copy2(wheel, os.path.join(entry_dir_text, wheel.name))
    origin = {"archive_info": {"hashes": source_hashes_for_link(candidate.link)}}
    with open(
        os.path.join(entry_dir_text, "origin.json"), "w", encoding="utf-8"
    ) as file:
        json.dump(origin, file)


def emit_build_message(message: str) -> None:
    if not os.environ.get("CPIP_QUIET"):
        print(message)
