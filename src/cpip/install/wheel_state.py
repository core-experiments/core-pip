"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

import csv
import os
import stat
from collections.abc import Iterable
from typing import TYPE_CHECKING

from cpip.core.errors import InstallationError

if TYPE_CHECKING:
    from cpip.build.metadata import InstalledMetadataDistribution
    from cpip.install.target import InstallTarget


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
    distribution: InstalledMetadataDistribution | None,
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
    else:
        entries = distribution.iter_declared_entries()
    root = os.fspath(distribution.location)
    existing: set[str] = set()
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
        distributions: dict[str, InstalledMetadataDistribution],
    ) -> None:
        self.distributions = distributions

    @classmethod
    def from_target(
        cls,
        target: InstallTarget,
        names: set[str] | None = None,
    ) -> InstalledTargetInventory:
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

    def find(self, name: str) -> InstalledMetadataDistribution | None:
        return self.distributions.get(name)


def is_within(path: str, root: str) -> bool:
    try:
        if os.path.commonpath((path, root)) != root:
            raise ValueError
    except (OSError, ValueError):
        return False
    return True
