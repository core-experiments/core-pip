"""Versioned cache of unpacked wheels for fresh target installations.

The compressed artifact cache avoids downloads. This cache avoids repeating
ZIP extraction and lets supported filesystems clone immutable wheel trees into
an installation target with copy-on-write semantics.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import marshal
import os
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

from cpip.core.errors import InstallationError
from cpip.core.utils import CACHE_INTERPRETER_TAG
from cpip.core.wheel import validate_wheel
from cpip.install.wheel_archive import (
    copy_member_with_metadata,
    validate_member_parts,
    zip_mode,
)

if TYPE_CHECKING:
    from typing import Protocol, TypeVar

    from cpip.core.direct_url import DirectUrl

    class WheelInstallCandidate(Protocol):
        """Read-only candidate boundary required by the archive installer."""

        @property
        def canonical_name(self) -> str: ...

        @property
        def name(self) -> str: ...

        @property
        def path(self) -> str: ...

        @property
        def source_hashes(self) -> dict[str, str] | None: ...

        @property
        def source_kind(self) -> str | None: ...

        @property
        def version(self) -> object: ...

        @property
        def wheel_layout(self) -> object | None: ...

    InstallCandidate = TypeVar("InstallCandidate", bound=WheelInstallCandidate)

    WheelRequest = tuple[str, bool, DirectUrl | None]

else:
    WheelRequest = tuple[str, bool, object | None]


# _extract_archive replaces an entry_root's tree/ and manifest together as
# one atomic unit on any miss (see its os.rename below), so scoping the
# manifest alone to the interpreter -- without also scoping the tree it is
# published alongside -- would make two interpreters sharing a cache_dir
# each invalidate and re-extract the other's tree on every run. Scoping the
# whole bucket keeps each interpreter's cache self-contained instead.
#
# ARCHIVE_CACHE_BUCKET_FAMILY names the pattern shared by every interpreter's
# bucket, so a cache-wide purge can find and remove all of them, not only the
# one the running interpreter would look in.
ARCHIVE_CACHE_BUCKET_FAMILY = "archive-v1"

ARCHIVE_CACHE_BUCKET = f"{ARCHIVE_CACHE_BUCKET_FAMILY}-{CACHE_INTERPRETER_TAG}"

ARCHIVE_CACHE_FORMAT = 1

_LOCK_WAIT_SECONDS = 30.0

_STALE_LOCK_SECONDS = 300.0

INSTALL_WORKERS = 4


# relative archive path, RECORD hash, RECORD size, source mode

ArchiveEntry = tuple[str, str, str, int]


def valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


class CachedWheelArchive:
    __slots__ = ("digest", "dist_info", "entries", "tree")

    def __init__(
        self,
        digest: str,
        tree: str,
        dist_info: str,
        entries: tuple[ArchiveEntry, ...],
    ) -> None:
        self.digest = digest

        self.tree = tree

        self.dist_info = dist_info

        self.entries = entries


def wheel_digest(candidate: WheelInstallCandidate) -> str:
    supplied = (
        (candidate.source_hashes or {}).get("sha256")
        if candidate.source_kind in {None, "wheel"}
        else None
    )

    if isinstance(supplied, str) and valid_sha256(supplied):
        return supplied.lower()

    digest = hashlib.sha256()

    with open(candidate.path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


def archive_entry_root(cache_dir: str, digest: str) -> str:
    return os.path.join(cache_dir, ARCHIVE_CACHE_BUCKET, digest[:2], digest)


def valid_archive_entries(entries: object) -> bool:
    return isinstance(entries, tuple) and all(
        isinstance(item, tuple)
        and len(item) == 4
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        and isinstance(item[2], str)
        and isinstance(item[3], int)
        for item in entries
    )


def load_archive(entry_root: str, digest: str) -> CachedWheelArchive | None:
    tree = os.path.join(entry_root, "tree")

    manifest = os.path.join(entry_root, "manifest.bin")

    if not os.path.isdir(tree) or not os.path.isfile(manifest):
        return None

    try:
        with open(manifest, "rb") as file:
            value = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if not (
        isinstance(value, tuple)
        and len(value) == 4
        and value[0] == ARCHIVE_CACHE_FORMAT
        and value[1] == digest
        and isinstance(value[2], str)
        and isinstance(value[3], tuple)
    ):
        return None

    entries = value[3]

    if not valid_archive_entries(entries):
        return None

    return CachedWheelArchive(digest, tree, value[2], entries)


def _remove_cache_path(path: str) -> None:
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)

        else:
            shutil.rmtree(path)

    except FileNotFoundError:
        pass


@contextmanager
def _entry_lock(path: str, entry_root: str, digest: str) -> Generator[None, None, None]:
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    descriptor: int | None = None

    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        except FileExistsError:
            if load_archive(entry_root, digest) is not None:
                # The caller will recheck after entering the no-op lock scope.

                yield

                return

            try:
                stale = time.time() - os.stat(path, follow_symlinks=False).st_mtime

            except FileNotFoundError:
                continue

            if stale > _STALE_LOCK_SECONDS:
                try:
                    os.unlink(path)

                except FileNotFoundError:
                    pass

                continue

            if time.monotonic() >= deadline:
                raise OSError(errno.EBUSY, "timed out waiting for wheel cache", path)

            time.sleep(0.05)

    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))

        yield

    finally:
        os.close(descriptor)

        try:
            os.unlink(path)

        except FileNotFoundError:
            pass


def _record_metadata(
    archive: zipfile.ZipFile, dist_info: str
) -> dict[str, tuple[str, str]]:
    try:
        text = archive.read(f"{dist_info}/RECORD").decode("utf-8")

    except (KeyError, UnicodeDecodeError):
        return {}

    result: dict[str, tuple[str, str]] = {}

    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[1].startswith("sha256=") and row[2].isdigit():
            result[row[0]] = (row[1], row[2])

    return result


def _extract_archive(
    candidate: WheelInstallCandidate,
    digest: str,
    entry_root: str,
) -> CachedWheelArchive:
    shard = os.path.dirname(entry_root)

    temporary = tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=shard)

    tree = os.path.join(temporary, "tree")

    os.mkdir(tree)

    try:
        with zipfile.ZipFile(candidate.path) as archive:
            layout = candidate.wheel_layout

            if isinstance(layout, tuple) and layout and isinstance(layout[0], str):
                dist_info = layout[0]

            else:
                dist_info = validate_wheel(
                    archive,
                    os.path.basename(candidate.path)[:-4].split("-", 1)[0],
                )

            wheel_metadata = _record_metadata(archive, dist_info)

            entries: list[ArchiveEntry] = []

            seen: set[str] = set()

            for member in archive.infolist():
                if member.is_dir():
                    continue

                parts = validate_member_parts(member.filename)

                if not parts:
                    raise InstallationError(
                        f"wheel member has an empty path: {member.filename!r}",
                    )

                relative = "/".join(parts)

                if relative in seen:
                    raise InstallationError(
                        f"Wheel {candidate.path} contains duplicate member {relative!r}",
                    )

                seen.add(relative)

                destination = os.path.join(tree, *parts)

                os.makedirs(os.path.dirname(destination), exist_ok=True)

                metadata = wheel_metadata.get(relative)

                if metadata is not None and metadata[1] != str(member.file_size):
                    metadata = None

                metadata = copy_member_with_metadata(
                    archive,
                    member,
                    destination,
                    metadata=metadata,
                )

                mode = zip_mode(member)

                if mode is not None:
                    os.chmod(destination, mode)

                entries.append(
                    (relative, metadata[0], metadata[1], mode or 0),
                )

        if f"{dist_info}/RECORD" not in seen:
            raise InstallationError(
                f"Wheel {candidate.path} has no valid dist-info metadata",
            )

        manifest = (
            ARCHIVE_CACHE_FORMAT,
            digest,
            dist_info,
            tuple(entries),
        )

        with open(os.path.join(temporary, "manifest.bin"), "wb") as file:
            marshal.dump(manifest, file)

        _remove_cache_path(entry_root)

        os.rename(temporary, entry_root)

        temporary = ""

        loaded = load_archive(entry_root, digest)

        if loaded is None:
            raise OSError(
                errno.EIO, "failed to publish wheel archive cache", entry_root
            )

        return loaded

    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare_cached_wheel(
    candidate: WheelInstallCandidate,
    cache_dir: str,
) -> CachedWheelArchive:
    layout = candidate.wheel_layout

    if isinstance(layout, CachedWheelArchive):
        return layout

    digest = wheel_digest(candidate)

    entry_root = archive_entry_root(cache_dir, digest)

    cached = load_archive(entry_root, digest)

    if cached is not None:
        return cached

    shard = os.path.dirname(entry_root)

    os.makedirs(shard, exist_ok=True)

    lock = f"{entry_root}.lock"

    with _entry_lock(lock, entry_root, digest):
        cached = load_archive(entry_root, digest)

        if cached is not None:
            return cached

        return _extract_archive(candidate, digest, entry_root)


def prepare_cached_wheels(
    candidates: tuple[WheelInstallCandidate, ...],
    cache_dir: str,
) -> tuple[CachedWheelArchive, ...]:
    if len(candidates) < INSTALL_WORKERS:
        return tuple(
            prepare_cached_wheel(candidate, cache_dir) for candidate in candidates
        )

    with ThreadPoolExecutor(
        max_workers=min(INSTALL_WORKERS, len(candidates)),
        thread_name_prefix="cpip-archive",
    ) as pool:
        return tuple(
            pool.map(
                lambda candidate: prepare_cached_wheel(candidate, cache_dir),
                candidates,
            ),
        )
