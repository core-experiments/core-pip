"""Cached indexing of local package source directories."""

from __future__ import annotations

import os
from typing import NamedTuple

from cpip.core.packaging import canonicalize_name
from cpip.core.versions import Version
from cpip.index.links import SOURCE_ARCHIVE_SUFFIXES


class LocalSourceEntry(NamedTuple):
    path: str
    # The stat-based identity is computed on first use by the Link built
    # from the entry (Link.artifact_kind / candidate_metadata_fingerprint),
    # not while scanning: a wheelhouse directory holds thousands of entries
    # and a resolve touches the metadata of a fraction of them, so a stat
    # per entry here was mostly wasted syscalls.
    identity: str | None
    stat_identity: tuple[int, int, int] | None


class LocalSourceSnapshot:
    """A single discovery view of a local package source directory.

    Entries carry no stat identity: the Link built from an entry computes
    one on first use, so only artifacts whose metadata is actually read pay
    a stat.
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
            # is_file() follows symlinks like stat() did and answers from the
            # directory entry's type without a syscall on the common
            # filesystems; only a symlink costs a stat.
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            discovered.append(LocalSourceEntry(entry.path, None, None))
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
