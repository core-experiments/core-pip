"""Transactional removal of installed distributions."""

from __future__ import annotations

import csv
import importlib.util
import ntpath
import os
import sys

from cpip.build.metadata import InstalledDistributionStore
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

    distribution = InstalledDistributionStore(paths=paths).find(name)

    if distribution is None:
        return False

    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = distribution.read_text("RECORD")

        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot uninstall {distribution.raw_name} {distribution.version}: "
                "no RECORD file was found",
            ) from exc

    else:
        entries = None

    root = os.path.realpath(os.fspath(distribution.location))

    recorded_paths: set[str] = set()

    if entries is not None:
        for row in csv.reader(entries.splitlines()):
            if not row or not row[0]:
                continue

            raw_relative = row[0]

            if raw_relative.startswith("/") or (
                os.name == "nt" and ntpath.isabs(raw_relative)
            ):
                continue

            relative_parts = tuple(
                part for part in raw_relative.split("/") if part and part != "."
            )

            if not relative_parts:
                continue

            path_text = os.path.join(root, *relative_parts)

            if ".." in relative_parts:
                resolved_text = os.path.realpath(path_text)

            else:
                resolved_text = path_text

            if ".." in relative_parts and os.path.basename(
                os.path.dirname(resolved_text),
            ) not in {"bin", "Scripts"}:
                continue

            if ".." in relative_parts:
                path_text = resolved_text

            recorded_paths.add(path_text)

            if os.path.splitext(path_text)[1] == ".py":
                recorded_paths.update(
                    {
                        importlib.util.cache_from_source(path_text),
                        f"{path_text}c",
                        f"{path_text[:-3]}.pyo",
                    },
                )

    elif distribution.info_location and distribution.info_location.endswith(
        ".egg-info",
    ):
        recorded_paths.add(distribution.info_location)

        egg_link_root = os.path.dirname(distribution.info_location)

        entries = distribution.iter_declared_entries()

        for entry in entries:
            if entry.startswith("/") or (os.name == "nt" and ntpath.isabs(entry)):
                continue

            relative_parts = tuple(
                part for part in entry.split("/") if part and part != "."
            )

            path = os.path.realpath(os.path.join(egg_link_root, *relative_parts))

            try:
                if os.path.commonpath((path, root)) != root:
                    raise ValueError

            except (OSError, ValueError):
                if os.path.basename(os.path.dirname(path)) not in {"bin", "Scripts"}:
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
                        {
                            os.path.join(root, module_name),
                            os.path.join(root, f"{module_name}.py"),
                        },
                    )

        egg_links: list[str] = []

        for path_entry in (egg_link_root, *sys.path):
            try:
                with os.scandir(path_entry) as children:
                    egg_links.extend(
                        entry.path
                        for entry in children
                        if entry.name.endswith(".egg-link")
                    )

            except OSError:
                continue

        for egg_link in egg_links:
            if os.path.splitext(os.path.basename(egg_link))[0].casefold() == (
                distribution.raw_name.casefold()
            ):
                recorded_paths.add(egg_link)

    existing = {path for path in recorded_paths if os.path.lexists(path)}

    if not existing:
        return False

    transaction = InstallTransaction()

    for path in existing:
        transaction.delete(path)

    transaction.commit()

    return True
