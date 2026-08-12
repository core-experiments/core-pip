"""Prepares a distribution for installation"""

# The following comment should be removed at some point in the future.
# mypy: strict-optional=False
from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable

from cpip.build.metadata import (
    MetadataDistribution,
)
from cpip.build.tracker import BuildTracker
from cpip.core.direct_url import ArchiveInfo, DirectUrl, DirInfo, VcsInfo
from cpip.core.errors import (
    DirectoryUrlHashUnsupported,
    HashUnpinned,
    InstallationError,
    VcsHashUnsupported,
)
from cpip.core.utils import display_path
from cpip.core.hashes import Hashes, MissingHashes, hash_file
from cpip.core.packaging import Requirement, canonicalize_name
from cpip.core.temp_dir import TempDirectory
from cpip.core.urls import path_to_url
from cpip.core.wheel import Wheel
from cpip.index.links import Link
from cpip.index.paths import PathComponent
from cpip.install.build_env.base import BuildEnvironmentInstaller, BuildIsolationMode
from cpip.install.metadata import direct_url_from_link
from cpip.install.downloads import (
    DownloadManager,
    File,
    check_download_dir,
)
from cpip.install.metadata import (
    DistributionPreparer,
    MetadataInconsistent,
    MetadataView,
    check_sidecar_matches_wheel,
)
from cpip.install.requirements import RequirementInstaller
from cpip.install.source import SourceManager
from cpip.network.download import Downloader
from cpip.network.exceptions import NetworkConnectionError
from cpip.network.http import NetworkSession
from cpip.network.lazy_wheel import (
    HTTPRangeRequestUnsupported,
    dist_from_wheel_url,
)
from cpip.vcs.support import hide_url
from cpip.vcs.versioncontrol import vcs

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


def redact_auth_from_requirement(req: Requirement) -> str:
    if not req.url:
        return str(req)
    return str(req).replace(req.url, str(hide_url(req.url)))


def unpack_vcs_link(link: Link, location: str, verbosity: int) -> None:
    vcs_backend = vcs.get_backend_for_scheme(link.scheme)
    assert vcs_backend is not None
    vcs_backend.unpack(location, url=hide_url(link.url), verbosity=verbosity)


class RequirementPreparer:
    """Prepares a Requirement"""

    def __init__(
        self,
        *,
        build_dir: str,
        download_dir: str | None,
        src_dir: str,
        build_isolation: BuildIsolationMode,
        build_isolation_installer: BuildEnvironmentInstaller,
        check_build_deps: bool,
        build_tracker: BuildTracker,
        session: NetworkSession,
        require_hashes: bool,
        lazy_wheel: bool,
        verbosity: int,
    ) -> None:
        super().__init__()

        self.src_dir = src_dir
        self.build_dir = build_dir
        self.build_tracker = build_tracker
        self.session_internal = session
        self.download_internal = Downloader(session)
        self.downloads_internal = DownloadManager(
            self.download_internal,
            download_dir=download_dir,
            check_download_dir=check_download_dir,
        )
        self.requirements_internal = RequirementInstaller()
        self.distribution_preparer = DistributionPreparer(
            build_tracker,
            build_isolation_installer,
            build_isolation,
            check_build_deps,
        )

        # Where still-packed archives should be written to. If None, they are
        # not saved, and are deleted immediately after unpacking.
        self.download_dir = download_dir

        # Is build isolation allowed?
        self.build_isolation = build_isolation
        self.build_env_installer = build_isolation_installer

        # Should check build dependencies?
        self.check_build_deps = check_build_deps

        # Should hash-checking be required?
        self.require_hashes = require_hashes

        # Should wheels be downloaded lazily?
        self.use_lazy_wheel = lazy_wheel

        # How verbose should underlying tooling be?
        self.verbosity = verbosity

        # Memoized downloaded files, as mapping of url: path.
        self.downloaded_internal: dict[str, str] = {}

        # Previous "header" printed for a link-based InstallRequirement
        self.previous_requirement_header = ("", "")

    def log_preparing_link(self, req: InstallRequirement) -> None:
        """Provide context for the requirement being prepared."""
        assert req.link is not None
        if req.link.is_file and not req.is_wheel_from_cache:
            message = "Processing %s"
            information = str(display_path(req.link.file_path))
        else:
            message = "Collecting %s"
            information = redact_auth_from_requirement(req.req) if req.req else str(req)

        # If we used req.req, inject requirement source if available (this
        # would already be included if we used req directly)
        if req.req and req.comes_from:
            if isinstance(req.comes_from, str):
                comes_from: str | None = req.comes_from
            else:
                comes_from = req.comes_from.from_path()
            if comes_from:
                information += f" (from {comes_from})"

        if (message, information) != self.previous_requirement_header:
            self.previous_requirement_header = (message, information)
            logger.info(message, information)

        if req.is_wheel_from_cache:
            logger.info("Using cached %s", req.link.filename)

    def ensure_link_req_src_dir(self, req: InstallRequirement) -> None:
        """Ensure source_dir of a linked InstallRequirement."""
        assert req.link is not None
        # Since source_dir is only set for editable requirements.
        if req.link.is_wheel:
            # We don't need to unpack wheels, so no need for a source
            # directory.
            return
        assert req.source_dir is None
        if req.link.is_existing_dir:
            # build local directories in-tree
            req.source_dir = req.link.file_path
            return

        req.ensure_has_source_dir(self.build_dir)
        req.ensure_pristine_source_checkout()

    def check_download_dir_for_requirement(
        self,
        req: InstallRequirement,
        *,
        warn_on_hash_mismatch: bool = True,
    ) -> str | None:
        assert req.link is not None
        if self.download_dir is None or not req.link.is_wheel:
            return None
        return self.downloads_internal.cached_path(
            req.link,
            self.get_linked_req_hashes(req),
            warn_on_hash_mismatch=warn_on_hash_mismatch,
        )

    def get_linked_req_hashes(self, req: InstallRequirement) -> Hashes:
        # By the time this is called, the requirement's link should have
        # been checked so we can tell what kind of requirements req is
        # and raise some more informative errors than otherwise.
        # (For example, we can raise VcsHashUnsupported for a VCS URL
        # rather than HashMissing.)
        assert req.link is not None
        if not self.require_hashes:
            return req.hashes(trust_internet=True)

        # We could check these first 2 conditions inside DownloadManager.unpack
        # and save repetition of conditions, but then we would
        # report less-useful error messages for unhashable
        # requirements, complaining that there's no hash provided.
        if req.link.is_vcs:
            raise VcsHashUnsupported()
        if req.link.is_existing_dir:
            raise DirectoryUrlHashUnsupported()

        # Unpinned packages are asking for trouble when a new version
        # is uploaded.  This isn't a security check, but it saves users
        # a surprising hash mismatch in the future.
        # file:/// URLs aren't pinnable, so don't complain about them
        # not being pinned.
        if not req.is_direct and not req.is_pinned:
            raise HashUnpinned()

        # If known-good hashes are missing for this requirement,
        # shim it with a facade object that will provoke hash
        # computation and then raise a HashMissing exception
        # showing the user what the hash should be.
        return req.hashes(trust_internet=False) or MissingHashes()

    def fetch_metadata_only(
        self,
        req: InstallRequirement,
    ) -> MetadataView | None:
        assert req.link is not None
        if self.require_hashes:
            logger.debug(
                "Metadata-only fetching is not used as hash checking is required",
            )
            return None
        # Try PEP 658 metadata first, then fall back to lazy wheel if unavailable.
        return self.fetch_metadata_using_link_data_attr(
            req,
        ) or self.fetch_metadata_using_lazy_wheel(req.link)

    def fetch_metadata_using_link_data_attr(
        self,
        req: InstallRequirement,
    ) -> MetadataView | None:
        """Fetch metadata from the data-dist-info-metadata attribute, if possible."""
        # (1) Get the link to the metadata file, if provided by the backend.
        assert req.link is not None
        metadata_link = req.link.metadata_link()
        if metadata_link is None:
            return None
        assert req.req is not None
        logger.info(
            "Obtaining dependency information for %s from %s",
            req.req,
            metadata_link,
        )
        # (2) Download the contents of the METADATA file, separate from the dist itself.
        metadata_file = self.downloads_internal.http_file(
            metadata_link,
            hashes=metadata_link.as_hashes(),
        )
        with open(metadata_file.path, "rb") as f:
            metadata_contents = f.read()
        # (3) Generate a dist just from those file contents.
        metadata_dist = MetadataDistribution.from_metadata_file_contents(
            metadata_contents,
            req.req.name,
        )
        # (4) Ensure the Name: field from the METADATA file matches the name from the
        #     install requirement.
        #
        #     NB: raw_name will fall back to the name from the install requirement if
        #     the Name: field is not present, but it's noted in the raw_name docstring
        #     that that should NEVER happen anyway.
        if canonicalize_name(metadata_dist.raw_name) != canonicalize_name(req.req.name):
            raise MetadataInconsistent(
                req,
                "Name",
                req.req.name,
                metadata_dist.raw_name,
            )
        return metadata_dist

    def fetch_metadata_using_lazy_wheel(
        self,
        link: Link,
    ) -> MetadataView | None:
        """Fetch metadata using lazy wheel, if possible."""
        # --use-feature=fast-deps must be provided.
        if not self.use_lazy_wheel:
            return None
        if link.is_file or not link.is_wheel:
            logger.debug(
                "Lazy wheel is not used as %r does not point to a remote wheel",
                link,
            )
            return None

        wheel = Wheel(link.filename)
        name = wheel.name
        logger.info(
            "Obtaining dependency information from %s %s",
            name,
            wheel.version,
        )
        url = link.url.split("#", 1)[0]
        try:
            return dist_from_wheel_url(name, url, self.session_internal)
        except HTTPRangeRequestUnsupported:
            logger.debug("%s does not support range requests", url)
            return None

    def complete_partial_requirements(
        self,
        partially_downloaded_reqs: Iterable[InstallRequirement],
    ) -> None:
        """Download any requirements which were only fetched by metadata."""
        # Download to a temporary directory. These will be copied over as
        # needed for downstream 'download', 'wheel', and 'install' commands.
        temp_dir = TempDirectory(kind="unpack", globally_managed=True).path

        # Map each link to the requirement that owns it. This allows us to set
        # `req.local_file_path` on the appropriate requirement after passing
        # all the links at once into BatchDownloader.
        links_to_fully_download: dict[Link, InstallRequirement] = {}
        for req in partially_downloaded_reqs:
            assert req.link
            links_to_fully_download[req.link] = req

        batch_download = self.download_internal.batch(
            links_to_fully_download.keys(),
            temp_dir,
        )
        for link, (filepath, _) in batch_download:
            logger.debug("Downloading link %s to %s", link, filepath)
            req = links_to_fully_download[link]
            # Record the downloaded file path so wheel reqs can extract a Distribution
            # in .get_dist().
            req.local_file_path = filepath
            # Record that the file is downloaded so we don't do it again in
            # _prepare_linked_requirement().
            assert req.link is not None
            self.downloaded_internal[req.link.url] = filepath

            # If this is an sdist, we need to unpack it after downloading, but the
            # .source_dir won't be set up until we are in _prepare_linked_requirement().
            # Add the downloaded archive to the install requirement to unpack after
            # preparing the source dir.
            if not req.is_wheel:
                req.needs_unpacked_archive(filepath)

        # This step is necessary to ensure all lazy wheels are processed
        # successfully by the 'download', 'wheel', and 'install' commands.
        for req in partially_downloaded_reqs:
            self.prepare_linked_requirement_internal(req)

    def prepare_linked_requirement(self, req: InstallRequirement) -> MetadataView:
        """Prepare a requirement to be obtained from req.link."""
        assert req.link
        self.log_preparing_link(req)
        # Check if the relevant file is already available in the download
        # directory. When a locally built wheel has been found in cache, we
        # don't warn about re-downloading when its hash does not match. The
        # original link's hash must be checked against the original link, not
        # the cached link.
        file_path = self.check_download_dir_for_requirement(
            req,
            warn_on_hash_mismatch=not req.is_wheel_from_cache,
        )

        if file_path is not None:
            # The file is already available, so mark it as downloaded.
            self.downloaded_internal[req.link.url] = file_path
        else:
            # The file is not available, attempt to fetch only metadata.
            metadata_dist = self.fetch_metadata_only(req)
            if metadata_dist is not None:
                req.needs_more_preparation = True
                req.set_dist(metadata_dist)
                # Ensure download_info is available even in dry-run mode.
                if req.download_info is None:
                    req.download_info = direct_url_from_link(
                        req.link,
                        source_dir=req.source_dir,
                    )
                return metadata_dist

        # None of the optimizations worked, fully prepare the requirement.
        return self.prepare_linked_requirement_internal(req)

    def prepare_linked_requirements_more(
        self,
        reqs: Iterable[InstallRequirement],
    ) -> None:
        """Prepare linked requirements more, if needed."""
        reqs = [req for req in reqs if req.link is not None]
        reqs = [req for req in reqs if req.needs_more_preparation]
        for req in reqs:
            assert req.link is not None
            # Determine if any of these requirements were already downloaded.
            file_path = self.check_download_dir_for_requirement(req)
            if file_path is not None:
                self.downloaded_internal[req.link.url] = file_path
                req.needs_more_preparation = False

        # Prepare requirements we found were already downloaded for some
        # reason. The other downloads will be completed separately.
        partially_downloaded_reqs: list[InstallRequirement] = []
        for req in reqs:
            if req.needs_more_preparation:
                partially_downloaded_reqs.append(req)
            else:
                self.prepare_linked_requirement_internal(req)

        # TODO: separate this part out from RequirementPreparer when the v1
        # resolver can be removed!
        self.complete_partial_requirements(partially_downloaded_reqs)

    def prepare_linked_requirement_internal(
        self,
        req: InstallRequirement,
    ) -> MetadataView:
        assert req.link is not None
        link = req.link

        hashes = self.get_linked_req_hashes(req)

        if hashes and req.is_wheel_from_cache:
            assert req.download_info is not None
            assert link.is_wheel
            assert link.is_file
            # We need to verify hashes, and we have found the requirement in the cache
            # of locally built wheels.
            if (
                req.download_info.archive_info
                and req.download_info.archive_info.hashes
                and hashes.has_one_of(req.download_info.archive_info.hashes)
            ):
                # At this point we know the requirement was built from a hashable source
                # artifact, and we verified that the cache entry's hash of the original
                # artifact matches one of the hashes we expect. We don't verify hashes
                # against the cached wheel, because the wheel is not the original.
                hashes = None
            else:
                logger.warning(
                    "The hashes of the source archive found in cache entry "
                    "don't match, ignoring cached built wheel "
                    "and re-downloading source.",
                )
                req.link = req.cached_wheel_source_link
                assert req.link is not None
                link = req.link

        self.ensure_link_req_src_dir(req)

        if link.is_existing_dir:
            local_file = None
        elif link.url not in self.downloaded_internal:
            try:
                local_file = self.downloads_internal.unpack(
                    link,
                    req.source_dir or self.src_dir,
                    self.verbosity,
                    hashes,
                    unpack_vcs=unpack_vcs_link,
                )
            except NetworkConnectionError as exc:
                raise InstallationError(
                    f"Could not install requirement {req} because of HTTP "
                    f"error {exc} for URL {link}",
                )
        else:
            file_path = self.downloaded_internal[link.url]
            if hashes:
                hashes.check_against_path(file_path)
            local_file = File(file_path, content_type=None)

        # If download_info is set, we got it from the wheel cache.
        if req.download_info is None:
            # Editables don't go through this function (see
            # prepare_editable_requirement).
            assert not req.editable
            req.download_info = direct_url_from_link(link, source_dir=req.source_dir)
            # Make sure we have a hash in download_info. If we got it as part of the
            # URL, it will have been verified and we can rely on it. Otherwise we
            # compute it from the downloaded file.
            # FIXME: https://github.com/pypa/cpip/issues/11943
            if (
                req.download_info.archive_info
                and not req.download_info.archive_info.hashes
                and local_file
            ):
                hash = hash_file(local_file.path)[0].hexdigest()
                # We populate archive_info.hashes. For backward compatibility,
                # the legacy hash field will be generated when converting to JSON.
                req.download_info = DirectUrl(
                    url=req.download_info.url,
                    archive_info=ArchiveInfo(hashes={"sha256": hash}),
                    info_subdir=req.download_info.subdirectory,
                )

        # For use in later processing,
        # preserve the file path on the requirement.
        if local_file:
            req.local_file_path = local_file.path

        dist = self.distribution_preparer.prepare(req)

        # If a PEP 658 .metadata file was used, check that fields relevant for
        # dependency resolution match with the wheel's METADATA file.
        #
        # NOTE: PEP 658 also permits .metadata files for source distributions,
        # but PyPI doesn't serve such files. In addition, an sdist's metadata
        # is generated at build time and may legitimately differ from what the
        # index declared, so it's been decided to skip this check for sdists.
        # This can change later if needed.
        #
        # TODO: this is a hack for checking whether a distribution is metadata-
        # only or not. If/when we refactor distributions to delineate between
        # metadata-only and concrete distributions, clean this up.
        if (
            link.is_wheel
            and req.distribution_internal is not None
            and req.distribution_internal is not dist
            and link.metadata_link() is not None
        ):
            check_sidecar_matches_wheel(
                req,
                req.distribution_internal,  # ty:ignore[invalid-argument-type]
                dist,
            )

        return dist

    def save_linked_requirement(self, req: InstallRequirement) -> None:
        assert self.download_dir is not None
        assert req.link is not None
        link = req.link
        if link.is_vcs or (link.is_existing_dir and req.editable):
            # Make a .zip of the source_dir we already created.
            SourceManager(req).archive(self.download_dir)
            return

        if link.is_existing_dir:
            logger.debug(
                "Not copying link to destination directory since it is a directory: %s",
                link,
            )
            return
        if req.local_file_path is None:
            # No distribution was downloaded for this requirement.
            return

        download_location = PathComponent(link.filename).join(self.download_dir)
        if not os.path.exists(download_location):
            shutil.copy(req.local_file_path, download_location)
            download_path = display_path(download_location)
            logger.info("Saved %s", download_path)

    def prepare_editable_requirement(
        self,
        req: InstallRequirement,
    ) -> MetadataView:
        """Prepare an editable requirement."""
        assert req.editable, "cannot prepare a non-editable req as editable"

        logger.info("Obtaining %s", req)

        if self.require_hashes:
            raise InstallationError(
                f"The editable requirement {req} cannot be installed when "
                "requiring hashes, because there is no single file to hash.",
            )
        req.ensure_has_source_dir(self.src_dir)
        SourceManager(req).update_editable()
        assert req.source_dir
        source_path = os.path.join(
            req.source_dir,
            (req.link.subdirectory_fragment if req.link else None) or "",
        )
        vcs_backend = vcs.get_backend_for_dir(source_path)
        if vcs_backend is not None:
            req.download_info = DirectUrl(
                url=vcs_backend.get_remote_url(source_path),
                vcs_info=VcsInfo(
                    vcs=vcs_backend.name,
                    commit_id=vcs_backend.get_revision(source_path),
                ),
            )
        else:
            req.download_info = DirectUrl(
                url=path_to_url(str(source_path)),
                dir_info=DirInfo(editable=True),
            )

        dist = self.distribution_preparer.prepare(req)

        self.requirements_internal.check_if_exists(req)

        return dist

    def prepare_installed_requirement(
        self,
        req: InstallRequirement,
        skip_reason: str,
    ) -> MetadataView:
        """Prepare an already-installed requirement."""
        assert req.satisfied_by, "req should have been satisfied but isn't"
        assert skip_reason is not None, (
            f"did not get skip reason skipped but req.satisfied_by is set to {req.satisfied_by}"
        )
        logger.info(
            "Requirement %s: %s (%s)",
            skip_reason,
            req,
            req.satisfied_by.version,
        )
        if self.require_hashes:
            logger.debug(
                "Since it is already installed, we are trusting this "
                "package without checking its hash. To ensure a "
                "completely repeatable environment, install into an "
                "empty virtualenv.",
            )
        return req.satisfied_by  # ty:ignore[invalid-return-type]
