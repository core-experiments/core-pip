from __future__ import annotations

import os
import re
import sys
import sysconfig
from collections.abc import Callable, Collection, Mapping
from functools import lru_cache

from .caches import bounded_put, memoized, register_table
from .errors import InstallationError, InvalidWheelFilename, UnsupportedWheel
from .packaging import Requirement, canonicalize_name, marker_applies, parse_requirement
from .versions import InvalidVersion, Version
from .utils import CURRENT_PYTHON_VERSION_DIGITS
from .wheel_metadata import (
    metadata_paths,
    parse_metadata_member,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
    import zipfile
    from email.parser import Parser as EmailParser
    from email.message import Message
    from typing import IO, NoReturn, Protocol

    # Structural types used only in annotations: typing stays off the
    # paths (check, list, show) that import this module for its parsers.
    class ZipEntryInfo(Protocol):
        """The subset of ``zipfile.ZipInfo`` these functions read.

        Satisfied structurally by both ``zipfile.ZipInfo`` and lighter adapters
        over faster archive readers, so this module never needs to know such an
        adapter exists. Declared as read-only properties (rather than plain
        attributes) so an immutable adapter -- e.g. a ``NamedTuple`` -- also
        satisfies it: a plain attribute requires write support too, which a
        read-only field doesn't have.
        """

        @property
        def CRC(self) -> int: ...

        @property
        def compress_type(self) -> int: ...

        @property
        def external_attr(self) -> int: ...

        @property
        def compress_size(self) -> int: ...

        @property
        def file_size(self) -> int: ...

        @property
        def header_offset(self) -> int: ...

    class ZipArchiveSource(Protocol):
        """The subset of ``zipfile.ZipFile`` these functions read from."""

        @property
        def NameToInfo(self) -> Mapping[str, ZipEntryInfo]: ...

        def getinfo(self, name: str) -> ZipEntryInfo: ...

        def read(self, name: str) -> bytes: ...

        def namelist(self) -> list[str]: ...

        def open(self, name: str) -> IO[bytes]: ...

    class MetadataCache(Protocol):
        """Minimal cache contract needed by wheel parsing."""

        def get_reference(
            self,
            identity: tuple[str, int, int],
        ) -> dict[str, list[str]] | None: ...

        def put(
            self,
            identity: tuple[str, int, int],
            headers: dict[str, list[str]],
        ) -> None: ...


class PureWheelCandidate:
    """The candidate shape the pure-wheel installer shortcut needs.

    Both :class:`WheelCandidate` and the process-level fast installer's much
    lighter candidate satisfy it, which is what lets a resolved plan from
    either side reach ``cli.fast.install.install_resolved_pure_wheels``.
    """

    __slots__ = ()

    canonical_name: str

    path: str


MACOS_COMPATIBLE_ARCHES = {
    "x86_64": frozenset(("x86_64", "intel", "universal")),
    "i386": frozenset(("i386", "intel", "universal")),
    "intel": frozenset(("intel", "universal")),
    "arm64": frozenset(("arm64", "universal2")),
    "aarch64": frozenset(("aarch64", "universal2")),
    "ppc": frozenset(("ppc", "universal")),
    "ppc64": frozenset(("ppc64", "universal")),
    "universal": frozenset(("universal",)),
    "universal2": frozenset(("universal2",)),
}


def Parser() -> EmailParser:
    """Lazily construct the legacy email parser.

    The import is deferred as well: ``email.parser`` costs more to import
    than everything the local fast install path runs, and that path never
    parses a METADATA file this way.
    """

    from email.parser import Parser as EmailParser

    return EmailParser()


_UNRESOLVED = object()


class LazyWheelLayout:
    """A wheel layout computed on first use.

    The resolver already holds a local wheel's metadata, and a warm install
    finds its unpacked tree in the archive cache, so the layout -- which
    costs opening the wheel and parsing its directory -- is only needed by
    the paths that extract or copy members. The computed value is memoized
    on this object, so copies of the candidate share one read.
    """

    __slots__ = ("_compute", "_value")

    def __init__(self, compute: Callable[[], object | None]) -> None:
        self._compute: Callable[[], object | None] | None = compute
        self._value: object = _UNRESOLVED

    def resolve(self) -> object | None:
        if self._value is _UNRESOLVED:
            assert self._compute is not None
            self._value = self._compute()
            self._compute = None
        return self._value


class WheelCandidate(PureWheelCandidate):
    __slots__ = (
        "_wheel_layout",
        "dependencies",
        "from_cache",
        "name",
        "path",
        "provided_extras",
        "requires_python",
        "source_hashes",
        "source_kind",
        "source_url",
        "source_vcs",
        "version",
        "yanked_reason",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        path: str,
        dependencies: tuple[Requirement, ...],
        provided_extras: frozenset[str] = frozenset(),
        requires_python: str | None = None,
        source_url: str | None = None,
        source_hashes: dict[str, str] | None = None,
        source_kind: str | None = None,
        source_vcs: str | None = None,
        from_cache: bool = False,
        yanked_reason: str | None = None,
        wheel_layout: object | None = None,
    ) -> None:
        self.name = name

        self.version = version

        self.path = os.fspath(path)

        self.dependencies = dependencies

        self.provided_extras = provided_extras

        self.requires_python = requires_python

        self.source_url = source_url

        self.source_hashes = source_hashes

        self.source_kind = source_kind

        self.source_vcs = source_vcs

        self.from_cache = from_cache

        self.yanked_reason = yanked_reason

        self._wheel_layout = wheel_layout

    @property
    def wheel_layout(self) -> object | None:
        """The layout, computing a :class:`LazyWheelLayout` on first access."""

        layout = self._wheel_layout

        if isinstance(layout, LazyWheelLayout):
            layout = layout.resolve()

            self._wheel_layout = layout

        return layout

    @wheel_layout.setter
    def wheel_layout(self, value: object | None) -> None:
        self._wheel_layout = value

    @property
    def stored_wheel_layout(self) -> object | None:
        """The layout as stored -- possibly still a :class:`LazyWheelLayout`
        -- for a caller rebuilding the candidate without reading the wheel."""

        return self._wheel_layout

    @property
    def wheel_layout_if_loaded(self) -> object | None:
        """The layout only if it is already known; never reads the wheel."""

        layout = self._wheel_layout

        return None if isinstance(layout, LazyWheelLayout) else layout

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WheelCandidate) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def copy_with(self, **changes: object) -> WheelCandidate:
        values = {name: getattr(self, name) for name in self.__slots__}

        # The stored layout travels as it is: a lazy one stays lazy and the
        # copy shares its eventual read.
        values["wheel_layout"] = values.pop("_wheel_layout")

        values.update(changes)

        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)


class WheelTag:
    __slots__ = (
        "_abi_lower",
        "_hash",
        "_interpreter_lower",
        "_platform_lower",
        "_platform_parts",
        "abi",
        "interpreter",
        "platform",
    )

    def __init__(self, interpreter: str, abi: str, platform: str) -> None:
        # Written through object.__setattr__ because __setattr__ below refuses
        # every assignment: a tag lives in sets and dictionary keys, and both
        # the cached hash and the cached lowercase forms would go stale if one
        # were rewritten in place.
        setter = object.__setattr__

        setter(self, "interpreter", interpreter)

        setter(self, "abi", abi)

        setter(self, "platform", platform)

        setter(self, "_interpreter_lower", interpreter.lower())

        setter(self, "_abi_lower", abi.lower())

        platform_lower = platform.lower()

        setter(self, "_platform_lower", platform_lower)

        # Tags are hashed far more often than they are built -- ranking one
        # wheel re-hashes the whole supported-tag tuple to probe
        # wheel_tag_rank's cache, so this runs per candidate per tag, while a
        # process builds only a handful of tags.
        setter(self, "_hash", hash((interpreter, abi, platform)))

        if platform_lower.startswith(("macosx_", "android_")):
            parts = tuple(platform_lower.split("_", 3))

        elif platform_lower.startswith("ios_"):
            parts = tuple(platform_lower.split("_", 4))

        else:
            parts = None

        setter(self, "_platform_parts", parts)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        """Refuse mutation; the hash and lowercase forms are cached."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot set {name!r}",
        )

    def __delattr__(self, name: str) -> NoReturn:
        """Refuse deletion for the same reason as :meth:`__setattr__`."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot delete {name!r}",
        )

    interpreter: str

    abi: str

    platform: str

    # Declared because __init__ writes them through object.__setattr__, which
    # no checker can follow back to an attribute.
    _interpreter_lower: str

    _abi_lower: str

    _platform_lower: str

    _platform_parts: tuple[str, ...] | None

    _hash: int

    def __str__(self) -> str:
        return f"{self.interpreter}-{self.abi}-{self.platform}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WheelTag):
            return NotImplemented

        return (
            self.interpreter == other.interpreter
            and self.abi == other.abi
            and self.platform == other.platform
        )

    def __hash__(self) -> int:
        return self._hash


class WheelFile:
    __slots__ = ("build_tag", "name", "tags", "version")

    def __init__(
        self,
        name: str,
        version: Version,
        build_tag: str | None,
        tags: tuple[WheelTag, ...],
    ) -> None:
        self.name = name

        self.version = version

        self.build_tag = build_tag

        self.tags = tags

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WheelFile) and (
            self.name,
            self.version,
            self.build_tag,
            self.tags,
        ) == (other.name, other.version, other.build_tag, other.tags)

    def __hash__(self) -> int:
        return hash((self.name, self.version, self.build_tag, self.tags))

    name: str

    version: Version

    build_tag: str | None

    tags: tuple[WheelTag, ...]


class Wheel:
    __slots__ = ("build_tag", "file_tags", "filename", "name", "version")

    def __init__(self, filename: str) -> None:
        self.filename = str(filename)

        wheel = parse_wheel_file(filename)

        if wheel is None:
            raise InvalidWheelFilename(f"Invalid wheel filename: {filename}")

        self.name = wheel.name

        self.version = str(wheel.version)

        self.build_tag = legacy_build_tag(wheel.build_tag)

        self.file_tags = frozenset(wheel.tags)


class TargetContext:
    __slots__ = ("_hash", "abis", "implementation", "platforms", "python_version")

    def __init__(
        self,
        platforms: tuple[str, ...] = (),
        implementation: str | None = None,
        python_version: str | None = None,
        abis: tuple[str, ...] = (),
    ) -> None:
        # Written through object.__setattr__ because __setattr__ below
        # refuses every assignment: a target is looked up in the unbounded
        # supported_wheel_tags cache by hash, and both the cache entry and
        # the cached hash below would go stale if a field were rewritten in
        # place after construction.
        setter = object.__setattr__

        setter(self, "platforms", platforms)

        setter(self, "implementation", implementation)

        setter(self, "python_version", python_version)

        setter(self, "abis", abis)

        # A cached, hashable target is looked up on every wheel candidate
        # through the unbounded ``supported_wheel_tags`` cache, which must
        # hash it on every call regardless of whether the result is already
        # cached.
        setter(self, "_hash", hash((platforms, implementation, python_version, abis)))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        """Refuse mutation; the hash is cached and used as a cache key."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot set {name!r}",
        )

    def __delattr__(self, name: str) -> NoReturn:
        """Refuse deletion for the same reason as :meth:`__setattr__`."""
        raise AttributeError(
            f"{type(self).__name__} is immutable, cannot delete {name!r}",
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TargetContext) and (
            self.platforms,
            self.implementation,
            self.python_version,
            self.abis,
        ) == (
            other.platforms,
            other.implementation,
            other.python_version,
            other.abis,
        )

    def __hash__(self) -> int:
        return self._hash

    platforms: tuple[str, ...]

    implementation: str | None

    python_version: str | None

    abis: tuple[str, ...]

    # Declared because __init__ writes it through object.__setattr__, which
    # no checker can follow back to an attribute.
    _hash: int


VERSION_COMPATIBLE = (1, 0)


WHEEL_METADATA_CACHE_SIZE = 1024

# Shared empty result for an absent multi-value header (never mutated).
_NO_HEADERS: list[str] = []


class WheelResolutionMetadata:
    __slots__ = (
        "dependencies",
        "name",
        "provided_extras",
        "requires_python",
        "version",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        dependencies: tuple[Requirement, ...],
        provided_extras: frozenset[str],
        requires_python: str | None,
    ) -> None:
        self.name = name

        self.version = version

        self.dependencies = dependencies

        self.provided_extras = provided_extras

        self.requires_python = requires_python

    name: str

    version: Version

    dependencies: tuple[Requirement, ...]

    provided_extras: frozenset[str]

    requires_python: str | None


wheel_metadata_cache: dict[tuple[str, int, int], WheelResolutionMetadata] = (
    register_table({})
)

wheel_dependency_cache: dict[
    tuple[tuple[str, int, int], frozenset[str]],
    tuple[Requirement, ...],
] = register_table({})

# Keyed by (stat-based identity, requested extras) rather than
# wheel_metadata_cache's own (possibly archive/CRC-based) identity, so
# wheel_candidate_from_path can check it via a cheap os.stat() *before*
# opening the archive at all -- see wheel_candidate_from_path.
no_layout_candidate_cache: dict[
    tuple[tuple[str, int, int], frozenset[str]],
    tuple[str, Version, tuple[Requirement, ...], frozenset[str], str | None],
] = register_table({})


def parse_wheel_file(path: str) -> WheelFile | None:
    name = os.fspath(path)
    # Most callers already hold a bare filename (Link.filename is one), and
    # basename of a string with no separator is the identity -- on ntpath
    # too, which additionally splits on a backslash and a drive colon. Skip
    # the call for those; it was the single largest cost of evaluating a
    # wheel link.
    if "/" in name or "\\" in name or ":" in name:
        name = os.path.basename(name)
    return _parse_wheel_filename(name)


@memoized(4096)
def _parse_wheel_filename(name: str) -> WheelFile | None:
    if not name.endswith(".whl"):
        return None

    stem = name[:-4]

    parts = stem.split("-")

    if len(parts) == 5:
        distribution, version, python_tags, abi_tags, platform_tags = parts

        build_tag = None

    elif len(parts) == 6:
        distribution, version, build_tag, python_tags, abi_tags, platform_tags = parts

    else:
        return None

    if build_tag is not None and "_" in build_tag:
        return None

    try:
        parsed_version = Version(version)

    except InvalidVersion:
        return None

    tags = parsed_wheel_tags(python_tags, abi_tags, platform_tags)

    if not tags:
        return None

    return WheelFile(
        name=canonicalize_name(distribution),
        version=parsed_version,
        build_tag=build_tag,
        tags=tags,
    )


@memoized(1024)
def parsed_wheel_tags(
    python_tags: str,
    abi_tags: str,
    platform_tags: str,
) -> tuple[WheelTag, ...]:
    return tuple(
        WheelTag(interpreter, abi, platform)
        for interpreter in python_tags.split(".")
        for abi in abi_tags.split(".")
        for platform in platform_tags.split(".")
    )


def parse_wheel_filename(path: str) -> tuple[str, str] | None:
    wheel = parse_wheel_file(path)

    if wheel is None:
        return None

    return wheel.name, str(wheel.version)


class _HashCachedTags(tuple):
    """A tuple of WheelTags that memoizes its own hash.

    supported_wheel_tags() results are used as half of wheel_tag_rank()'s
    lru_cache key, and a plain tuple recomputes its hash on every lookup --
    a Python-level WheelTag.__hash__ dispatch per element, times the
    couple-dozen supported tags, per link evaluated. Since the @cache on
    supported_wheel_tags hands out one long-lived instance per target,
    caching the hash on the instance pays those element hashes once per
    process instead of once per link.
    """

    _cached_hash: int

    def __hash__(self) -> int:
        try:
            return self._cached_hash

        except AttributeError:
            value = tuple.__hash__(self)

            self._cached_hash = value

            return value


# A process-constant derivation, not a parse cache: what tags this
# interpreter (or the given target) supports never changes while it runs,
# so it is deliberately not registered for caches.clear_all().
@lru_cache(maxsize=1024)
def supported_wheel_tags(target: TargetContext | None = None) -> tuple[WheelTag, ...]:
    if target is None:
        implementation = "cp"

        version_digits = CURRENT_PYTHON_VERSION_DIGITS

        platform_tags = (current_platform_tag(),)

        abi_tags = ()

    else:
        version = target.python_version or CURRENT_PYTHON_VERSION_DIGITS

        version_digits = version.replace(".", "")

        implementation = target.implementation or "cp"

        platform_tags = target.platforms or (current_platform_tag(),)

        abi_tags = target.abis

    impl_tag = f"{implementation}{version_digits}"

    major = version_digits[0]

    interpreters = (impl_tag, f"py{version_digits}", f"py{major}")

    abis = tuple(abi_tags) or (impl_tag, "abi3", "none")

    platforms = tuple(platform_tags) + ("any",)

    return _HashCachedTags(
        WheelTag(interpreter, abi, platform)
        for interpreter in interpreters
        for abi in abis
        for platform in platforms
    )


def current_platform_tag() -> str:
    platform_name = sysconfig.get_platform()

    if sys.platform == "darwin":
        import platform

        mac_version = platform.mac_ver()[0].split(".")

        if len(mac_version) >= 2 and all(part.isdigit() for part in mac_version[:2]):
            platform_name = (
                f"macosx_{mac_version[0]}_{mac_version[1]}_{platform.machine()}"
            )

    return platform_name.replace("-", "_").replace(".", "_")


@memoized(4096)
def wheel_tag_rank(
    tags: tuple[WheelTag, ...],
    supported_tags: tuple[WheelTag, ...] | None = None,
) -> int | None:
    supported = supported_wheel_tags() if supported_tags is None else supported_tags

    for index, supported_tag in enumerate(supported):
        for tag in tags:
            if tag_matches(supported_tag, tag):
                return index

    return None


def wheel_archive_identity(
    path: str,
    archive: ZipArchiveSource | None,
    dist_info_dir: str | None,
) -> tuple[str, int, int] | None:
    path_text = os.fspath(path)

    try:
        if archive is not None and dist_info_dir is not None:
            metadata = archive.getinfo(f"{dist_info_dir}/METADATA")

            path_key = (
                path_text if os.path.isabs(path_text) else os.path.abspath(path_text)
            )

            return path_key, metadata.CRC, metadata.file_size

        stat = os.stat(path_text)

        path_key = path_text if os.path.isabs(path_text) else os.path.abspath(path_text)

        return path_key, stat.st_size, stat.st_mtime_ns

    except (KeyError, OSError):
        return None


def project_wheel_dependencies(
    metadata: WheelResolutionMetadata,
    identity: tuple[str, int, int] | None,
    extras: frozenset[str],
) -> tuple[Requirement, ...]:
    key = (identity, extras) if identity is not None else None

    dependencies = wheel_dependency_cache.get(key) if key is not None else None

    if dependencies is not None:
        return dependencies

    declared = metadata.dependencies

    # A wheel whose Requires-Dist lines carry no markers applies them all
    # for every extras set: hand back the metadata's own tuple instead of
    # filtering -- and when filtering is needed, a list comprehension runs
    # in one frame where the generator expression paid a frame resumption
    # per dependency, per candidate wheel examined.
    if all(requirement.marker is None for requirement in declared):
        dependencies = declared
    else:
        dependencies = tuple(
            [
                requirement
                for requirement in declared
                if marker_applies(requirement.marker, extras=extras)
            ],
        )

    if key is not None:
        bounded_put(
            wheel_dependency_cache, key, dependencies, WHEEL_METADATA_CACHE_SIZE
        )

    return dependencies


def wheel_candidate(
    path: str,
    extras: Collection[str] | None = None,
    *,
    archive: ZipArchiveSource | None = None,
    filename_info: tuple[str, str | Version] | None = None,
    dist_info_dir: str | None = None,
    wheel_metadata_text: str | None = None,
    include_layout: bool = True,
    metadata_cache: MetadataCache | None = None,
) -> WheelCandidate:
    wheel_path = os.fspath(path)

    parsed = filename_info or parse_wheel_filename(wheel_path)

    if parsed is None:
        raise InvalidWheelFilename(f"Invalid wheel filename: {wheel_path}")

    name, version = parsed

    identity = wheel_archive_identity(wheel_path, archive, dist_info_dir)

    metadata = wheel_metadata_cache.get(identity) if identity is not None else None

    if metadata is None:
        if archive is not None and dist_info_dir is not None:
            headers = (
                metadata_cache.get_reference(identity)
                if metadata_cache is not None and identity is not None
                else None
            )

            if headers is None:
                headers = read_core_metadata_headers(archive, wheel_path, dist_info_dir)

                if metadata_cache is not None and identity is not None:
                    metadata_cache.put(identity, headers)

            # The header dict is keyed by casefolded names already; these are
            # the same lookups without a casefold per call (five per
            # candidate wheel examined).
            header_get = headers.get

            def get_header(name: str) -> str | None:
                values = header_get(name)

                return values[0] if values else None

            def get_all_headers(name: str) -> list[str]:
                return header_get(name, _NO_HEADERS)

        else:
            message = (
                read_metadata_message_internal(
                    archive,
                    wheel_path,
                    expected_name=name,
                    dist_info_dir=dist_info_dir,
                )
                if archive is not None
                else read_metadata_message(wheel_path)
            )

            def get_header(name: str) -> str | None:
                return message.get(name)

            def get_all_headers(name: str) -> list[str]:
                return message.get_all(name, [])

        metadata_name = get_header("name") or name

        metadata_version = get_header("version") or str(version)

        parsed_metadata_version = (
            version
            if isinstance(version, Version) and metadata_version == str(version)
            else Version(metadata_version)
        )

        metadata = WheelResolutionMetadata(
            name=metadata_name,
            version=parsed_metadata_version,
            # map(), not a genexpr: this runs per candidate wheel examined
            # during resolution, and a genexpr pays a generator-frame
            # resumption per Requires-Dist line where map iterates at the
            # C level with no intermediate frame or list at all.
            dependencies=tuple(
                map(parse_requirement, get_all_headers("requires-dist")),
            ),
            provided_extras=frozenset(
                stripped
                for value in get_all_headers("provides-extra")
                if (stripped := value.strip())
            ),
            requires_python=get_header("requires-python"),
        )

        if identity is not None:
            bounded_put(
                wheel_metadata_cache, identity, metadata, WHEEL_METADATA_CACHE_SIZE
            )

    requested_extras = frozenset(extras or ())

    dependencies = project_wheel_dependencies(metadata, identity, requested_extras)

    wheel_layout = None

    if include_layout and archive is not None and dist_info_dir is not None:
        if wheel_metadata_text is None:
            wheel_metadata_text = archive.read(f"{dist_info_dir}/WHEEL").decode("utf-8")

        # Everything open_wheel_archive needs to read the members again
        # without re-parsing the central directory -- the mode bits
        # included, so executable members keep their modes on that path.
        wheel_layout = (
            dist_info_dir,
            tuple(
                (
                    name,
                    info.compress_type,
                    info.CRC,
                    info.compress_size,
                    info.file_size,
                    info.header_offset,
                    info.external_attr,
                )
                for name, info in archive.NameToInfo.items()
            ),
            any(
                line.casefold().strip() == "root-is-purelib: true"
                for line in wheel_metadata_text.splitlines()
            ),
        )

    return WheelCandidate(
        name=metadata.name,
        version=metadata.version,
        path=os.fspath(wheel_path),
        dependencies=dependencies,
        provided_extras=metadata.provided_extras,
        requires_python=metadata.requires_python,
        wheel_layout=wheel_layout,
    )


def read_core_metadata_headers(
    archive: ZipArchiveSource,
    path: str,
    dist_info_dir: str,
) -> dict[str, list[str]]:
    """Read core metadata headers needed during candidate resolution."""

    metadata_path = f"{dist_info_dir}/METADATA"

    try:
        return parse_metadata_member(archive.read, metadata_path)

    except KeyError as exc:
        raise InstallationError(f"Wheel has no METADATA: {path}") from exc

    except UnicodeDecodeError as exc:
        raise InstallationError(
            f"Error decoding metadata for {path}: {metadata_path}",
        ) from exc


def read_metadata_message(path: str):
    import zipfile

    with zipfile.ZipFile(path) as archive:
        return read_metadata_message_internal(archive, path)


def read_metadata_message_internal(
    archive: ZipArchiveSource,
    path: str,
    *,
    expected_name: str | None = None,
    dist_info_dir: str | None = None,
) -> Message:
    metadata_names = (
        [f"{dist_info_dir}/METADATA"]
        if dist_info_dir is not None
        else metadata_paths(archive.namelist())
    )

    if not metadata_names:
        raise InstallationError(f"Wheel has no METADATA: {path}")

    if expected_name is None:
        parsed = parse_wheel_filename(path)

        expected_name = parsed[0] if parsed is not None else None

    if expected_name is not None:
        expected = canonicalize_name(expected_name).replace("-", "_")

        expected_casefold = expected.casefold()

        matching = [
            name
            for name in metadata_names
            if name.count("/") == 1
            and name.rsplit("/", 1)[0]
            .split(".", 1)[0]
            .casefold()
            .startswith(expected_casefold)
        ]

        if matching:
            metadata_names = matching

    try:
        metadata_file = archive.open(metadata_names[0])

    except KeyError as exc:
        raise InstallationError(f"Wheel has no METADATA: {path}") from exc

    with metadata_file as file:
        try:
            contents = file.read().decode("utf-8")

        except UnicodeDecodeError as exc:
            raise InstallationError(
                f"Error decoding metadata for {path}: {metadata_names[0]}",
            ) from exc

        return Parser().parsestr(contents)


_NAME_SEPARATORS_RE = re.compile(r"[-_.]+")


@memoized(4096)
def _dist_info_match_key(name: str) -> str:
    """Normalized project name for matching against a dist-info directory.

    Every release of a package examined during a resolve calls
    ``wheel_dist_info_dir`` with the same project name, so this is the same
    regex substitution repeated once per candidate wheel with an identical
    result each time.
    """
    return re.sub(r"[-_.]+", "", canonicalize_name(name)).casefold()


def wheel_dist_info_dir(source: ZipArchiveSource, name: str) -> str:
    # ZipFile already builds this filename index while reading the central

    # directory. Iterating it avoids another ZipInfo lookup for every member,

    # which is significant for wheels containing thousands of files.

    dist_info_dir: str | None = None

    for filename in source.NameToInfo:
        if not filename.endswith(".dist-info/WHEEL") or filename.count("/") != 1:
            continue

        match = filename.split("/", 1)[0]

        if dist_info_dir is not None:
            raise UnsupportedWheel("multiple .dist-info directories found")

        dist_info_dir = match

    if dist_info_dir is None:
        raise UnsupportedWheel(".dist-info directory not found")

    expected = _dist_info_match_key(name)

    actual = _NAME_SEPARATORS_RE.sub(
        "", dist_info_dir.removesuffix(".dist-info")
    ).casefold()

    if not actual.startswith(expected):
        raise UnsupportedWheel(
            f".dist-info directory {dist_info_dir!r} does not start with {name!r}",
        )

    return dist_info_dir


def read_wheel_archive_member(source: ZipArchiveSource, path: str) -> bytes:
    import zipfile

    try:
        return source.read(path)

    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise UnsupportedWheel(f"could not read {path!r} file: {exc!r}") from exc


def read_wheel_format_metadata(source: ZipArchiveSource, dist_info_dir: str) -> Message:
    wheel_path = f"{dist_info_dir}/WHEEL"

    raw = read_wheel_archive_member(source, wheel_path)

    try:
        text = raw.decode()

    except UnicodeDecodeError as exc:
        raise UnsupportedWheel(f"error decoding {wheel_path!r}: {exc!r}") from exc

    return Parser().parsestr(text)


def wheel_version(metadata: Message) -> tuple[int, ...]:
    value = metadata.get("Wheel-Version")

    if value is None:
        raise UnsupportedWheel("WHEEL is missing Wheel-Version")

    version = value.strip()

    try:
        return tuple(map(int, version.split(".")))

    except ValueError as exc:
        raise UnsupportedWheel(f"invalid Wheel-Version: {version!r}") from exc


def wheel_version_from_text(text: str) -> tuple[int, ...]:
    """Read Wheel-Version without constructing an email Message."""

    value: str | None = None

    for line in text.splitlines():
        if not line:
            break

        name, separator, header_value = line.partition(":")

        if separator and name.casefold() == "wheel-version":
            value = header_value.strip()

            break

    if value is None:
        raise UnsupportedWheel("WHEEL is missing Wheel-Version")

    try:
        return tuple(map(int, value.split(".")))

    except ValueError as exc:
        raise UnsupportedWheel(f"invalid Wheel-Version: {value!r}") from exc


def check_compatibility(version: tuple[int, ...], name: str) -> None:
    if version[0] > VERSION_COMPATIBLE[0]:
        raise UnsupportedWheel(
            "{}'s Wheel-Version ({}) is not compatible with this version of cpip".format(
                name,
                ".".join(map(str, version)),
            ),
        )

    if version > VERSION_COMPATIBLE:
        # The module's only use of logging; importing it here keeps the
        # logging machinery off the local fast install path.
        import logging

        logging.getLogger(__name__).warning(
            "Installing from a newer Wheel-Version (%s)",
            ".".join(map(str, version)),
        )


def validate_wheel_with_metadata(
    source: ZipArchiveSource, name: str
) -> tuple[str, str]:
    """Validate a wheel and return its metadata directory and WHEEL text."""

    try:
        info_dir = wheel_dist_info_dir(source, name)

        wheel_path = f"{info_dir}/WHEEL"

        raw = read_wheel_archive_member(source, wheel_path)

        try:
            text = raw.decode()

        except UnicodeDecodeError as exc:
            raise UnsupportedWheel(f"error decoding {wheel_path!r}: {exc!r}") from exc

        version = wheel_version_from_text(text)

    except UnsupportedWheel as exc:
        raise UnsupportedWheel(f"{name} has an invalid wheel, {exc}") from exc

    check_compatibility(version, name)

    return info_dir, text


def validate_wheel(source: ZipArchiveSource, name: str) -> str:
    """Validate a wheel without materializing its WHEEL metadata message."""

    return validate_wheel_with_metadata(source, name)[0]


def wheel_candidate_from_path(
    path: str,
    extras: Collection[str] | None = None,
    *,
    include_layout: bool = True,
) -> WheelCandidate:
    """Build a WheelCandidate for a wheel not already backed by an open archive.

    ``wheel_candidate(path)`` alone reopens the archive with no dist-info
    directory in hand, which sends it through the slow, email-based metadata
    fallback. Validating first and handing the freshly opened archive and
    dist-info directory into ``wheel_candidate`` directly -- the same thing
    candidate materialization does during resolution -- takes the fast path
    instead. Use this instead of a bare ``wheel_candidate(path)`` call
    whenever there's no already-open archive to reuse (a freshly built
    wheel, a batch of paths to validate, an install with no pre-resolved
    candidate).

    ``include_layout`` defaults to True to preserve the resolver-cache
    benefit (a later real install can reuse the captured zip layout instead
    of re-reading the central directory). Callers that build a candidate and
    then immediately extract it themselves -- and so never reuse that cached
    layout -- should pass ``include_layout=False``: a non-None layout on a
    freshly (re)opened archive makes ``open_wheel_archive`` fall back to the
    slower ``zipfile.ZipFile`` reader instead of the fast raw one.

    When ``include_layout=False``, this also checks/populates
    ``no_layout_candidate_cache`` keyed by a cheap ``os.stat()``-based
    identity *before* opening the archive at all. wheel_candidate()'s own
    metadata cache can't help here on its own: it's checked only after this
    function has already paid for opening and fully parsing the archive
    (zipfile.ZipFile eagerly parses every member into a ZipInfo on
    construction) just to hand it in. That parsing is real work independent
    of validate_wheel_with_metadata's own dist-info checks, which the
    caller's later archive open (via open_wheel_archive + validate_wheel,
    once wheel_layout stays None) redundantly repeats anyway -- so a stat
    identity match here means we can skip both without losing any structural
    validation that wasn't already going to happen again downstream.
    """
    import zipfile

    requested_extras = frozenset(extras or ())

    if not include_layout:
        identity = wheel_archive_identity(path, None, None)

        if identity is not None:
            cached = no_layout_candidate_cache.get((identity, requested_extras))

            if cached is not None:
                name, version, dependencies, provided_extras, requires_python = cached

                return WheelCandidate(
                    name=name,
                    version=version,
                    path=os.fspath(path),
                    dependencies=dependencies,
                    provided_extras=provided_extras,
                    requires_python=requires_python,
                )

    with (
        open(path, "rb", buffering=32768) as stream,
        zipfile.ZipFile(stream) as archive,
    ):
        dist_info_dir, wheel_metadata_text = validate_wheel_with_metadata(
            archive,
            os.path.basename(path)[:-4].split("-", 1)[0],
        )
        candidate = wheel_candidate(
            path,
            extras,
            archive=archive,
            dist_info_dir=dist_info_dir,
            wheel_metadata_text=wheel_metadata_text,
            include_layout=include_layout,
        )

    if not include_layout:
        identity = wheel_archive_identity(path, None, None)

        if identity is not None:
            bounded_put(
                no_layout_candidate_cache,
                (identity, requested_extras),
                (
                    candidate.name,
                    candidate.version,
                    candidate.dependencies,
                    candidate.provided_extras,
                    candidate.requires_python,
                ),
                WHEEL_METADATA_CACHE_SIZE,
            )

    return candidate


def parse_wheel(wheel_zip: zipfile.ZipFile, name: str) -> tuple[str, Message]:
    """Validate a wheel archive and return its metadata directory and WHEEL data."""

    try:
        info_dir = wheel_dist_info_dir(wheel_zip, name)

        metadata = read_wheel_format_metadata(wheel_zip, info_dir)

        version = wheel_version(metadata)

    except UnsupportedWheel as exc:
        raise UnsupportedWheel(f"{name} has an invalid wheel, {exc}") from exc

    check_compatibility(version, name)

    return info_dir, metadata


def legacy_build_tag(value: str | None) -> tuple[int, str] | tuple[()]:
    if value is None:
        return ()

    digits = ""

    suffix = ""

    for index, char in enumerate(value):
        if char.isdigit():
            digits += char

            continue

        suffix = value[index:]

        break

    return (int(digits or 0), suffix)


def tag_matches(supported: WheelTag, candidate: WheelTag) -> bool:
    supported_interpreter = supported._interpreter_lower

    candidate_interpreter = candidate._interpreter_lower

    supported_abi = supported._abi_lower

    candidate_abi = candidate._abi_lower

    return (
        interpreter_matches(
            supported_interpreter,
            candidate_interpreter,
            candidate_abi,
        )
        and supported_abi == candidate_abi
        and platform_matches(
            supported._platform_lower,
            candidate._platform_lower,
            supported._platform_parts,
            candidate._platform_parts,
        )
    )


def interpreter_matches(runtime: str, wheel: str, abi: str) -> bool:
    if runtime == wheel:
        return True

    if abi == "abi3" and runtime.startswith("cp") and wheel.startswith("cp"):
        try:
            return int(wheel[2:]) <= int(runtime[2:])

        except ValueError:
            return False

    if wheel == "py3" and runtime.startswith(("cp", "py")):
        return True

    return False


def platform_matches(
    runtime: str,
    wheel: str,
    runtime_parts: tuple[str, ...] | None,
    wheel_parts: tuple[str, ...] | None,
) -> bool:
    if runtime == wheel:
        return True

    if runtime == "any" or wheel == "any":
        return runtime == wheel

    if runtime.startswith("macosx_") and wheel.startswith("macosx_"):
        assert runtime_parts is not None
        assert wheel_parts is not None

        return _macos_platform_matches_parts(runtime_parts, wheel_parts)

    if runtime.startswith("ios_") and wheel.startswith("ios_"):
        assert runtime_parts is not None
        assert wheel_parts is not None

        return _ios_platform_matches_parts(runtime_parts, wheel_parts)

    if runtime.startswith("android_") and wheel.startswith("android_"):
        assert runtime_parts is not None
        assert wheel_parts is not None

        return _android_platform_matches_parts(runtime_parts, wheel_parts)

    return False


def _macos_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False

    _, runtime_major, runtime_minor, runtime_arch = runtime_parts

    _, wheel_major, wheel_minor, wheel_arch = wheel_parts

    if (int(wheel_major), int(wheel_minor)) > (int(runtime_major), int(runtime_minor)):
        return False

    compatible_arches = MACOS_COMPATIBLE_ARCHES.get(runtime_arch)

    return (
        wheel_arch == runtime_arch
        if compatible_arches is None
        else wheel_arch in compatible_arches
    )


def _ios_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 5 or len(wheel_parts) != 5:
        return False

    _, runtime_major, runtime_minor, runtime_arch, runtime_env = runtime_parts

    _, wheel_major, wheel_minor, wheel_arch, wheel_env = wheel_parts

    if runtime_arch != wheel_arch or runtime_env != wheel_env:
        return False

    return (int(wheel_major), int(wheel_minor)) <= (
        int(runtime_major),
        int(runtime_minor),
    )


def _android_platform_matches_parts(
    runtime_parts: tuple[str, ...],
    wheel_parts: tuple[str, ...],
) -> bool:
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False

    _, runtime_api, runtime_arch_a, runtime_arch_b = runtime_parts

    _, wheel_api, wheel_arch_a, wheel_arch_b = wheel_parts

    if (runtime_arch_a, runtime_arch_b) != (wheel_arch_a, wheel_arch_b):
        return False

    return int(wheel_api) <= int(runtime_api)
