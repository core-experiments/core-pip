"""Artifact hash and built-wheel cache operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil

from cpip.index.artifacts import ArtifactLocator
from cpip.index.cache import origin_hashes, wheel_cache_path
from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord
from cpip.index.vcs import is_immutable_vcs_link, vcs_reference

logger = logging.getLogger(__name__)


def source_hashes_for_link(link: Link) -> dict[str, str]:
    hashes = dict(link.hashes)

    if hashes:
        return hashes

    local = ArtifactLocator().local_path(link.url)

    if local is not None:
        try:
            with open(local, "rb") as file:
                return {"sha256": hashlib.sha256(file.read()).hexdigest()}

        except OSError:
            return {}

    return {}


def cache_identity(url: str) -> str:
    """Return the stable cache key for an artifact URL."""

    if is_immutable_vcs_link(url):
        reference = vcs_reference(url)

        return f"{reference.vcs}+{reference.repo_url}@{reference.requested_revision}"

    return url


def cached_wheel_for_link(
    wheel_cache_dir: str | os.PathLike[str] | None,
    url: str,
) -> tuple[str, dict[str, str] | None] | None:
    if wheel_cache_dir is None:
        return None

    entry_dir_text = wheel_cache_path(os.fspath(wheel_cache_dir), cache_identity(url))

    try:
        with os.scandir(entry_dir_text) as entries:
            wheels = sorted(
                entry.path
                for entry in entries
                if entry.name.endswith(".whl") and entry.is_file()
            )

    except OSError:
        return None

    if not wheels:
        return None

    return wheels[0], origin_hashes(os.path.join(entry_dir_text, "origin.json"))


def cache_built_wheel(
    wheel_cache_dir: str | os.PathLike[str] | None,
    candidate: CandidateRecord,
    wheel: str,
) -> None:
    if wheel_cache_dir is None:
        return

    entry_dir_text = wheel_cache_path(
        os.fspath(wheel_cache_dir),
        cache_identity(candidate.link.url),
    )

    os.makedirs(entry_dir_text, exist_ok=True)

    shutil.copy2(wheel, os.path.join(entry_dir_text, os.path.basename(wheel)))

    origin = {"archive_info": {"hashes": source_hashes_for_link(candidate.link)}}

    with open(
        os.path.join(entry_dir_text, "origin.json"),
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(origin, file)


def emit_build_message(message: str) -> None:
    if not os.environ.get("CPIP_QUIET"):
        print(message)
