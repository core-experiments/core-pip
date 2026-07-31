"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

import compileall
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
    result = []
    for source, destination, _ in staged:
        if source.suffix != ".py":
            continue
        if not compileall.compile_file(os.fspath(source), force=True, quiet=True):
            continue
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
    entries = distribution.iter_declared_entries()
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            distribution.read_text("RECORD")
        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot replace {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found"
            ) from exc
    paths = {
        (Path(distribution.location) / entry).resolve(strict=False) for entry in entries
    }
    existing = {path for path in paths if path.exists() or path.is_symlink()}
    return existing, existing


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
