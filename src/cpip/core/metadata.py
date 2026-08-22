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
    from typing import Protocol

    HeaderIdentity = tuple[str, int, int]

    class HeaderCache(Protocol):
        """What the installed-state scan asks of a persistent header store.

        ``index/metadata_cache.py:WheelMetadataCache`` is the implementation;
        the identity is that module's ``metadata_identity`` shape -- absolute
        path, size, mtime in nanoseconds -- so one table serves both local
        wheels and installed METADATA files.
        """

        def prefetch(self, identities: Iterable[HeaderIdentity]) -> None: ...

        def get_reference(
            self, identity: HeaderIdentity
        ) -> dict[str, list[str]] | None: ...

        def put(
            self, identity: HeaderIdentity, headers: dict[str, list[str]]
        ) -> None: ...


stdlib_pkgs = {"python", "wsgiref", "argparse"}


def _read_text_file(target: str) -> str | None:
    try:
        with open(target, encoding="utf-8") as file:
            return file.read()

    except (
        FileNotFoundError,
        IsADirectoryError,
        NotADirectoryError,
        PermissionError,
    ):
        return None


def _read_raw_metadata_text(
    raw: RawDistribution,
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

            text = _read_text_file(target)

            if text:
                return text

        return None

    return raw.read_text("METADATA") or raw.read_text("PKG-INFO") or raw.read_text("")


class PathDistribution:
    """A ``*.dist-info`` or ``*.egg-info`` entry found under a search path.

    The cheap stand-in for ``importlib.metadata.PathDistribution``, answering
    the same ``_path``, ``read_text`` and ``locate_file`` that cpip reads
    itself; the parsed ``metadata`` message and ``files`` are delegated to
    the real one, built on first use, so they behave identically while the
    commands that never touch them skip that module's import.
    """

    __slots__ = ("_path", "_stdlib")

    def __init__(self, path: pathlib.Path) -> None:
        self._path = path

        self._stdlib: importlib.metadata.PathDistribution | None = None

    def read_text(self, filename: str) -> str | None:
        try:
            return self._path.joinpath(filename).read_text(encoding="utf-8")

        except (
            FileNotFoundError,
            IsADirectoryError,
            KeyError,
            NotADirectoryError,
            PermissionError,
        ):
            return None

    def locate_file(self, path: str | os.PathLike[str]) -> pathlib.Path:
        return self._path.parent / path

    @property
    def stdlib(self) -> importlib.metadata.PathDistribution:
        if self._stdlib is None:
            import importlib.metadata

            self._stdlib = importlib.metadata.PathDistribution(self._path)

        return self._stdlib

    @property
    def metadata(self) -> importlib.metadata.PackageMetadata:
        return self.stdlib.metadata

    @property
    def files(self) -> list[importlib.metadata.PackagePath] | None:
        return self.stdlib.files


if TYPE_CHECKING:
    RawDistribution = importlib.metadata.Distribution | PathDistribution


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
        raw: RawDistribution,
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

    raw: RawDistribution

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


_INFO_SUFFIXES = (".dist-info", ".egg-info")


def _iter_raw_distributions(
    paths: Iterable[str] | None,
) -> Iterable[RawDistribution]:
    """Every metadata entry ``importlib.metadata.distributions`` would find.

    That function costs ~12 ms to import (``inspect``, ``email`` and
    friends), paid by every default install just to list ``*.dist-info``
    directories. A plain directory root is listed here the way the stdlib's
    ``FastPath``/``Lookup`` list it -- case-insensitive suffix match on the
    child name, no other filter -- and every other shape keeps going through
    the stdlib so its answer is unchanged: a root that is a file (a zip or
    egg on ``sys.path``), a ``*.egg`` directory (whose ``EGG-INFO`` child
    counts too), and a default scan while any other finder on
    ``sys.meta_path`` offers ``find_distributions``.
    """

    if paths is None:
        from importlib.machinery import PathFinder

        if any(
            finder is not PathFinder and hasattr(finder, "find_distributions")
            for finder in sys.meta_path
        ):
            import importlib.metadata

            yield from importlib.metadata.distributions()

            return

        roots = [os.fspath(entry) for entry in sys.path]

    else:
        roots = [os.fspath(path) for path in paths]

    for root in roots:
        try:
            children = os.listdir(root or ".")

        except OSError:
            if os.path.isfile(root):
                import importlib.metadata

                yield from importlib.metadata.distributions(path=[root])

            continue

        if os.path.basename(root).lower().endswith(".egg"):
            import importlib.metadata

            yield from importlib.metadata.distributions(path=[root])

            continue

        for child in children:
            if child.lower().endswith(_INFO_SUFFIXES):
                yield PathDistribution(pathlib.Path(root, child))


# The persistent store for parsed METADATA headers, if the command wired
# one (cli/install does, with the wheel metadata cache): a default install
# otherwise reads and parses every installed distribution's METADATA on
# every run, ~0.1 ms each, just to learn names and versions that have not
# changed since the last run. Keyed by the file's path, size and mtime, so a
# replaced dist-info directory is always a miss.
_header_cache: HeaderCache | None = None


def use_header_cache(cache: HeaderCache | None) -> None:
    global _header_cache

    _header_cache = cache


def _metadata_file_identity(base: str) -> HeaderIdentity | None:
    """The header-cache key of ``<dist-info>/METADATA`` -- the same shape as
    ``index/metadata_cache.py:metadata_identity`` -- or None without one."""
    target = os.path.join(base, "METADATA")

    try:
        stat = os.stat(target)

    except OSError:
        return None

    return (os.path.abspath(target), stat.st_size, stat.st_mtime_ns)


def _iter_installed_distributions(
    paths: Iterable[str] | None = None,
    names: Collection[str] | None = None,
) -> Iterable[InstalledDistribution]:
    canonical_names = (
        {canonicalize_name(name) for name in names} if names is not None else None
    )

    cache = _header_cache

    found: Iterable[RawDistribution] = _iter_raw_distributions(paths)

    identities: dict[int, HeaderIdentity] = {}

    if cache is not None:
        found = list(found)

        for dist in found:
            path = getattr(dist, "_path", None)

            if isinstance(path, pathlib.Path):
                identity = _metadata_file_identity(str(path))

                if identity is not None:
                    identities[id(dist)] = identity

        cache.prefetch(identities.values())

    for dist in found:
        headers = None

        identity = identities.get(id(dist))

        if identity is not None and cache is not None:
            headers = cache.get_reference(identity)

            if headers is None:
                text = _read_text_file(identity[0])

                if text:
                    headers = parse_metadata_headers(text)

                    cache.put(identity, headers)

        if headers is None:
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
