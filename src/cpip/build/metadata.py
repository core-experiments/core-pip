"""Metadata loading boundary used by build-owned distribution lifecycles."""

from __future__ import annotations

import email.message
import email.parser
import configparser
import os
import re
import sys
import zipfile
from collections.abc import Collection
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from cpip.core.packaging import (
    Requirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.metadata import (
    InstalledDistribution,
    find_installed,
    iter_installed_distributions,
)
from cpip.core.direct_url import DirectUrl
from cpip.core.urls import url_to_path
from cpip.core.wheel import parse_wheel, read_wheel_metadata_file


def egg_link_names(raw_name: str) -> list[str]:
    """Return the filename variants used by setuptools for an egg-link."""
    return [
        re.sub("[^A-Za-z0-9.]+", "-", raw_name) + ".egg-link",
        f"{raw_name}.egg-link",
    ]


def egg_link_path_from_sys_path(raw_name: str) -> str | None:
    """Find an egg-link for ``raw_name`` by walking the interpreter path."""
    for path_item in sys.path:
        for egg_link_name in egg_link_names(raw_name):
            egg_link = os.path.join(path_item, egg_link_name)
            if os.path.isfile(egg_link):
                return egg_link
    return None


def parse_entry_points(text: str | None) -> list[SimpleNamespace]:
    if not text:
        return []
    parser = configparser.ConfigParser(delimiters=("=",), strict=False)
    parser.read_string(text)
    return [
        SimpleNamespace(name=name, value=value, group=group)
        for group in parser.sections()
        for name, value in parser.items(group)
    ]


class MetadataDistribution:
    """Metadata view backed by a dist-info directory or wheel archive."""

    def __init__(
        self,
        metadata: email.message.Message,
        *,
        location: str | None,
        info_location: str | None,
        entry_points_text: str | None = None,
    ) -> None:
        self.metadata = metadata
        self.location_internal = location
        self.info_location_internal = info_location
        self.entry_points_text_internal = entry_points_text

    @property
    def metadata_version(self) -> str | None:
        return self.metadata.get("Metadata-Version")

    @classmethod
    def from_directory(
        cls,
        directory: str,
    ) -> MetadataDistribution:
        path = Path(directory)
        metadata = email.parser.Parser().parsestr(
            (path / "METADATA").read_text(encoding="utf-8")
        )
        return cls(
            metadata,
            location=str(path.parent),
            info_location=str(path),
            entry_points_text=(
                (path / "entry_points.txt").read_text(encoding="utf-8")
                if (path / "entry_points.txt").exists()
                else None
            ),
        )

    @classmethod
    def from_wheel(
        cls,
        path: str,
        name: str,
    ) -> MetadataDistribution:
        with zipfile.ZipFile(path, allowZip64=True) as archive:
            return cls.from_wheel_archive(archive, name, path)

    @classmethod
    def from_wheel_archive(
        cls,
        archive: zipfile.ZipFile,
        name: str,
        location: str,
    ) -> MetadataDistribution:
        info_dir, _ = parse_wheel(archive, name)
        contents = read_wheel_metadata_file(archive, f"{info_dir}/METADATA")
        metadata = email.parser.BytesParser().parsebytes(contents)
        return cls(
            metadata,
            location=location,
            info_location=f"{location}/{info_dir}",
            entry_points_text=(
                read_wheel_metadata_file(
                    archive, f"{info_dir}/entry_points.txt"
                ).decode()
                if f"{info_dir}/entry_points.txt" in archive.namelist()
                else None
            ),
        )

    @classmethod
    def from_metadata_file_contents(
        cls,
        contents: bytes,
        project_name: str,
    ) -> MetadataDistribution:
        metadata = email.parser.BytesParser().parsebytes(contents)
        if metadata.get("Name") is None:
            metadata["Name"] = project_name
        return cls(metadata, location=None, info_location=None)

    @property
    def location(self) -> str | None:
        return self.location_internal

    @property
    def info_location(self) -> str | None:
        return self.info_location_internal

    @property
    def raw_name(self) -> str:
        return str(self.metadata.get("Name", ""))

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.raw_name)

    @property
    def raw_version(self) -> str:
        return str(self.metadata.get("Version", ""))

    @property
    def version(self) -> Version:
        return Version(self.raw_version)

    @property
    def requires_python(self) -> SpecifierSet:
        return SpecifierSet(str(self.metadata.get("Requires-Python", "")))

    def iter_dependencies(self, extras: tuple[str, ...] = ()) -> list[Requirement]:
        dependencies: list[Requirement] = []
        for value in self.metadata.get_all("Requires-Dist", []):
            requirement = parse_requirement(value)
            if marker_applies(requirement.marker, extras=extras):
                dependencies.append(requirement)
        return dependencies

    def iter_raw_dependencies(self) -> list[str]:
        return self.metadata.get_all("Requires-Dist", [])

    def iter_entry_points(self) -> list[SimpleNamespace]:
        return parse_entry_points(self.entry_points_text_internal)

    def iter_provided_extras(self) -> list[str]:
        return [
            canonicalize_name(value)
            for value in self.metadata.get_all("Provides-Extra", [])
            if value.strip()
        ]

    def read_text(self, path: str) -> str:
        info_location = self.info_location_internal
        if info_location is None or self.location_internal == info_location:
            raise FileNotFoundError(path)
        target = Path(info_location) / path
        return target.read_text(encoding="utf-8")


class InstalledMetadataDistribution:
    """Metadata view for a distribution discovered in the running environment."""

    def __init__(
        self,
        distribution: InstalledDistribution,
        *,
        user_site: str | None = None,
    ) -> None:
        self.distribution_internal = distribution
        self.user_site_internal = user_site

    @property
    def location(self) -> str:
        return str(self.distribution_internal.location)

    @property
    def installed_location(self) -> str:
        return self.location

    @property
    def info_location(self) -> str | None:
        location = self.distribution_internal.metadata_location
        return str(location) if location is not None else None

    @property
    def canonical_name(self) -> str:
        return self.distribution_internal.canonical_name

    @property
    def raw_name(self) -> str:
        return self.distribution_internal.name

    @property
    def raw_version(self) -> str:
        return self.distribution_internal.version

    @property
    def version(self) -> Version:
        return Version(self.raw_version)

    @property
    def metadata(self) -> email.message.Message:
        return cast(email.message.Message, self.distribution_internal.raw.metadata)

    @property
    def metadata_dict(self) -> dict[str, object]:
        fields = {
            "metadata-version": False,
            "name": False,
            "version": False,
            "summary": False,
            "home-page": False,
            "author": False,
            "author-email": False,
            "license": False,
            "license-expression": False,
            "requires-python": False,
            "description-content-type": False,
            "dynamic": True,
            "platform": True,
            "supported-platform": True,
            "download-url": False,
            "maintainer": False,
            "maintainer-email": False,
            "license-file": True,
            "classifier": True,
            "requires-dist": True,
            "requires-external": True,
            "project-url": True,
            "provides-extra": True,
            "provides-dist": True,
            "obsoletes-dist": True,
        }
        result: dict[str, object] = {}
        for field, multiple in fields.items():
            header = field.title()
            values = self.metadata.get_all(header)
            if values:
                result[field.replace("-", "_")] = values if multiple else values[0]
        payload = self.metadata.get_payload()
        if isinstance(payload, str) and payload:
            result["description"] = payload
        return result

    @property
    def metadata_version(self) -> str | None:
        return self.metadata.get("Metadata-Version")

    @property
    def installer(self) -> str:
        try:
            return next(
                line.strip()
                for line in self.read_text("INSTALLER").splitlines()
                if line.strip()
            )
        except (FileNotFoundError, StopIteration):
            return ""

    @property
    def requested(self) -> bool:
        try:
            self.read_text("REQUESTED")
        except FileNotFoundError:
            return False
        return True

    @property
    def installed_with_dist_info(self) -> bool:
        return bool(self.info_location and self.info_location.endswith(".dist-info"))

    @property
    def metadata_location(self) -> str | None:
        return self.info_location

    @property
    def installed_with_setuptools_egg_info(self) -> bool:
        return bool(self.info_location and self.info_location.endswith(".egg-info"))

    @property
    def setuptools_filename(self) -> str:
        return self.raw_name

    @property
    def installed_by_distutils(self) -> bool:
        return False

    @property
    def installed_as_egg(self) -> bool:
        return False

    @property
    def direct_url(self) -> DirectUrl | None:
        try:
            return DirectUrl.from_json(self.read_text("direct_url.json"))
        except (FileNotFoundError, ValueError):
            return None

    @property
    def editable_project_location(self) -> str | None:
        direct_url = self.direct_url
        if direct_url and direct_url.is_local_editable():
            return url_to_path(direct_url.url)
        if self.info_location and self.info_location.endswith(".egg-info"):
            egg_links = Path(self.info_location).parent.glob("*.egg-link")
            egg_link = next(egg_links, None)
            if egg_link is not None:
                lines = egg_link.read_text(encoding="utf-8").splitlines()
                if lines:
                    return lines[0]
            egg_link = egg_link_path_from_sys_path(self.raw_name)
            if egg_link is not None:
                lines = Path(egg_link).read_text(encoding="utf-8").splitlines()
                if lines:
                    return lines[0]
        return None

    @property
    def local(self) -> bool:
        return self.location.startswith(sys.prefix)

    @property
    def requires_python(self) -> SpecifierSet:
        return SpecifierSet(str(self.metadata.get("Requires-Python", "")))

    @property
    def editable(self) -> bool:
        return self.editable_project_location is not None

    @property
    def in_usersite(self) -> bool:
        return self.user_site_internal is not None and self.location.startswith(
            self.user_site_internal
        )

    @property
    def in_site_packages(self) -> bool:
        return True

    def iter_dependencies(self, extras: tuple[str, ...] = ()) -> list[Requirement]:
        return self.distribution_internal.dependencies(extras)

    def iter_raw_dependencies(self) -> list[str]:
        return self.metadata.get_all("Requires-Dist", [])

    def iter_provided_extras(self) -> list[str]:
        return [
            canonicalize_name(value)
            for value in self.metadata.get_all("Provides-Extra", [])
            if value.strip()
        ]

    def read_text(self, path: str) -> str:
        return self.distribution_internal.read_text(path)

    def iter_declared_entries(self) -> list[str]:
        if self.info_location and self.info_location.endswith(".egg-info"):
            try:
                return [
                    line for line in self.read_text("installed-files.txt").splitlines()
                ]
            except FileNotFoundError:
                return []
        return self.distribution_internal.files()

    def iter_distutils_script_names(self) -> list[str]:
        return []

    def iter_entry_points(self) -> list[SimpleNamespace]:
        try:
            entry_points = self.read_text("entry_points.txt")
        except FileNotFoundError:
            return []
        return parse_entry_points(entry_points)

    def is_file(self, path: str) -> bool:
        try:
            self.read_text(path)
        except FileNotFoundError:
            return False
        return True


class InstalledDistributionStore:
    """Discover and query installed distribution metadata."""

    def __init__(
        self,
        *,
        paths: list[str] | None = None,
        user_site: str | None = None,
    ) -> None:
        self.paths = paths
        self.user_site = user_site

    def iter(
        self,
        *,
        local_only: bool = False,
        user_only: bool = False,
        editables_only: bool = False,
        include_editables: bool = True,
        skip: Collection[str] | None = None,
    ) -> list[InstalledMetadataDistribution]:
        result: list[InstalledMetadataDistribution] = []
        for distribution in iter_installed_distributions(self.paths):
            view = InstalledMetadataDistribution(
                distribution,
                user_site=self.user_site,
            )
            if local_only and not view.local:
                continue
            if user_only and not view.in_usersite:
                continue
            if editables_only and not view.editable:
                continue
            if not include_editables and view.editable:
                continue
            if skip is not None and view.canonical_name in skip:
                continue
            result.append(view)
        return result

    def find(self, name: str) -> InstalledMetadataDistribution | None:
        if self.paths is not None and self.user_site is None:
            distribution = find_installed(name, self.paths)
            return (
                InstalledMetadataDistribution(distribution, user_site=self.user_site)
                if distribution is not None
                else None
            )
        canonical = canonicalize_name(name)
        distributions = [
            distribution
            for distribution in self.iter()
            if distribution.canonical_name == canonical
        ]
        return next(
            (
                distribution
                for distribution in distributions
                if distribution.in_usersite
            ),
            next(iter(distributions), None),
        )
