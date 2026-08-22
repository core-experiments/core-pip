"""Lightweight installed-distribution reader for read-only report commands.

``check``, ``show``, ``inspect``, and ``freeze`` only ever *read* installed
metadata -- they never install, resolve, or modify anything -- but
``core.metadata``'s discovery goes through ``importlib.metadata``, and merely
importing that module costs several milliseconds every time:
``importlib.metadata._adapters`` defines a ``Message`` subclass of
``email.message.Message`` at module-body execution time, so there is no way
to import ``importlib.metadata`` without paying for ``email`` too, regardless
of what you actually use from it.

This module hand-parses dist-info/egg-info directories directly instead,
staying off both ``importlib.metadata`` and ``email``. It deliberately
duplicates a subset of what ``core.metadata``/``build.metadata`` already do
-- the same tradeoff ``cli/fast.py`` already makes for ``list`` -- rather
than touching those modules, which install/uninstall/build/resolution rely
on for correctness. Keep this module's own imports light in turn.
"""

from __future__ import annotations

import os
import re
import sys

from cpip.core.direct_url import DirectUrl
from cpip.core.packaging import canonicalize_name, marker_applies, parse_requirement
from cpip.core.versions import Version
from cpip.core.urls import url_to_path

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from cpip.core.packaging import Requirement

stdlib_pkgs = frozenset({"python", "wsgiref", "argparse"})


class LightMetadata:
    """A minimal, dict-backed stand-in for ``email.message.Message``'s read side."""

    __slots__ = ("_fields", "_payload")

    def __init__(self, fields: dict[str, list[str]], payload: str) -> None:
        self._fields = fields
        self._payload = payload

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self._fields.get(name.lower())
        return values[0] if values else default

    def get_all(self, name: str, default: list[str] | None = None) -> list[str]:
        values = self._fields.get(name.lower())
        if values:
            return list(values)
        return list(default) if default is not None else []

    def get_payload(self) -> str:
        return self._payload


def parse_metadata_text(text: str) -> LightMetadata:
    """Parse RFC 822-style metadata text (METADATA, WHEEL, PKG-INFO)."""
    # Walk the raw text (not str.splitlines()) so the body slice below keeps
    # whatever trailing newline the source had, matching
    # email.message.Message.get_payload() exactly.
    fields: dict[str, list[str]] = {}
    current_key: str | None = None
    position = 0
    text_length = len(text)
    while position < text_length:
        newline = text.find("\n", position)
        line_end = newline if newline != -1 else text_length
        line = text[position:line_end]
        if not line:
            position = line_end + 1
            break
        # A line folded across multiple physical lines (RFC 822 style, used
        # for License/Description headers) continues with leading
        # whitespace; email.message.Message strips that per line and joins
        # with "\n" rather than truly unfolding, so match that exactly.
        if line[0] in " \t" and current_key is not None:
            fields[current_key][-1] += "\n" + line.strip()
        else:
            key, separator, value = line.partition(":")
            current_key = key.strip().lower() if separator else None
            if current_key is not None:
                values = fields.get(current_key)
                if values is None:
                    values = []
                    fields[current_key] = values
                values.append(value.strip())
        position = line_end + 1
    return LightMetadata(fields, text[position:])


def _read_metadata_file(info_location: str) -> LightMetadata | None:
    # The simplest legacy egg-info layout is a single flat PKG-INFO-formatted
    # file (no dist-info/PKG-INFO subfile to look inside).
    if os.path.isfile(info_location):
        try:
            with open(info_location, encoding="utf-8", errors="replace") as file:
                text = file.read()
        except OSError:
            return None
        metadata = parse_metadata_text(text)
        if metadata.get("Name") and metadata.get("Version"):
            return metadata
        return None

    # dist-info uses METADATA; legacy egg-info uses PKG-INFO. Try both, since
    # some setuptools egg-info layouts carry either.
    for filename in ("METADATA", "PKG-INFO"):
        try:
            with open(
                os.path.join(info_location, filename),
                encoding="utf-8",
                errors="replace",
            ) as file:
                text = file.read()
        except OSError:
            continue
        metadata = parse_metadata_text(text)
        if metadata.get("Name") and metadata.get("Version"):
            return metadata
    return None


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


# Mirrors InstalledMetadataDistribution.metadata_dict's field list exactly.
_METADATA_DICT_FIELDS = {
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


class LightDistribution:
    """An importlib.metadata-free stand-in for ``InstalledMetadataDistribution``."""

    __slots__ = (
        "_direct_url",
        "_direct_url_read",
        "_installer",
        "canonical_name",
        "info_location",
        "location",
        "metadata",
        "raw_name",
        "raw_version",
        "user_site",
    )

    def __init__(
        self,
        *,
        raw_name: str,
        raw_version: str,
        location: str,
        info_location: str,
        metadata: LightMetadata,
        user_site: str | None,
    ) -> None:
        self.raw_name = raw_name
        self.raw_version = raw_version
        self.canonical_name = canonicalize_name(raw_name)
        self.location = location
        self.info_location = info_location
        self.metadata = metadata
        self.user_site = user_site
        # INSTALLER and direct_url.json are read once: list, freeze and
        # inspect each ask for them more than once per distribution.
        self._installer: str | None = None
        self._direct_url: DirectUrl | None = None
        self._direct_url_read = False

    @property
    def version(self) -> Version:
        return Version(self.raw_version)

    @property
    def metadata_version(self) -> str | None:
        return self.metadata.get("Metadata-Version")

    @property
    def metadata_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for field, multiple in _METADATA_DICT_FIELDS.items():
            values = self.metadata.get_all(field.title())
            if values:
                result[field.replace("-", "_")] = values if multiple else values[0]
        payload = self.metadata.get_payload()
        if payload:
            result["description"] = payload
        return result

    @property
    def installed_with_dist_info(self) -> bool:
        return self.info_location.endswith(".dist-info")

    @property
    def installed_with_setuptools_egg_info(self) -> bool:
        return self.info_location.endswith(".egg-info")

    @property
    def installer(self) -> str:
        if self._installer is None:
            try:
                self._installer = next(
                    line.strip()
                    for line in self.read_text("INSTALLER").splitlines()
                    if line.strip()
                )
            except (FileNotFoundError, StopIteration):
                self._installer = ""
        return self._installer

    @property
    def requested(self) -> bool:
        try:
            self.read_text("REQUESTED")
        except FileNotFoundError:
            return False
        return True

    @property
    def direct_url(self) -> DirectUrl | None:
        if not self._direct_url_read:
            self._direct_url_read = True
            try:
                self._direct_url = DirectUrl.from_json(
                    self.read_text("direct_url.json")
                )
            except (FileNotFoundError, ValueError):
                self._direct_url = None
        return self._direct_url

    @property
    def editable_project_location(self) -> str | None:
        direct_url = self.direct_url
        if direct_url and direct_url.is_local_editable():
            return url_to_path(direct_url.url)

        if self.installed_with_setuptools_egg_info:
            egg_link_root = os.path.dirname(self.info_location)
            try:
                with os.scandir(egg_link_root) as entries:
                    egg_link = next(
                        (
                            entry.path
                            for entry in entries
                            if entry.name.endswith(".egg-link")
                        ),
                        None,
                    )
            except OSError:
                egg_link = None

            if egg_link is not None:
                with open(egg_link, encoding="utf-8") as file:
                    lines = file.read().splitlines()
                if lines:
                    return lines[0]

            egg_link = egg_link_path_from_sys_path(self.raw_name)
            if egg_link is not None:
                with open(egg_link, encoding="utf-8") as file:
                    lines = file.read().splitlines()
                if lines:
                    return lines[0]

        return None

    @property
    def editable(self) -> bool:
        return self.editable_project_location is not None

    @property
    def local(self) -> bool:
        return self.location.startswith(sys.prefix)

    @property
    def in_usersite(self) -> bool:
        return self.user_site is not None and self.location.startswith(self.user_site)

    def iter_dependencies(self, extras: tuple[str, ...] = ()) -> list[Requirement]:
        result: list[Requirement] = []
        for value in self.metadata.get_all("Requires-Dist"):
            requirement = parse_requirement(value)
            if marker_applies(requirement.marker, extras=extras):
                result.append(requirement)
        return result

    def iter_raw_dependencies(self) -> list[str]:
        return self.metadata.get_all("Requires-Dist")

    def read_text(self, path: str) -> str:
        try:
            with open(os.path.join(self.info_location, path), encoding="utf-8") as file:
                return file.read()
        except OSError:
            raise FileNotFoundError(path) from None

    def iter_declared_entries(self) -> list[str]:
        if self.installed_with_setuptools_egg_info:
            try:
                return self.read_text("installed-files.txt").splitlines()
            except FileNotFoundError:
                return []

        try:
            record_text = self.read_text("RECORD")
        except FileNotFoundError:
            return []

        # RECORD is a CSV of path,hash,size -- only reached by `show --files`,
        # so keep the csv module off every other invocation of these commands.
        import csv
        import io

        return sorted(row[0] for row in csv.reader(io.StringIO(record_text)) if row)


def _iter_root_distributions(
    root: str,
    user_site: str | None,
) -> Iterator[LightDistribution]:
    try:
        with os.scandir(root or os.curdir) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if (entry.name.endswith(".dist-info") and entry.is_dir())
                # Legacy egg-info can be a directory (with PKG-INFO inside)
                # or, for the simplest packages, a single flat metadata file.
                or (
                    entry.name.endswith(".egg-info")
                    and (entry.is_dir() or entry.is_file())
                )
            )
    except OSError:
        return

    for name in names:
        info_location = os.path.join(root or os.curdir, name)
        metadata = _read_metadata_file(info_location)
        if metadata is None:
            continue
        raw_name = metadata.get("Name")
        raw_version = metadata.get("Version")
        if not raw_name or not raw_version:
            continue
        yield LightDistribution(
            raw_name=raw_name,
            raw_version=raw_version,
            location=root,
            info_location=info_location,
            metadata=metadata,
            user_site=user_site,
        )


class LightDistributionStore:
    """Discover and query installed distribution metadata without importlib.metadata."""

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
        names: Collection[str] | None = None,
    ) -> list[LightDistribution]:
        canonical_names = (
            {canonicalize_name(name) for name in names} if names is not None else None
        )
        roots = self.paths if self.paths is not None else sys.path

        result: list[LightDistribution] = []
        seen: set[str] = set()
        for root in roots:
            for dist in _iter_root_distributions(root, self.user_site):
                if dist.canonical_name in seen:
                    continue
                if (
                    canonical_names is not None
                    and dist.canonical_name not in canonical_names
                ):
                    continue
                if local_only and not dist.local:
                    continue
                if user_only and not dist.in_usersite:
                    continue
                if editables_only and not dist.editable:
                    continue
                if not include_editables and dist.editable:
                    continue
                if skip is not None and dist.canonical_name in skip:
                    continue
                seen.add(dist.canonical_name)
                result.append(dist)
        return result
