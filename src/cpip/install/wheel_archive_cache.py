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
import time
from collections.abc import Iterable
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
    import zipfile
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
ARCHIVE_CACHE_BUCKET = f"archive-{CACHE_INTERPRETER_TAG}"

_LOCK_WAIT_SECONDS = 30.0

_STALE_LOCK_SECONDS = 300.0

INSTALL_WORKERS = 4


# relative archive path, RECORD hash, RECORD size, source mode

ArchiveEntry = tuple[str, str, str, int]


_HEX_DIGITS = "0123456789abcdefABCDEF"


def loaded_layout(candidate: WheelInstallCandidate) -> object | None:
    """The candidate's layout if it is already known, without reading the
    wheel; a lazily computed layout reads as not yet known."""
    loaded = getattr(candidate, "wheel_layout_if_loaded", _UNKNOWN)
    return candidate.wheel_layout if loaded is _UNKNOWN else loaded


_UNKNOWN = object()


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not value.strip(_HEX_DIGITS)


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


def supplied_wheel_digest(candidate: WheelInstallCandidate) -> str | None:
    """The SHA-256 the candidate's source vouched for, if any."""
    supplied = (
        (candidate.source_hashes or {}).get("sha256")
        if candidate.source_kind in {None, "wheel"}
        else None
    )

    if isinstance(supplied, str) and valid_sha256(supplied):
        return supplied.lower()

    return None


def prefetch_wheel_digests(
    candidates: Iterable[WheelInstallCandidate],
    cache_dir: str,
) -> None:
    """One database read for the recorded digests of a whole batch."""
    # Deferred: the metadata store (and sqlite3) only for wheels without a
    # supplied hash.
    from cpip.index.metadata_cache import get_wheel_metadata_cache, metadata_identity

    identities = [
        identity
        for candidate in candidates
        if supplied_wheel_digest(candidate) is None
        and (identity := metadata_identity(candidate.path)) is not None
    ]

    if identities:
        get_wheel_metadata_cache(cache_dir).prefetch_digests(identities)


def wheel_digest(candidate: WheelInstallCandidate, cache_dir: str | None = None) -> str:
    """The wheel's SHA-256: as supplied by its source, else as recorded for
    this exact file (path, size, mtime) in the metadata cache, else hashed.

    A wheel from a local wheelhouse carries no index-supplied hash, so every
    install used to read it in full to find its archive entry; the digest is
    now hashed once per file and reused while the file is unchanged.
    """
    supplied = supplied_wheel_digest(candidate)

    if supplied is not None:
        return supplied

    cache = None

    identity = None

    if cache_dir is not None:
        from cpip.index.metadata_cache import (
            get_wheel_metadata_cache,
            metadata_identity,
        )

        identity = metadata_identity(candidate.path)

        if identity is not None:
            cache = get_wheel_metadata_cache(cache_dir)

            recorded = cache.get_digest(identity)

            if recorded is not None:
                return recorded

    digest = hashlib.sha256()

    with open(candidate.path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)

    result = digest.hexdigest()

    if cache is not None and identity is not None:
        cache.put_digest(identity, result)

    return result


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
        and len(value) == 3
        and value[0] == digest
        and isinstance(value[1], str)
        and isinstance(value[2], tuple)
    ):
        return None

    entries = value[2]

    if not valid_archive_entries(entries):
        return None

    return CachedWheelArchive(digest, tree, value[1], entries)


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

    # Deferred: tempfile only when a wheel is actually extracted.
    import tempfile

    temporary = tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=shard)

    tree = os.path.join(temporary, "tree")

    os.mkdir(tree)

    try:
        # Deferred: zipfile only when a wheel is actually extracted.
        import zipfile

        with zipfile.ZipFile(candidate.path) as archive:
            layout = loaded_layout(candidate)

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
    layout = loaded_layout(candidate)

    if isinstance(layout, CachedWheelArchive):
        return layout

    digest = wheel_digest(candidate, cache_dir)

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
    prefetch_wheel_digests(candidates, cache_dir)

    if len(candidates) < INSTALL_WORKERS:
        return tuple(
            prepare_cached_wheel(candidate, cache_dir) for candidate in candidates
        )

    # Deferred: concurrent.futures drags in logging and more, and the pure-wheel
    # fast path imports this module without ever starting a pool.
    from concurrent.futures import ThreadPoolExecutor

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
