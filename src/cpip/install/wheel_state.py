"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

import compileall
import csv
import importlib.util
import os
from pathlib import Path
from typing import Iterable

from cpip.build.metadata import InstalledMetadataDistribution
from cpip.core.errors import InstallationError


def compiled_files(
    stage_root: Path,
    staged: Iterable[tuple[Path, Path, int | None]],
) -> list[tuple[Path, Path, int | None]]:
    python_files = [
        (source, destination)
        for source, destination, _ in staged
        if source.suffix == ".py"
    ]
    if not python_files:
        return []

    compileall.compile_dir(os.fspath(stage_root), force=True, quiet=1)
    result = []
    for source, destination in python_files:
        cache = Path(importlib.util.cache_from_source(os.fspath(source)))
        if cache.is_file():
            relative = cache.relative_to(stage_root)
            result.append(
                (cache, destination.parent / Path(*relative.parts[-2:]), None)
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
    paths = {
        (Path(distribution.location) / entry).resolve(strict=False) for entry in entries
    }
    existing = {path for path in paths if os.path.lexists(path)}
    return existing, existing


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
