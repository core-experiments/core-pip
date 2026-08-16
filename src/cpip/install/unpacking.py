"""Secure archive extraction for installation and build workflows."""

from __future__ import annotations

import logging
import os
import stat
import sys
import tarfile
import zipfile
from zipfile import ZipInfo

from cpip.core.errors import InstallationError
from cpip.core.utils import ensure_dir

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import IO

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

# Matches shutil's own default copy buffer size (not exposed in typeshed).
_COPY_BUFSIZE = 1024 * 1024 if sys.platform == "win32" else 64 * 1024


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
    if "\\" in path:
        return path.split("\\", 1)
    return [path, ""]


def has_leading_dir(paths: Iterable[str]) -> bool:
    """Returns true if all the paths have the same leading path name
    (i.e., everything is in one subdirectory in an archive)
    """
    common_prefix = None
    for path in paths:
        prefix, rest = split_leading_dir(path)
        if not prefix:
            return False
        if common_prefix is None:
            common_prefix = prefix
        elif prefix != common_prefix:
            return False
    return True


def is_within_directory(
    directory: str,
    target: str,
    *,
    resolve_symlinks: bool = False,
) -> bool:
    """Return true if the absolute path of target is within the directory
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
    """Make file present at path have execute for user/group/world
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


def _write_stream_to_path(fp: IO[bytes], path: str) -> None:
    """Copy a readable stream to `path` using raw fd calls.

    Wheels/sdists are typically many small files, so the per-member cost of
    building an `io.BufferedWriter` (and its buffered flush-then-close on
    exit) dominates over the actual bytes copied. A bare os.open/write/close
    sequence skips that object construction for every member.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        read = fp.read
        while True:
            chunk = read(_COPY_BUFSIZE)
            if not chunk:
                break
            os.write(fd, chunk)
    finally:
        os.close(fd)


def unzip_file(filename: str, location: str, flatten: bool = True) -> None:
    """Unzip the file (with path `filename`) to the destination `location`.  All
    files are written based on system defaults and umask (i.e. permissions are
    not preserved), except that regular file members with any execute
    permissions (user, group, or world) have "chmod +x" applied after being
    written. Note that for windows, any execute changes using os.chmod are
    no-ops per the python docs.
    """
    ensure_dir(location)
    absolute_location = os.path.abspath(location)
    # Members of a wheel/sdist overwhelmingly share a handful of parent
    # directories (a package's whole tree, one .dist-info), so calling
    # ensure_dir -> os.makedirs for every single member means every file
    # after the first in a directory pays a real syscall just to be told
    # EEXIST. Tracking what this extraction has already created skips that.
    ensured_dirs: set[str] = {absolute_location}
    zipfp = open(filename, "rb")
    try:
        zip = zipfile.ZipFile(zipfp, allowZip64=True)
        infos = zip.infolist()
        leading = flatten and has_leading_dir(info.filename for info in infos)
        for info in infos:
            name = info.filename
            fn = name
            if leading:
                fn = split_leading_dir(name)[1]
            fn = os.path.join(location, fn)
            dir = os.path.dirname(fn)
            absolute_fn = os.path.abspath(fn)
            if not (
                absolute_fn == absolute_location
                or absolute_fn.startswith(absolute_location + os.sep)
            ):
                message = (
                    "The zip file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(message.format(filename, fn, location))
            if fn.endswith(("/", "\\")):
                # A directory
                if absolute_fn not in ensured_dirs:
                    ensure_dir(fn)
                    ensured_dirs.add(absolute_fn)
            else:
                absolute_dir = os.path.dirname(absolute_fn)
                if absolute_dir not in ensured_dirs:
                    ensure_dir(dir)
                    ensured_dirs.add(absolute_dir)
                # Don't use read() to avoid allocating an arbitrarily large
                # chunk of memory for the file's content
                fp = zip.open(info)
                try:
                    _write_stream_to_path(fp, fn)
                finally:
                    fp.close()
                    if zip_item_is_executable(info):
                        set_extracted_file_to_default_mode_plus_executable(fn)
    finally:
        zipfp.close()


def untar_file(filename: str, location: str) -> None:
    """Untar the file (with path `filename`) to the destination `location`.
    All files are written based on system defaults and umask (i.e. permissions
    are not preserved), except that regular file members with any execute
    permissions (user, group, or world) have "chmod +x" applied on top of the
    default.  Note that for windows, any execute changes using os.chmod are
    no-ops per the python docs.
    """
    ensure_dir(location)
    filename_lower = filename.lower()
    if filename_lower.endswith(".gz") or filename_lower.endswith(".tgz"):
        mode = "r:gz"
    elif filename_lower.endswith(BZ2_EXTENSIONS):
        mode = "r:bz2"
    elif filename_lower.endswith(XZ_EXTENSIONS):
        mode = "r:xz"
    elif filename_lower.endswith(".tar"):
        mode = "r"
    else:
        logger.warning(
            "Cannot determine compression type for file %s",
            filename,
        )
        mode = "r:*"

    tar = tarfile.open(filename, mode, encoding="utf-8")
    try:
        members = tar.getmembers()
        leading = has_leading_dir(member.name for member in members)

        # PEP 706 added `tarfile.data_filter`, and made some other changes to
        # Python's tarfile module (see below). The features were backported to
        # security releases.
        try:
            data_filter = tarfile.data_filter
        except AttributeError:
            untar_without_filter(filename, location, tar, leading, members)
        else:
            mask = os.umask(0)
            os.umask(mask)
            default_mode_plus_executable = 0o777 & ~mask | 0o111

            if leading:
                # Strip the leading directory from all files in the archive,
                # including hardlink targets (which are relative to the
                # unpack location).
                for member in members:
                    name_lead, name_rest = split_leading_dir(member.name)
                    member.name = name_rest
                    if member.islnk():
                        lnk_lead, lnk_rest = split_leading_dir(member.linkname)
                        if lnk_lead == name_lead:
                            member.linkname = lnk_rest

            if _untar_regular_members(filename, location, tar, members):
                return

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
                        ),
                    )
                if member.isfile() and orig_mode & 0o111:
                    member.mode = default_mode_plus_executable
                else:
                    # See PEP 706 note above.
                    # The PEP changed this from `int` to `Optional[int]`,
                    # where None means "use the default". Mypy doesn't
                    # know this yet.
                    member.mode = None  # ty:ignore[invalid-assignment]
                return member

            tar.extractall(location, filter=cpip_filter)

    finally:
        tar.close()


def _untar_regular_members(
    filename: str,
    location: str,
    tar: tarfile.TarFile,
    members: list[tarfile.TarInfo],
) -> bool:
    """Extract a regular-only archive without repeated realpath checks."""
    if any(not (member.isfile() or member.isdir()) for member in members):
        return False
    with os.scandir(location) as entries:
        if next(entries, None) is not None:
            return False

    absolute_location = os.path.abspath(location)
    prepared: list[tuple[tarfile.TarInfo, str]] = []
    for member in members:
        path = os.path.join(location, member.name)
        absolute_path = os.path.abspath(path)
        if not (
            absolute_path == absolute_location
            or absolute_path.startswith(absolute_location + os.sep)
        ):
            raise InstallationError(
                f"{member.name!r} is outside the destination in {filename}",
            )
        prepared.append((member, path))

    mask = os.umask(0)
    os.umask(mask)
    executable_mode = 0o777 & ~mask | 0o111
    created_directories = {absolute_location}
    for member, path in prepared:
        if member.isdir():
            if path and path not in created_directories:
                os.makedirs(path, exist_ok=True)
                created_directories.add(path)
            continue
        parent = os.path.dirname(path)
        if parent not in created_directories:
            os.makedirs(parent, exist_ok=True)
            created_directories.add(parent)
        source = tar.extractfile(member)
        if source is None:
            raise InstallationError(
                f"Unable to extract {member.name!r} from {filename}",
            )
        with source:
            _write_stream_to_path(source, path)
        tar.utime(member, path)
        if member.mode & 0o111:
            os.chmod(path, executable_mode)
    return True


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
    members: list[tarfile.TarInfo],
) -> None:
    """Fallback for Python without tarfile.data_filter"""
    # NOTE: This function can be removed once cpip requires CPython ≥ 3.12.​
    # PEP 706 added tarfile.data_filter, made tarfile extraction operations more secure.
    # This feature is fully supported from CPython 3.12 onward.
    absolute_location = os.path.abspath(location)
    resolved_location = os.path.realpath(location)

    def is_within(root: str, target: str) -> bool:
        return target == root or target.startswith(root + os.sep)

    for member in members:
        fn = member.name
        if leading:
            fn = split_leading_dir(fn)[1]
        path = os.path.join(location, fn)

        # The plain check rejects textual ".." escapes; resolving symlinks also
        # catches a later member redirected outside by an earlier member's
        # symlink (e.g. "link/../file").
        if not is_within(absolute_location, os.path.abspath(path)) or not is_within(
            resolved_location,
            os.path.realpath(path),
        ):
            message = "The tar file ({}) has a file ({}) trying to install outside target directory ({})"
            raise InstallationError(message.format(filename, path, location))
        if member.isdir():
            ensure_dir(path)
        elif member.issym():
            # Reject symlinks resolving outside the destination, so a later
            # member cannot be written through them.
            target = os.path.join(os.path.dirname(path), member.linkname)
            if not is_within(resolved_location, os.path.realpath(target)):
                message = (
                    "The tar file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(
                    message.format(filename, member.name, member.linkname),
                )
            if not is_symlink_target_in_tar(tar, member):
                message = (
                    "The tar file ({}) has a file ({}) trying to install "
                    "outside target directory ({})"
                )
                raise InstallationError(
                    message.format(filename, member.name, member.linkname),
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
            try:
                _write_stream_to_path(fp, path)
            finally:
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
        self.filename = os.fspath(filename)
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

        filename_lower = self.filename.lower()
        if filename_lower.endswith(ZIP_EXTENSIONS):
            unzip_file(self.filename, self.location, flatten=zip_flatten)
            return
        if filename_lower.endswith(TAR_EXTENSIONS + BZ2_EXTENSIONS + XZ_EXTENSIONS):
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
