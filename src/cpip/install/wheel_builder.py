"""Orchestrator for building wheels from InstallRequirements."""

from __future__ import annotations

import logging
import os.path
import re
from collections.abc import Iterable
from tempfile import TemporaryDirectory

from cpip.build.cache import WheelCache
from cpip.build.metadata import MetadataDistribution
from cpip.build.wheels import build_wheel_pep517
from cpip.core.errors import InvalidWheelFilename, UnsupportedWheel
from cpip.core.filesystem import ensure_dir
from cpip.core.hashes import hash_file
from cpip.core.packaging import (
    InvalidVersion,
    Version,
    canonicalize_name,
    canonicalize_version,
)
from cpip.core.urls import path_to_url
from cpip.core.wheel import Wheel
from cpip.index.links import Link
from cpip.resolution.req_install import InstallRequirement
from cpip.vcs.versioncontrol import vcs

logger = logging.getLogger(__name__)


egg_info_re = re.compile(r"([a-z0-9_.]+)-([a-z0-9_.!+-]+)", re.IGNORECASE)

BuildResult = tuple[list[InstallRequirement], list[InstallRequirement]]


def should_cache(
    req: InstallRequirement,
) -> bool:
    """Return whether a built InstallRequirement can be stored in the persistent
    wheel cache, assuming the wheel cache is available.
    """
    if req.editable or not req.source_dir:
        # never cache editable requirements
        return False
    if req.link and req.link.is_existing_dir:
        # never cache local directory requirements
        return False

    if req.link and req.link.is_vcs:
        # VCS checkout. Do not cache
        # unless it points to an immutable commit hash.
        assert not req.editable
        assert req.source_dir
        vcs_backend = vcs.get_backend_for_scheme(req.link.scheme)
        assert vcs_backend
        if vcs_backend.is_immutable_rev_checkout(req.link.url, req.source_dir):
            return True
        return False

    assert req.link
    base, ext = req.link.splitext()
    if egg_info_re.search(base):
        return True

    # Otherwise, do not cache.
    return False


def verify_one(req: InstallRequirement, wheel_path: str) -> None:
    canonical_name = canonicalize_name(req.name or "")
    w = Wheel(os.path.basename(wheel_path))
    if w.name != canonical_name:
        raise InvalidWheelFilename(
            f"Wheel has unexpected file name: expected {canonical_name!r}, "
            f"got {w.name!r}",
        )
    dist = MetadataDistribution.from_wheel(wheel_path, canonical_name)
    dist_verstr = str(dist.version)
    if canonicalize_version(dist_verstr) != canonicalize_version(w.version):
        raise InvalidWheelFilename(
            f"Wheel has unexpected file name: expected {dist_verstr!r}, "
            f"got {w.version!r}",
        )
    metadata_version_value = dist.metadata_version
    if metadata_version_value is None:
        raise UnsupportedWheel("Missing Metadata-Version")
    try:
        metadata_version = Version(metadata_version_value)
    except InvalidVersion:
        msg = f"Invalid Metadata-Version: {metadata_version_value}"
        raise UnsupportedWheel(msg)
    if metadata_version >= Version("1.2") and not isinstance(dist.version, Version):
        raise UnsupportedWheel(
            f"Metadata 1.2 mandates PEP 440 version, but {dist_verstr!r} is not",
        )


def build_one(
    req: InstallRequirement,
    output_dir: str,
    verify: bool,
    editable: bool,
) -> str | None:
    """Build one wheel.

    :return: The filename of the built wheel, or None if the build failed.
    """
    artifact = "editable" if editable else "wheel"
    try:
        ensure_dir(output_dir)
    except OSError as e:
        logger.warning(
            "Building %s for %s failed: %s",
            artifact,
            req.name,
            e,
        )
        return None

    # Install build deps into temporary directory (PEP 518)
    with req.build_env:
        wheel_path = build_one_inside_env(req, output_dir, editable)
    if wheel_path and verify:
        try:
            verify_one(req, wheel_path)
        except (InvalidWheelFilename, UnsupportedWheel) as e:
            logger.warning("Built %s for %s is invalid: %s", artifact, req.name, e)
            return None
    return wheel_path


def build_one_inside_env(
    req: InstallRequirement,
    output_dir: str,
    editable: bool,
) -> str | None:
    with TemporaryDirectory(dir=output_dir) as wheel_directory:
        assert req.name
        assert req.metadata_directory
        assert req.pep517_backend
        wheel_path = build_wheel_pep517(
            name=req.name,
            backend=req.pep517_backend,
            metadata_directory=req.metadata_directory,
            wheel_directory=wheel_directory,
            editable=editable,
        )

        if wheel_path is not None:
            wheel_name = os.path.basename(wheel_path)
            dest_path = os.path.join(output_dir, wheel_name)
            try:
                wheel_hash, length = hash_file(wheel_path)
                # We can do a replace here because wheel_path is guaranteed to
                # be in the same filesystem as output_dir. This will perform an
                # atomic rename, which is necessary to avoid concurrency issues
                # when populating the cache.
                os.replace(wheel_path, dest_path)
                logger.info(
                    "Created wheel for %s: filename=%s size=%d sha256=%s",
                    req.name,
                    wheel_name,
                    length,
                    wheel_hash.hexdigest(),
                )
                logger.info("Stored in directory: %s", output_dir)
                return dest_path
            except Exception as e:
                logger.warning(
                    "Building wheel for %s failed: %s",
                    req.name,
                    e,
                )
        return None


class WheelBuilder:
    """Build prepared requirements with one cache and verification policy."""

    def __init__(self, wheel_cache: WheelCache, *, verify: bool) -> None:
        self.wheel_cache = wheel_cache
        self.verify = verify

    def build(self, requirements: Iterable[InstallRequirement]) -> BuildResult:
        return build_internal(
            requirements,
            wheel_cache=self.wheel_cache,
            verify=self.verify,
        )


def build_internal(
    requirements: Iterable[InstallRequirement],
    wheel_cache: WheelCache,
    verify: bool,
) -> BuildResult:
    """Build wheels.

    :return: The list of InstallRequirement that succeeded to build and
        the list of InstallRequirement that failed to build.
    """
    requirements = list(requirements)
    if not requirements:
        return [], []

    logger.info(
        "Building wheels for collected packages: %s",
        ", ".join(req.name or "" for req in requirements),
    )

    build_successes, build_failures = [], []
    for req in requirements:
        assert req.name
        assert req.link
        if wheel_cache.cache_dir and should_cache(req):
            cache_dir = wheel_cache.get_path_for_link(req.link)
        else:
            cache_dir = wheel_cache.get_ephem_path_for_link(req.link)
        wheel_file = build_one(
            req,
            cache_dir,
            verify,
            req.editable and req.permit_editable_wheels,
        )
        if wheel_file:
            if req.download_info is not None:
                wheel_cache.record_download_origin(cache_dir, req.download_info)
            req.link = Link(path_to_url(wheel_file))
            req.local_file_path = req.link.file_path
            assert req.link.is_wheel
            build_successes.append(req)
        else:
            build_failures.append(req)

    # notify success/failure
    if build_successes:
        logger.info(
            "Successfully built %s",
            " ".join([req.name for req in build_successes]),
        )
    if build_failures:
        logger.info(
            "Failed to build %s",
            " ".join([req.name for req in build_failures]),
        )
    # Return a list of requirements that failed to build
    return build_successes, build_failures
