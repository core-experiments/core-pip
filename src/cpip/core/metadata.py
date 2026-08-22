from __future__ import annotations

import os
import pathlib
import site
import sys
import sysconfig
from collections.abc import Collection, Iterable

from .packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from .versions import Version, version_of
from .wheel_metadata import parse_metadata_headers

TYPE_CHECKING = False

if TYPE_CHECKING:
    import importlib.metadata
    from email.message import Message

stdlib_pkgs = {"python", "wsgiref", "argparse"}


def _read_raw_metadata_text(
    raw: importlib.metadata.Distribution,
) -> str | None:
    """Read the metadata file text through the same fallback chain as
    ``importlib.metadata.Distribution.metadata``: ``METADATA``, then
    ``PKG-INFO`` for sdist-built distributions, then the bare dist-info path
    for old egg-info installs that have neither.

    ``Distribution.read_text`` goes through ``pathlib.Path.joinpath`` and
    ``Path.read_text`` for every candidate filename, which is real overhead
    across a whole-environment scan. When ``raw._path`` is a genuine
    ``pathlib.Path`` -- true for every finder-discovered on-disk
    distribution, per ``importlib.metadata``'s own ``FastPath.joinpath``
    (only rebound to ``zipfile.Path.joinpath`` when the root turns out to be
    a zip) -- reading through plain ``open()`` is equivalent but cheaper.
    Anything else (a zipped egg, or a custom finder's own path type) falls
    back to the original, fully general chain unchanged.
    """
    path = getattr(raw, "_path", None)

    if isinstance(path, pathlib.Path):
        base = str(path)

        for filename in ("METADATA", "PKG-INFO", ""):
            target = base if not filename else os.path.join(base, filename)

            try:
                with open(target, encoding="utf-8") as file:
                    text = file.read()

            except (
                FileNotFoundError,
                IsADirectoryError,
                NotADirectoryError,
                PermissionError,
            ):
                continue

            if text:
                return text

        return None

    return raw.read_text("METADATA") or raw.read_text("PKG-INFO") or raw.read_text("")


class InstalledDistribution:
    """An installed distribution as found on disk.

    ``raw_version`` is the text in its METADATA; ``version`` is that text
    as a Version, or None when it is not a PEP 440 version (a legacy
    package), which every comparison reads as "not that version" so that
    inspection and removal keep working for such packages.
    """

    __slots__ = (
        "_fast_headers",
        "location",
        "metadata_location",
        "name",
        "raw",
        "raw_version",
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

        self.raw_version = version

        self.version = version_of(version)

        self.location = location

        self.metadata_location = metadata_location

        self.raw = raw

        self._fast_headers: dict[str, list[str]] | None = None

    name: str

    raw_version: str

    version: Version | None

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

    # Deferred: importlib.metadata costs ~10 ms to import (email.message and
    # friends), and an install that ignores the installed state never scans.
    import importlib.metadata

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


# One environment scan per run instead of one per lookup.
#
# find_installed is asked once per package during a default (no
# --ignore-installed) resolve, once per root requirement beforehand, and
# again per candidate while reporting; each call used to re-walk every
# sys.path entry through importlib.metadata and re-read every installed
# distribution's METADATA just to filter by one name -- a 300-distribution
# environment paid ~20ms a call, so a 60-package resolve spent over a
# second re-reading the same files. The index below is built once per
# search-path tuple and revalidated by the mtime of each search-path entry:
# installing or removing a distribution creates or deletes a dist-info
# directory, which bumps its parent's mtime, so cpip's own installs in the
# same process (and most external ones) invalidate it for the price of a
# stat per entry. Rewriting a METADATA file in place without touching its
# dist-info directory is the one change this does not see; no installer does
# that (an upgrade replaces the whole dist-info directory), and
# clear_installed_index covers anything that does.
_InstalledIndex = dict[str, InstalledDistribution]
# Keyed by (default scan?, search paths): the default scan consults every
# metadata finder on sys.meta_path, an explicit path list only the path
# finder, so an explicit tuple equal to sys.path is a different scan.
_installed_index_cache: dict[
    tuple[bool, tuple[str, ...]], tuple[tuple[int | None, ...], _InstalledIndex]
] = {}


def _search_paths(paths: Iterable[str] | None) -> tuple[str, ...]:
    if paths is None:
        return tuple(os.fspath(entry) for entry in sys.path)

    return tuple(os.fspath(path) for path in paths)


def _paths_generation(search_paths: tuple[str, ...]) -> tuple[int | None, ...]:
    generation: list[int | None] = []

    for entry in search_paths:
        try:
            generation.append(os.stat(entry).st_mtime_ns)

        except OSError:
            generation.append(None)

    return tuple(generation)


def installed_index(paths: Iterable[str] | None = None) -> _InstalledIndex:
    """Installed distributions by canonical name, first match on the path wins."""
    search_paths = _search_paths(paths)

    generation = _paths_generation(search_paths)

    cache_key = (paths is None, search_paths)

    cached = _installed_index_cache.get(cache_key)

    if cached is not None and cached[0] == generation:
        return cached[1]

    index: _InstalledIndex = {}

    # The materialized tuple, not ``paths``: a generator argument was
    # already consumed computing the key, and None must stay None so the
    # default scan keeps consulting every metadata finder, not just
    # sys.path.
    for dist in _iter_installed_distributions(None if paths is None else search_paths):
        index.setdefault(dist.canonical_name, dist)

    _installed_index_cache[cache_key] = (generation, index)

    return index


def clear_installed_index() -> None:
    """Forget every cached environment scan (tests and in-process installers)."""
    _installed_index_cache.clear()


def find_installed(
    name: str,
    paths: Iterable[str] | None = None,
) -> InstalledDistribution | None:
    return installed_index(paths).get(canonicalize_name(name))
