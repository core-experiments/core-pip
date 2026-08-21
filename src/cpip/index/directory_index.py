"""Cached indexing of local package source directories."""

from __future__ import annotations

import os
import stat
from typing import NamedTuple

from cpip.core.packaging import canonicalize_name
from cpip.core.versions import Version
from cpip.index.links import SOURCE_ARCHIVE_SUFFIXES


class LocalSourceEntry(NamedTuple):
    path: str
    identity: str
    stat_identity: tuple[int, int, int]


class LocalSourceSnapshot:
    """A single discovery view of a local package source directory.

    The identity is captured while the directory entry is already being
    inspected.  Consumers can carry it through candidate discovery and avoid
    issuing another stat for the same artifact later in resolution.
    """

    __slots__ = ("entries", "is_directory", "path")

    def __init__(
        self,
        path: str,
        entries: tuple[LocalSourceEntry, ...],
        *,
        is_directory: bool = True,
    ) -> None:
        self.path = path
        self.entries = entries
        self.is_directory = is_directory


def local_source_snapshot(
    path: str,
    *,
    suffixes: tuple[str, ...] | None = None,
) -> LocalSourceSnapshot | None:
    """Return one stable local-source snapshot, or ``None`` if not a directory."""
    path_text = os.fspath(path)
    normalized_suffixes = (
        tuple(suffix.lower() for suffix in suffixes) if suffixes is not None else None
    )
    try:
        entries = os.scandir(path_text)
    except NotADirectoryError:
        return LocalSourceSnapshot(path_text, (), is_directory=False)
    except OSError:
        return None
    discovered: list[LocalSourceEntry] = []
    with entries:
        for entry in entries:
            if normalized_suffixes is not None and not entry.name.lower().endswith(
                normalized_suffixes
            ):
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            identity = (
                f"stat:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
            )
            discovered.append(
                LocalSourceEntry(
                    entry.path,
                    identity,
                    (info.st_ino, info.st_size, info.st_mtime_ns),
                ),
            )
    return LocalSourceSnapshot(path_text, tuple(discovered))


def project_version_from_filename(filename: str) -> tuple[str, Version] | None:
    stem = filename
    for suffix in SOURCE_ARCHIVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        return None
    name, sep, version = stem.rpartition("-")
    if not sep or not name or not version:
        return None
    try:
        return canonicalize_name(name), Version(version)
    except ValueError:
        return None
