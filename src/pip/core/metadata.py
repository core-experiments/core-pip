from __future__ import annotations

import importlib.metadata
import os
import sysconfig
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Iterable, cast

from .packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)

stdlib_pkgs = {"python", "wsgiref", "argparse"}


@dataclass(frozen=True)
class InstalledDistribution:
    name: str
    version: str
    location: Path
    metadata_location: Path | None
    raw: importlib.metadata.Distribution

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    @property
    def metadata(self) -> Message | importlib.metadata.PackageMetadata:
        return self.raw.metadata

    def dependencies(self, extras: Iterable[str] = ()) -> list[Requirement]:
        result: list[Requirement] = []
        for value in self.raw.metadata.get_all("Requires-Dist", []):
            req = parse_requirement(value)
            if marker_applies(req.marker, extras=extras):
                result.append(req)
        return result

    def read_text(self, name: str) -> str:
        text = self.raw.read_text(name)
        if text is None:
            raise FileNotFoundError(name)
        return text

    def files(self) -> list[str]:
        files = self.raw.files or ()
        return sorted(str(file) for file in files)


def default_lib_path() -> Path:
    return Path(sysconfig.get_paths()["purelib"])


def default_scripts_path() -> Path:
    return Path(sysconfig.get_paths()["scripts"])


def user_lib_path() -> Path:
    import site

    return Path(site.getusersitepackages())


def user_scripts_path() -> Path:
    return Path(sysconfig.get_path("scripts", f"{os.name}_user"))


def iter_installed_distributions(
    paths: Iterable[str] | None = None,
) -> list[InstalledDistribution]:
    result: list[InstalledDistribution] = []
    if paths is None:
        distributions = importlib.metadata.distributions()
    else:
        distribution_paths = [str(Path(path)) for path in paths]
        distributions = importlib.metadata.distributions(path=distribution_paths)
    for dist in distributions:
        metadata = cast(Any, dist.metadata)
        name = metadata.get("Name")
        version = dist.version
        if not name or not version:
            continue
        metadata_location = getattr(dist, "_path", None)
        location = Path(str(dist.locate_file("")))
        if metadata_location is None or str(location) == "<memory>":
            continue
        result.append(
            InstalledDistribution(
                name=name,
                # Keep the metadata spelling intact.  Installed distributions
                # may contain legacy versions that are not PEP 440 versions;
                # presentation commands must still be able to inspect and
                # remove them.
                version=str(version),
                location=location,
                metadata_location=Path(metadata_location)
                if metadata_location
                else None,
                raw=dist,
            )
        )
    return sorted(result, key=lambda dist: dist.canonical_name)


def find_installed(
    name: str, paths: Iterable[str] | None = None
) -> InstalledDistribution | None:
    canonical = canonicalize_name(name)
    for dist in iter_installed_distributions(paths):
        if dist.canonical_name == canonical:
            return dist
    return None
