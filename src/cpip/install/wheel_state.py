"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

import csv
import os
import stat
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from cpip.core.errors import InstallationError

if TYPE_CHECKING:
    from cpip.build.metadata import InstalledMetadataDistribution
    from cpip.install.target import InstallTarget


def _canonicalize_installed_name(value: str) -> str:
    result: list[str] = []
    separator = False
    for character in value:
        if character in "-_.":
            separator = bool(result)
            continue
        if separator:
            result.append("-")
            separator = False
        result.append(character.lower())
    return "".join(result)


class InstalledWheelDistribution:
    """Minimal installed-wheel metadata used by replacement transactions."""

    __slots__ = (
        "canonical_name",
        "info_location",
        "location",
        "raw_name",
        "raw_version",
        "version",
    )

    def __init__(
        self,
        *,
        location: str,
        info_location: str,
        name: str,
        version: str,
    ) -> None:
        self.location = location
        self.info_location = info_location
        self.raw_name = name
        self.raw_version = version
        self.version = version
        self.canonical_name = _canonicalize_installed_name(name)

    def read_text(self, path: str) -> str:
        with open(os.path.join(self.info_location, path), encoding="utf-8") as file:
            return file.read()


def _wheel_metadata_identity(path: str) -> tuple[str, str] | None:
    name = None
    version = None
    try:
        with open(path, encoding="utf-8") as file:
            for raw_line in file:
                if raw_line in {"\n", "\r\n"}:
                    break
                key, separator, value = raw_line.partition(":")
                if not separator:
                    continue
                key = key.casefold()
                if key == "name" and name is None:
                    name = value.strip()
                elif key == "version" and version is None:
                    version = value.strip()
                if name and version:
                    return name, version
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _metadata_entry_might_match(
    filename: str,
    suffix: str,
    requested: set[str],
) -> bool:
    """Conservatively match an installed metadata filename to requested names."""
    stem = _canonicalize_installed_name(filename[: -len(suffix)])
    return any(stem == name or stem.startswith(f"{name}-") for name in requested)


def discover_installed_wheels(
    paths: Iterable[str],
    *,
    names: set[str] | None = None,
) -> dict[str, InstalledWheelDistribution] | None:
    """Discover ordinary dist-info installs without loading packaging metadata.

    ``None`` requests the authoritative metadata scanner.  It is returned for
    legacy, malformed, or ambiguous layouts rather than guessing about file
    ownership during an upgrade.
    """
    requested = names
    result: dict[str, InstalledWheelDistribution] = {}
    scanned: set[str] = set()
    for value in paths:
        root = os.path.abspath(os.fspath(value))
        if root in scanned:
            continue
        scanned.add(root)
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name.endswith(".dist-info"):
                        suffix = ".dist-info"
                    elif entry.name.endswith(".egg-info"):
                        suffix = ".egg-info"
                    elif entry.name.endswith(".egg-link"):
                        suffix = ".egg-link"
                    else:
                        continue
                    if requested is not None and not _metadata_entry_might_match(
                        entry.name,
                        suffix,
                        requested,
                    ):
                        continue
                    if suffix != ".dist-info":
                        return None
                    if not entry.is_dir(follow_symlinks=False):
                        return None
                    identity = _wheel_metadata_identity(
                        os.path.join(entry.path, "METADATA")
                    )
                    if identity is None:
                        return None
                    name, version = identity
                    canonical_name = _canonicalize_installed_name(name)
                    if requested is not None and canonical_name not in requested:
                        # A matching filename with different metadata is ambiguous.
                        return None
                    if canonical_name in result:
                        return None
                    result[canonical_name] = InstalledWheelDistribution(
                        location=root,
                        info_location=entry.path,
                        name=name,
                        version=version,
                    )
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return result


def compiled_files(
    stage_root: str,
    staged: Iterable[tuple[str, str, str, int | None]],
) -> list[tuple[str, str, str, int | None]]:
    python_files = [
        (source, destination)
        for source, destination, _, _ in staged
        if os.path.splitext(os.fspath(source))[1] == ".py"
    ]
    if not python_files:
        return []

    import compileall
    import importlib.util

    compiled = [
        (source, destination)
        for source, destination in python_files
        if compileall.compile_file(os.fspath(source), force=True, quiet=1)
    ]
    result = []
    stage_root_text = os.fspath(stage_root)
    for source, destination in compiled:
        cache_text = importlib.util.cache_from_source(os.fspath(source))
        relative = os.path.relpath(cache_text, stage_root_text)
        relative_parts = relative.split(os.sep)
        compiled_destination = os.path.join(
            os.path.dirname(destination),
            *relative_parts[-2:],
        )
        result.append(
            (
                cache_text,
                compiled_destination,
                compiled_destination,
                None,
            ),
        )
    return result


def existing_paths(
    distribution: InstalledMetadataDistribution | InstalledWheelDistribution | None,
) -> tuple[set[str], set[str]]:
    if distribution is None:
        return set(), set()
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = [
                row[0]
                for row in csv.reader(distribution.read_text("RECORD").splitlines())
                if row and row[0]
            ]
        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot replace {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found",
            ) from exc
    elif isinstance(distribution, InstalledWheelDistribution):
        raise InstallationError(
            f"Cannot replace {distribution.raw_name} {distribution.version}: "
            "the installed wheel has no dist-info RECORD",
        )
    else:
        entries = distribution.iter_declared_entries()
    root = os.fspath(distribution.location)
    existing: set[str] = set()
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        entries.extend(
            os.path.relpath(
                os.path.join(distribution.info_location, name),
                root,
            )
            for name in ("INSTALLER", "REQUESTED", "direct_url.json", "RECORD")
        )
    for entry in entries:
        path = os.path.join(root, entry)
        try:
            path_stat = os.lstat(path)
        except OSError:
            continue
        existing.add(
            os.path.realpath(path)
            if stat.S_ISLNK(path_stat.st_mode)
            else os.path.abspath(path),
        )
    return existing, existing


class InstalledTargetInventory:
    """Installed distributions discovered once for an install transaction."""

    __slots__ = ("distributions",)

    def __init__(
        self,
        distributions: Mapping[
            str,
            InstalledMetadataDistribution | InstalledWheelDistribution,
        ],
    ) -> None:
        self.distributions = dict(distributions)

    @classmethod
    def from_target(
        cls,
        target: InstallTarget,
        names: set[str] | None = None,
    ) -> InstalledTargetInventory:
        lightweight = discover_installed_wheels(target.library_roots, names=names)
        if lightweight is not None:
            return cls(lightweight)

        from cpip.build.metadata import InstalledDistributionStore

        distributions = InstalledDistributionStore(
            paths=[os.fspath(root) for root in target.library_roots],
        ).iter(names=names)
        return cls(
            {
                distribution.canonical_name: distribution
                for distribution in distributions
            },
        )

    def find(
        self,
        name: str,
    ) -> InstalledMetadataDistribution | InstalledWheelDistribution | None:
        return self.distributions.get(name)


def is_within(path: str, root: str) -> bool:
    try:
        if os.path.commonpath((path, root)) != root:
            raise ValueError
    except (OSError, ValueError):
        return False
    return True
