"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from cpip.core.errors import InstallationError

if TYPE_CHECKING:
    from cpip.build.metadata import InstalledMetadataDistribution


def compiled_files(
    stage_root: Path,
    staged: Iterable[tuple[Path, Path, str, int | None]],
) -> list[tuple[Path, Path, str, int | None]]:
    python_files = [
        (source, destination)
        for source, destination, _, _ in staged
        if os.path.splitext(os.fspath(source))[1] == ".py"
    ]
    if not python_files:
        return []

    import compileall
    import importlib.util

    for source, _ in python_files:
        compileall.compile_file(os.fspath(source), force=True, quiet=1)
    result = []
    stage_root_text = os.fspath(stage_root)
    for source, destination in python_files:
        cache_text = importlib.util.cache_from_source(os.fspath(source))
        if os.path.isfile(cache_text):
            relative = os.path.relpath(cache_text, stage_root_text)
            relative_parts = relative.split(os.sep)
            cache = Path(cache_text)
            compiled_destination = destination.parent / Path(*relative_parts[-2:])
            result.append(
                (
                    cache,
                    compiled_destination,
                    os.fspath(compiled_destination),
                    None,
                )
            )
    return result


def existing_paths(
    distribution: InstalledMetadataDistribution | None,
) -> tuple[set[Path], set[Path]]:
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
                "no RECORD file was found"
            ) from exc
    else:
        entries = distribution.iter_declared_entries()
    root = os.fspath(distribution.location)
    paths = {
        Path(os.path.realpath(os.path.join(root, entry))) for entry in entries
    }
    existing = {path for path in paths if os.path.lexists(path)}
    return existing, existing


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
