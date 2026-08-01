"""Download and unpack services used while preparing installations."""

from __future__ import annotations

import mimetypes
import os
import tempfile
import logging
from typing import Protocol

from cpip.core.errors import HashMismatch, InstallationError
from cpip.core.hashes import Hashes
from cpip.index.links import Link
from cpip.index.paths import PathComponent

from .unpacking import ArchiveExtractor

logger = logging.getLogger(__name__)


class Downloader(Protocol):
    def __call__(self, link: Link, location: str) -> tuple[str, str | None]: ...


class DownloadDirectoryChecker(Protocol):
    def __call__(
        self,
        link: Link,
        download_dir: str,
        hashes: Hashes | None,
    ) -> str | None: ...


class VCSUnpacker(Protocol):
    def __call__(self, link: Link, location: str, verbosity: int) -> None: ...


class File:
    __slots__ = ("path", "content_type")

    def __init__(self, path: str, content_type: str | None = None) -> None:
        self.path = path
        self.content_type = content_type
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.content_type is None:
            try:
                self.content_type = mimetypes.guess_type(self.path)[0]
            except OSError:
                pass


class DownloadManager:
    """Download, verify, cache, and unpack installation artifacts."""

    def __init__(
        self,
        download: Downloader,
        *,
        download_dir: str | None = None,
        check_download_dir: DownloadDirectoryChecker | None = None,
    ) -> None:
        self.download = download
        self.download_dir = download_dir
        self.check_download_dir = check_download_dir

    def cached_path(
        self,
        link: Link,
        hashes: Hashes | None,
        *,
        warn_on_hash_mismatch: bool = True,
    ) -> str | None:
        if self.download_dir is None or self.check_download_dir is None:
            return None
        if self.check_download_dir is check_download_dir:
            return check_download_dir(
                link,
                self.download_dir,
                hashes,
                warn_on_hash_mismatch=warn_on_hash_mismatch,
            )
        return self.check_download_dir(link, self.download_dir, hashes)

    def http_file(self, link: Link, hashes: Hashes | None = None) -> File:
        cached = self.cached_path(link, hashes)
        if cached:
            return File(cached)
        path, content_type = self.download(
            link, tempfile.mkdtemp(prefix="cpip-unpack-")
        )
        if hashes:
            hashes.check_against_path(path)
        return File(path, content_type)

    def local_file(self, link: Link, hashes: Hashes | None = None) -> File:
        cached = self.cached_path(link, hashes)
        path = cached or link.file_path
        if hashes:
            hashes.check_against_path(path)
        return File(path)

    def unpack(
        self,
        link: Link,
        location: str,
        verbosity: int,
        hashes: Hashes | None = None,
        *,
        unpack_vcs: VCSUnpacker | None = None,
    ) -> File | None:
        if link.is_vcs:
            if unpack_vcs is None:
                raise InstallationError("No VCS unpacker was provided")
            unpack_vcs(link, location, verbosity)
            return None

        assert not link.is_existing_dir
        file = (
            self.local_file(link, hashes)
            if link.is_file
            else self.http_file(link, hashes)
        )
        if not link.is_wheel:
            ArchiveExtractor(file.path, location, file.content_type).extract()
        return file


def check_download_dir(
    link: Link,
    download_dir: str,
    hashes: Hashes | None,
    warn_on_hash_mismatch: bool = True,
) -> str | None:
    download_path = PathComponent(link.filename).join(download_dir)
    if not os.path.exists(download_path):
        return None
    logger.info("File was already downloaded %s", download_path)
    if hashes:
        try:
            hashes.check_against_path(download_path)
        except HashMismatch:
            if warn_on_hash_mismatch:
                logger.warning(
                    "Previously-downloaded file %s has bad hash. Re-downloading.",
                    download_path,
                )
            os.unlink(download_path)
            return None
    return download_path
