"""Secure archive extraction for installation and build workflows."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from typing import Any, cast
from zipfile import ZipInfo

from cpip.core.errors import InstallationError
from cpip.core.filesystem import ensure_dir

BZ2_EXTENSIONS: tuple[str, ...] = (".tar.bz2", ".tbz")
XZ_EXTENSIONS: tuple[str, ...] = (
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
)
ZIP_EXTENSIONS: tuple[str, ...] = (".zip", ".whl")
TAR_EXTENSIONS: tuple[str, ...] = (".tar.gz", ".tgz", ".tar")


logger = logging.getLogger(__name__)


SUPPORTED_EXTENSIONS = ZIP_EXTENSIONS + TAR_EXTENSIONS

try:
    import bz2  # noqa

    SUPPORTED_EXTENSIONS += BZ2_EXTENSIONS
except ImportError:
    logger.debug("bz2 module is not available")

try:
    # Only for Python 3.3+
    import lzma  # noqa

    SUPPORTED_EXTENSIONS += XZ_EXTENSIONS
except ImportError:
    logger.debug("lzma module is not available")


def split_leading_dir(path: str) -> list[str]:
    path = path.lstrip("/").lstrip("\\")
    if "/" in path and (
        ("\\" in path and path.find("/") < path.find("\\")) or "\\" not in path
    ):
        return path.split("/", 1)
    elif "\\" in path:
        return path.split("\\", 1)
    else:
        return [path, ""]


def has_leading_dir(paths: Iterable[str]) -> bool:
    """Returns true if all the paths have the same leading path name
    (i.e., everything is in one subdirectory in an archive)"""
    common_prefix = None
    for path in paths:
        prefix, rest = split_leading_dir(path)
        if not prefix:
            return False
        elif common_prefix is None:
            common_prefix = prefix
        elif prefix != common_prefix:
            return False
    return True


def is_within_directory(
    directory: str, target: str, *, resolve_symlinks: bool = False
) -> bool:
    """
    Return true if the absolute path of target is within the directory
    (including when target is equal to the directory).

    When ``resolve_symlinks`` is true, resolve symlinks before comparing so
    traversal through a symlink (e.g. "link/../file") is also caught.
    """
    if resolve_symlinks:
        abs_directory = os.path.realpath(directory)
        abs_target = os.path.realpath(target)
    else:
        abs_directory = os.path.abspath(directory)
        abs_target = os.path.abspath(target)

    return abs_target == abs_directory or abs_target.startswith(abs_directory + os.sep)


def set_extracted_file_to_default_mode_plus_executable(path: str) -> None:
    """
    Make file present at path have execute for user/group/world
    (chmod +x) is no-op on windows per python docs
    """
    mask = os.umask(0)
    os.umask(mask)
    os.chmod(path, 0o777 & ~mask | 0o111)


def zip_item_is_executable(info: ZipInfo) -> bool:
    mode = info.external_attr >> 16
    # if mode and regular file and any execute permissions for
    # user/group/world?
    return bool(mode and stat.S_ISREG(mode) and mode & 0o111)


def unzip_file(filename: str, location: str, flatten: bool = True) -> None:
    """
    Unzip the file (with path `filename`) to the destination `location`.  All
    files are written based on system defaults and umask (i.e. permissions are
    not preserved), except that regular file members with any execute
    permissions (user, group, or world) have "chmod +x" applied after being
    written. Note that for windows, any execute changes using os.chmod are
    no-ops per the python docs.
    """
    ensure_dir(location)
    zipfp = open(filename, "rb")
    try:
        zip = zipfile.ZipFile(zipfp, allowZip64=True)
        leading = has_leading_dir(zip.namelist()) and flatten
        for info in zip.infolist():
            name = info.filename
            fn = name
            if leading:
                fn = split_leading_dir(name)[1]
            fn = os.path.join(location, fn)
            dir = os.path.dirname(fn)
            if not is_within_directory(location, fn):
                message = (
                    "The zip file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(message.format(filename, fn, location))
            if fn.endswith(("/", "\\")):
                # A directory
                ensure_dir(fn)
            else:
                ensure_dir(dir)
                # Don't use read() to avoid allocating an arbitrarily large
                # chunk of memory for the file's content
                fp = zip.open(name)
                try:
                    with open(fn, "wb") as destfp:
                        shutil.copyfileobj(fp, destfp)
                finally:
                    fp.close()
                    if zip_item_is_executable(info):
                        set_extracted_file_to_default_mode_plus_executable(fn)
    finally:
        zipfp.close()


def untar_file(filename: str, location: str) -> None:
    """
    Untar the file (with path `filename`) to the destination `location`.
    All files are written based on system defaults and umask (i.e. permissions
    are not preserved), except that regular file members with any execute
    permissions (user, group, or world) have "chmod +x" applied on top of the
    default.  Note that for windows, any execute changes using os.chmod are
    no-ops per the python docs.
    """
    ensure_dir(location)
    if filename.lower().endswith(".gz") or filename.lower().endswith(".tgz"):
        mode = "r:gz"
    elif filename.lower().endswith(BZ2_EXTENSIONS):
        mode = "r:bz2"
    elif filename.lower().endswith(XZ_EXTENSIONS):
        mode = "r:xz"
    elif filename.lower().endswith(".tar"):
        mode = "r"
    else:
        logger.warning(
            "Cannot determine compression type for file %s",
            filename,
        )
        mode = "r:*"

    tar = tarfile.open(filename, mode, encoding="utf-8")
    try:
        leading = has_leading_dir([member.name for member in tar.getmembers()])

        # PEP 706 added `tarfile.data_filter`, and made some other changes to
        # Python's tarfile module (see below). The features were backported to
        # security releases.
        try:
            data_filter = tarfile.data_filter
        except AttributeError:
            untar_without_filter(filename, location, tar, leading)
        else:
            mask = os.umask(0)
            os.umask(mask)
            default_mode_plus_executable = 0o777 & ~mask | 0o111

            if leading:
                # Strip the leading directory from all files in the archive,
                # including hardlink targets (which are relative to the
                # unpack location).
                for member in tar.getmembers():
                    name_lead, name_rest = split_leading_dir(member.name)
                    member.name = name_rest
                    if member.islnk():
                        lnk_lead, lnk_rest = split_leading_dir(member.linkname)
                        if lnk_lead == name_lead:
                            member.linkname = lnk_rest

            def cpip_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
                orig_mode = member.mode
                try:
                    try:
                        member = data_filter(member, location)
                    except tarfile.LinkOutsideDestinationError:
                        if sys.version_info[:3] in {
                            (3, 9, 17),
                            (3, 10, 12),
                            (3, 11, 4),
                        }:
                            # The tarfile filter in specific Python versions
                            # raises LinkOutsideDestinationError on valid input
                            # (https://github.com/python/cpython/issues/107845)
                            # Ignore the error there, but do use the
                            # more lax `tar_filter`
                            member = tarfile.tar_filter(member, location)
                        else:
                            raise
                except tarfile.TarError as exc:
                    message = "Invalid member in the tar file {}: {}"
                    # Filter error messages mention the member name.
                    # No need to add it here.
                    raise InstallationError(
                        message.format(
                            filename,
                            exc,
                        )
                    )
                if member.isfile() and orig_mode & 0o111:
                    member.mode = default_mode_plus_executable
                else:
                    # See PEP 706 note above.
                    # The PEP changed this from `int` to `Optional[int]`,
                    # where None means "use the default". Mypy doesn't
                    # know this yet.
                    cast(Any, member).mode = None
                return member

            tar.extractall(location, filter=cpip_filter)

    finally:
        tar.close()


def is_symlink_target_in_tar(tar: tarfile.TarFile, tarinfo: tarfile.TarInfo) -> bool:
    """Check if the file pointed to by the symbolic link is in the tar archive"""
    linkname = os.path.join(os.path.dirname(tarinfo.name), tarinfo.linkname)

    linkname = os.path.normpath(linkname)
    linkname = linkname.replace("\\", "/")

    try:
        tar.getmember(linkname)
        return True
    except KeyError:
        return False


def untar_without_filter(
    filename: str,
    location: str,
    tar: tarfile.TarFile,
    leading: bool,
) -> None:
    """Fallback for Python without tarfile.data_filter"""
    # NOTE: This function can be removed once cpip requires CPython ≥ 3.12.​
    # PEP 706 added tarfile.data_filter, made tarfile extraction operations more secure.
    # This feature is fully supported from CPython 3.12 onward.
    for member in tar.getmembers():
        fn = member.name
        if leading:
            fn = split_leading_dir(fn)[1]
        path = os.path.join(location, fn)

        # The plain check rejects textual ".." escapes; resolving symlinks also
        # catches a later member redirected outside by an earlier member's
        # symlink (e.g. "link/../file").
        if not is_within_directory(location, path) or not is_within_directory(
            location, path, resolve_symlinks=True
        ):
            message = (
                "The tar file ({}) has a file ({}) trying to install "
                "outside target directory ({})"
            )
            raise InstallationError(message.format(filename, path, location))
        if member.isdir():
            ensure_dir(path)
        elif member.issym():
            # Reject symlinks resolving outside the destination, so a later
            # member cannot be written through them.
            target = os.path.join(os.path.dirname(path), member.linkname)
            if not is_within_directory(location, target, resolve_symlinks=True):
                message = (
                    "The tar file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(
                    message.format(filename, member.name, member.linkname)
                )
            if not is_symlink_target_in_tar(tar, member):
                message = (
                    "The tar file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(
                    message.format(filename, member.name, member.linkname)
                )
            try:
                # Avoid tarfile's internal ``data_filter`` lookup here: it is
                # absent on older Python versions and can also be deliberately
                # disabled by callers testing the fallback path.
                ensure_dir(os.path.dirname(path))
                os.symlink(member.linkname, path)
            except Exception as exc:
                # Some corrupt tar files seem to produce this
                # (specifically bad symlinks)
                logger.warning(
                    "In the tar file %s the member %s is invalid: %s",
                    filename,
                    member.name,
                    exc,
                )
                continue
        else:
            try:
                fp = tar.extractfile(member)
            except (KeyError, AttributeError) as exc:
                # Some corrupt tar files seem to produce this
                # (specifically bad symlinks)
                logger.warning(
                    "In the tar file %s the member %s is invalid: %s",
                    filename,
                    member.name,
                    exc,
                )
                continue
            ensure_dir(os.path.dirname(path))
            assert fp is not None
            with open(path, "wb") as destfp:
                shutil.copyfileobj(fp, destfp)
            fp.close()
            # Update the timestamp (useful for cython compiled files)
            tar.utime(member, path)
            # member have any execute permissions for user/group/world?
            if member.mode & 0o111:
                set_extracted_file_to_default_mode_plus_executable(path)


class ArchiveExtractor:
    """Detect and securely extract one archive into a destination."""

    def __init__(
        self,
        filename: str,
        location: str,
        content_type: str | None = None,
    ) -> None:
        self.filename = os.path.realpath(filename)
        self.location = location
        self.content_type = content_type

    def extract(self) -> None:
        """Extract using content type, extension, then unambiguous signatures."""
        zip_flatten = not self.filename.endswith(".whl")

        if self.content_type == "application/zip":
            unzip_file(self.filename, self.location, flatten=zip_flatten)
            return
        if self.content_type == "application/x-gzip":
            untar_file(self.filename, self.location)
            return

        if self.filename.lower().endswith(ZIP_EXTENSIONS):
            unzip_file(self.filename, self.location, flatten=zip_flatten)
            return
        if self.filename.lower().endswith(
            TAR_EXTENSIONS + BZ2_EXTENSIONS + XZ_EXTENSIONS
        ):
            untar_file(self.filename, self.location)
            return

        # Avoid the ambiguous case where both signature checks return true.
        is_zipfile = zipfile.is_zipfile(self.filename)
        is_tarfile = tarfile.is_tarfile(self.filename)
        if is_zipfile and not is_tarfile:
            unzip_file(self.filename, self.location, flatten=zip_flatten)
            return
        if is_tarfile and not is_zipfile:
            untar_file(self.filename, self.location)
            return
        if is_zipfile and is_tarfile:
            logger.error("Ambiguous file signature in %s.", self.filename)

        logger.critical(
            "Cannot unpack file %s (downloaded from %s, content-type: %s); "
            "cannot detect archive format",
            self.filename,
            self.location,
            self.content_type,
        )
        raise InstallationError(f"Cannot determine archive format of {self.location}")
