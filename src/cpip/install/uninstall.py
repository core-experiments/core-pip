"""Transactional removal of installed distributions."""

from __future__ import annotations

import csv
import importlib.util
import os
import sys
from pathlib import Path, PurePosixPath

from cpip.core.errors import InstallationError
from cpip.install.transaction import InstallTransaction


class DistributionUninstaller:
    """Remove installed distributions through their recorded files."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths = paths

    def uninstall(self, name: str) -> bool:
        return uninstall_distribution(name, paths=self.paths)


def uninstall_distribution(
    name: str,
    *,
    paths: list[str] | None = None,
) -> bool:
    """Remove an installed distribution from its RECORD manifest atomically."""
    from cpip.build.metadata import InstalledDistributionStore

    distribution = InstalledDistributionStore(paths=paths).find(name)
    if distribution is None:
        return False
    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = distribution.read_text("RECORD")
        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot uninstall {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found"
            ) from exc
    else:
        entries = None

    root = os.path.realpath(os.fspath(distribution.location))
    root_path = Path(root)
    recorded_paths: set[Path] = set()
    if entries is not None:
        for row in csv.reader(entries.splitlines()):
            if not row or not row[0]:
                continue
            relative = PurePosixPath(row[0])
            if relative.is_absolute():
                continue
            path_text = os.path.join(root, *relative.parts)
            resolved_text = os.path.realpath(path_text)
            if os.name == "nt" and Path(row[0]).is_absolute():
                continue
            if ".." in relative.parts and os.path.basename(
                os.path.dirname(resolved_text)
            ) not in {"bin", "Scripts"}:
                continue
            if ".." in relative.parts:
                path_text = resolved_text
            path = Path(path_text)
            recorded_paths.add(path)
            if os.path.splitext(path_text)[1] == ".py":
                recorded_paths.update(
                    {
                        Path(importlib.util.cache_from_source(path_text)),
                        Path(f"{path_text}c"),
                        Path(f"{path_text[:-3]}.pyo"),
                    }
                )
    elif distribution.info_location and distribution.info_location.endswith(
        ".egg-info"
    ):
        recorded_paths.add(Path(distribution.info_location))
        egg_link_root = Path(distribution.info_location).parent
        entries = distribution.iter_declared_entries()
        for entry in entries:
            relative = PurePosixPath(entry)
            if relative.is_absolute():
                continue
            path = Path(
                os.path.realpath(
                    os.path.join(os.fspath(egg_link_root), *relative.parts)
                )
            )
            try:
                path.relative_to(root_path)
            except ValueError:
                if path.parent.name not in {"bin", "Scripts"}:
                    continue
            recorded_paths.add(path)
        if not entries:
            try:
                top_level = distribution.read_text("top_level.txt")
            except FileNotFoundError:
                top_level = ""
            for module_name in top_level.splitlines():
                module_name = module_name.strip()
                if module_name and module_name.isidentifier():
                    recorded_paths.update(
                        {root_path / module_name, root_path / f"{module_name}.py"}
                    )
        egg_links = list(egg_link_root.glob("*.egg-link"))
        egg_links.extend(
            egg_link
            for path_entry in sys.path
            for egg_link in Path(path_entry).glob("*.egg-link")
        )
        for egg_link in egg_links:
            if egg_link.stem.casefold() == distribution.raw_name.casefold():
                recorded_paths.add(egg_link)

    existing = {path for path in recorded_paths if os.path.lexists(path)}
    if not existing:
        return False
    transaction = InstallTransaction()
    for path in existing:
        transaction.delete(path)
    transaction.commit()
    return True
