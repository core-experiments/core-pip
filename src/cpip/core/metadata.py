from __future__ import annotations

import importlib.metadata
import os
import site
import sysconfig
from collections.abc import Collection, Iterable

from .packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from .wheel_metadata import parse_metadata_headers

TYPE_CHECKING = False

if TYPE_CHECKING:
    from email.message import Message

stdlib_pkgs = {"python", "wsgiref", "argparse"}


def _read_raw_metadata_text(
    raw: importlib.metadata.Distribution,
) -> str | None:
    """Read the metadata file text through the same fallback chain as
    ``importlib.metadata.Distribution.metadata``: ``METADATA``, then
    ``PKG-INFO`` for sdist-built distributions, then the bare dist-info path
    for old egg-info installs that have neither.
    """

    return raw.read_text("METADATA") or raw.read_text("PKG-INFO") or raw.read_text("")


class InstalledDistribution:
    __slots__ = (
        "_fast_headers",
        "location",
        "metadata_location",
        "name",
        "raw",
        "version",
    )

    def __init__(
        self,
        name: str,
        version: str,
        location: str,
        metadata_location: str | None,
        raw: importlib.metadata.Distribution,
    ) -> None:
        self.name = name

        self.version = version

        self.location = location

        self.metadata_location = metadata_location

        self.raw = raw

        self._fast_headers: dict[str, list[str]] | None = None

    name: str

    version: str

    location: str

    metadata_location: str | None

    raw: importlib.metadata.Distribution

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    @property
    def metadata(self) -> Message | importlib.metadata.PackageMetadata:
        return self.raw.metadata

    def _fast_metadata_headers(self) -> dict[str, list[str]]:
        """Read Requires-Dist/Name/Version through the wheel-metadata fast path.

        ``self.raw.metadata`` parses the file through the full RFC822 email
        machinery the first time it's touched -- expensive, and paid by every
        already-installed distribution ``dependencies()`` inspects during
        resolution. ``parse_metadata_headers`` already does this reliably for
        candidate wheels pulled from PyPI; installed distributions use the
        identical METADATA format, so it's just as trustworthy here.
        """

        headers = self._fast_headers

        if headers is None:
            headers = parse_metadata_headers(_read_raw_metadata_text(self.raw) or "")

            self._fast_headers = headers

        return headers

    def dependencies(self, extras: Iterable[str] = ()) -> list[Requirement]:
        result: list[Requirement] = []

        for value in self._fast_metadata_headers().get("requires-dist", []):
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


def default_lib_path() -> str:
    return sysconfig.get_paths()["purelib"]


def user_lib_path() -> str:
    return site.getusersitepackages()


def _iter_installed_distributions(
    paths: Iterable[str] | None = None,
    names: Collection[str] | None = None,
) -> Iterable[InstalledDistribution]:
    canonical_names = (
        {canonicalize_name(name) for name in names} if names is not None else None
    )

    if paths is None:
        distributions = importlib.metadata.distributions()

    else:
        distribution_paths = [os.fspath(path) for path in paths]

        distributions = importlib.metadata.distributions(path=distribution_paths)

    for dist in distributions:
        text = _read_raw_metadata_text(dist)

        if text is None:
            continue

        # Reading through parse_metadata_headers instead of dist.metadata
        # avoids the full RFC822 email-parser cost for every installed
        # distribution -- paid on every default (no --ignore-installed)
        # resolve, for every package already in the environment, just to
        # learn its name.
        headers = parse_metadata_headers(text)

        name = headers.get("name", [None])[0]

        version = headers.get("version", [None])[0]

        if not name or not version:
            continue

        if (
            canonical_names is not None
            and canonicalize_name(name) not in canonical_names
        ):
            continue

        metadata_location = getattr(dist, "_path", None)

        location = str(dist.locate_file(""))

        if metadata_location is None or str(location) == "<memory>":
            continue

        distribution = InstalledDistribution(
            name=name,
            # Keep the metadata spelling intact.  Installed distributions
            # may contain legacy versions that are not PEP 440 versions;
            # presentation commands must still be able to inspect and
            # remove them.
            version=str(version),
            location=location,
            metadata_location=(str(metadata_location) if metadata_location else None),
            raw=dist,
        )

        # Already parsed above; seed the cache so dependencies() doesn't
        # read and parse the same file a second time.
        distribution._fast_headers = headers

        yield distribution


def iter_installed_distributions(
    paths: Iterable[str] | None = None,
    *,
    names: Collection[str] | None = None,
) -> list[InstalledDistribution]:
    return sorted(
        _iter_installed_distributions(paths, names),
        key=lambda dist: dist.canonical_name,
    )


def find_installed(
    name: str,
    paths: Iterable[str] | None = None,
) -> InstalledDistribution | None:
    canonical = canonicalize_name(name)

    for dist in _iter_installed_distributions(paths, {canonical}):
        if dist.canonical_name == canonical:
            return dist

    return None
