from __future__ import annotations

import logging
import os
import re
import sys
import sysconfig
import zipfile
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .errors import InstallationError, InvalidWheelFilename, UnsupportedWheel
from .packaging import (
    InvalidVersion,
    Requirement,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from .wheel_metadata import (
    metadata_paths,
    parse_metadata_member,
)

if TYPE_CHECKING:
    from email.message import Message
    from email.parser import Parser as EmailParser


class MetadataCache(Protocol):
    """Minimal cache contract needed by wheel parsing."""

    def get_reference(
        self, identity: tuple[str, int, int]
    ) -> dict[str, list[str]] | None: ...

    def put(
        self, identity: tuple[str, int, int], headers: dict[str, list[str]]
    ) -> None: ...


def Parser() -> EmailParser:
    """Lazily construct the legacy email parser."""
    from email.parser import Parser as EmailParser

    return EmailParser()


class WheelCandidate:
    __slots__ = (
        "name",
        "version",
        "path",
        "dependencies",
        "provided_extras",
        "requires_python",
        "source_url",
        "source_hashes",
        "source_kind",
        "source_vcs",
        "from_cache",
        "yanked_reason",
        "wheel_layout",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        path: Path,
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
        self.path = path
        self.dependencies = dependencies
        self.provided_extras = provided_extras
        self.requires_python = requires_python
        self.source_url = source_url
        self.source_hashes = source_hashes
        self.source_kind = source_kind
        self.source_vcs = source_vcs
        self.from_cache = from_cache
        self.yanked_reason = yanked_reason
        self.wheel_layout = wheel_layout

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WheelCandidate) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def copy_with(self, **changes: object) -> WheelCandidate:
        values = {name: getattr(self, name) for name in self.__slots__}
        values.update(changes)
        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)


class WheelTag:
    __slots__ = ("interpreter", "abi", "platform")

    def __init__(self, interpreter: str, abi: str, platform: str) -> None:
        self.interpreter = interpreter
        self.abi = abi
        self.platform = platform

    interpreter: str
    abi: str
    platform: str

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
        return hash((self.interpreter, self.abi, self.platform))


class WheelFile:
    __slots__ = ("name", "version", "build_tag", "tags")

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

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    @classmethod
    def open(cls, path: str | Path) -> zipfile.ZipFile:
        return zipfile.ZipFile(path)


class Wheel:
    def __init__(self, filename: str | Path) -> None:
        self.filename = str(filename)
        wheel = parse_wheel_file(filename)
        if wheel is None:
            raise InvalidWheelFilename(f"Invalid wheel filename: {filename}")
        self.name = wheel.name
        self.version = str(wheel.version)
        self.build_tag = legacy_build_tag(wheel.build_tag)
        self.file_tags = frozenset(wheel.tags)

    def get_formatted_file_tags(self) -> list[str]:
        """Return the wheel's tags as sorted strings."""
        return sorted(str(tag) for tag in self.file_tags)

    def supported(self, tags: list[WheelTag] | tuple[WheelTag, ...]) -> bool:
        return wheel_tag_rank(tuple(self.file_tags), tuple(tags)) is not None

    def support_index_min(self, tags: list[WheelTag] | tuple[WheelTag, ...]) -> int:
        rank = wheel_tag_rank(tuple(self.file_tags), tuple(tags))
        if rank is None:
            raise ValueError("Wheel is not supported")
        return rank


class TargetContext:
    __slots__ = ("platforms", "implementation", "python_version", "abis")

    def __init__(
        self,
        platforms: tuple[str, ...] = (),
        implementation: str | None = None,
        python_version: str | None = None,
        abis: tuple[str, ...] = (),
    ) -> None:
        self.platforms = platforms
        self.implementation = implementation
        self.python_version = python_version
        self.abis = abis

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
        return hash(
            (self.platforms, self.implementation, self.python_version, self.abis)
        )

    platforms: tuple[str, ...]
    implementation: str | None
    python_version: str | None
    abis: tuple[str, ...]


VERSION_COMPATIBLE = (1, 0)
logger = logging.getLogger(__name__)

WHEEL_METADATA_CACHE_SIZE = 1024


class WheelResolutionMetadata:
    __slots__ = (
        "name",
        "version",
        "dependencies",
        "provided_extras",
        "requires_python",
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


wheel_metadata_cache: dict[tuple[str, int, int], WheelResolutionMetadata] = {}
wheel_dependency_cache: dict[
    tuple[tuple[str, int, int], frozenset[str]], tuple[Requirement, ...]
] = {}


def parse_wheel_file(path: str | Path) -> WheelFile | None:
    name = Path(path).name
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
        parsed_version = parsed_wheel_version(version)
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


@lru_cache(maxsize=4096)
def parsed_wheel_version(value: str) -> Version:
    return Version(value)


@lru_cache(maxsize=1024)
def parsed_wheel_tags(
    python_tags: str, abi_tags: str, platform_tags: str
) -> tuple[WheelTag, ...]:
    return tuple(
        WheelTag(interpreter, abi, platform)
        for interpreter in python_tags.split(".")
        for abi in abi_tags.split(".")
        for platform in platform_tags.split(".")
    )


def parse_wheel_filename(path: str | Path) -> tuple[str, str] | None:
    wheel = parse_wheel_file(path)
    if wheel is None:
        return None
    return wheel.name, str(wheel.version)


@cache
def supported_wheel_tags(target: TargetContext | None = None) -> tuple[WheelTag, ...]:
    if target is None:
        major = sys.version_info.major
        minor = sys.version_info.minor
        implementation = "cp"
        version_digits = f"{major}{minor}"
        platform_tags = (current_platform_tag(),)
        abi_tags = ()
    else:
        version = (
            target.python_version or f"{sys.version_info.major}{sys.version_info.minor}"
        )
        version_digits = version.replace(".", "")
        implementation = target.implementation or "cp"
        platform_tags = target.platforms or (current_platform_tag(),)
        abi_tags = target.abis
    impl_tag = f"{implementation}{version_digits}"
    major = version_digits[0]
    interpreters = (impl_tag, f"py{version_digits}", f"py{major}")
    abis = tuple(abi_tags) or (impl_tag, "abi3", "none")
    platforms = tuple(platform_tags) + ("any",)
    return tuple(
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


@lru_cache(maxsize=4096)
def wheel_tag_rank(
    tags: tuple[WheelTag, ...], supported_tags: tuple[WheelTag, ...] | None = None
) -> int | None:
    supported = supported_wheel_tags() if supported_tags is None else supported_tags
    ranks = [
        index
        for index, supported_tag in enumerate(supported)
        for tag in tags
        if tag_matches(supported_tag, tag)
    ]
    if not ranks:
        return None
    return min(ranks)


def wheel_archive_identity(
    path: Path, archive: zipfile.ZipFile | None, dist_info_dir: str | None
) -> tuple[str, int, int] | None:
    try:
        if archive is not None and dist_info_dir is not None:
            metadata = archive.getinfo(f"{dist_info_dir}/METADATA")
            path_key = str(path) if path.is_absolute() else os.path.abspath(path)
            return path_key, metadata.CRC, metadata.file_size
        stat = path.stat()
        path_key = str(path) if path.is_absolute() else os.path.abspath(path)
        return path_key, stat.st_size, stat.st_mtime_ns
    except (KeyError, OSError):
        return None


def bounded_cache_put(cache: dict, key: object, value: object) -> None:
    if len(cache) >= WHEEL_METADATA_CACHE_SIZE:
        cache.clear()
    cache[key] = value


def project_wheel_dependencies(
    metadata: WheelResolutionMetadata,
    identity: tuple[str, int, int] | None,
    extras: frozenset[str],
) -> tuple[Requirement, ...]:
    key = (identity, extras) if identity is not None else None
    dependencies = wheel_dependency_cache.get(key) if key is not None else None
    if dependencies is not None:
        return dependencies
    dependencies = tuple(
        requirement
        for requirement in metadata.dependencies
        if marker_applies(requirement.marker, extras=extras)
    )
    if key is not None:
        bounded_cache_put(wheel_dependency_cache, key, dependencies)
    return dependencies


def wheel_candidate(
    path: str | Path,
    extras: set[str] | None = None,
    *,
    archive: zipfile.ZipFile | None = None,
    filename_info: tuple[str, str | Version] | None = None,
    dist_info_dir: str | None = None,
    metadata_cache: MetadataCache | None = None,
) -> WheelCandidate:
    wheel_path = Path(path)
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

            def get_header(name: str) -> str | None:
                values = headers.get(name.casefold())
                return values[0] if values else None

            def get_all_headers(name: str) -> list[str]:
                return headers.get(name.casefold(), [])

        else:
            message = (
                read_wheel_metadata_internal(
                    archive,
                    wheel_path,
                    expected_name=name,
                    dist_info_dir=dist_info_dir,
                )
                if archive is not None
                else read_wheel_metadata(wheel_path)
            )

            def get_header(name: str) -> str | None:
                return message.get(name)

            def get_all_headers(name: str) -> list[str]:
                return message.get_all(name, [])

        metadata_name = get_header("Name") or name
        metadata_version = get_header("Version") or str(version)
        parsed_metadata_version = (
            version
            if isinstance(version, Version) and metadata_version == str(version)
            else Version(metadata_version)
        )
        metadata = WheelResolutionMetadata(
            name=metadata_name,
            version=parsed_metadata_version,
            dependencies=tuple(
                parse_requirement(value) for value in get_all_headers("Requires-Dist")
            ),
            provided_extras=frozenset(
                value.strip()
                for value in get_all_headers("Provides-Extra")
                if value.strip()
            ),
            requires_python=get_header("Requires-Python"),
        )
        if identity is not None:
            bounded_cache_put(wheel_metadata_cache, identity, metadata)
    requested_extras = frozenset(extras or ())
    dependencies = project_wheel_dependencies(metadata, identity, requested_extras)
    wheel_layout = None
    if archive is not None and dist_info_dir is not None:
        wheel_metadata_text = archive.read(f"{dist_info_dir}/WHEEL").decode("utf-8")
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
        path=wheel_path,
        dependencies=dependencies,
        provided_extras=metadata.provided_extras,
        requires_python=metadata.requires_python,
        wheel_layout=wheel_layout,
    )


def read_core_metadata_headers(
    archive: zipfile.ZipFile,
    path: str | Path,
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
            f"Error decoding metadata for {path}: {metadata_path}"
        ) from exc


def read_wheel_metadata(path: str | Path):
    with zipfile.ZipFile(path) as archive:
        return read_wheel_metadata_internal(archive, path)


def read_wheel_metadata_internal(
    archive: zipfile.ZipFile,
    path: str | Path,
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
        matching = [
            name
            for name in metadata_names
            if name.count("/") == 1
            and name.rsplit("/", 1)[0]
            .split(".", 1)[0]
            .casefold()
            .startswith(expected.casefold())
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
                f"Error decoding metadata for {path}: {metadata_names[0]}"
            ) from exc
        return Parser().parsestr(contents)


def wheel_dist_info_dir(source: zipfile.ZipFile, name: str) -> str:
    # ZipFile already builds this filename index while reading the central
    # directory. Iterating it avoids another ZipInfo lookup for every member,
    # which is significant for wheels containing thousands of files.
    matches = sorted(
        {
            filename.split("/", 1)[0]
            for filename in source.NameToInfo
            if filename.endswith(".dist-info/WHEEL") and filename.count("/") == 1
        }
    )
    if not matches:
        raise UnsupportedWheel(".dist-info directory not found")
    if len(matches) > 1:
        raise UnsupportedWheel("multiple .dist-info directories found")
    dist_info_dir = matches[0]
    expected = re.sub(r"[-_.]+", "", canonicalize_name(name)).casefold()
    actual = re.sub(r"[-_.]+", "", dist_info_dir.removesuffix(".dist-info")).casefold()
    if not actual.startswith(expected):
        raise UnsupportedWheel(
            f".dist-info directory {dist_info_dir!r} does not start with {name!r}"
        )
    return dist_info_dir


def read_wheel_metadata_file(source: zipfile.ZipFile, path: str) -> bytes:
    try:
        return source.read(path)
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise UnsupportedWheel(f"could not read {path!r} file: {exc!r}") from exc


def wheel_metadata(source: zipfile.ZipFile, dist_info_dir: str) -> Message:
    wheel_path = f"{dist_info_dir}/WHEEL"
    raw = read_wheel_metadata_file(source, wheel_path)
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
                name, ".".join(map(str, version))
            )
        )
    elif version > VERSION_COMPATIBLE:
        logger.warning(
            "Installing from a newer Wheel-Version (%s)",
            ".".join(map(str, version)),
        )


def validate_wheel(source: zipfile.ZipFile, name: str) -> str:
    """Validate a wheel without materializing its WHEEL metadata message."""
    try:
        info_dir = wheel_dist_info_dir(source, name)
        wheel_path = f"{info_dir}/WHEEL"
        raw = read_wheel_metadata_file(source, wheel_path)
        try:
            text = raw.decode()
        except UnicodeDecodeError as exc:
            raise UnsupportedWheel(f"error decoding {wheel_path!r}: {exc!r}") from exc
        version = wheel_version_from_text(text)
    except UnsupportedWheel as exc:
        raise UnsupportedWheel(f"{name} has an invalid wheel, {exc}") from exc

    check_compatibility(version, name)
    return info_dir


def parse_wheel(wheel_zip: zipfile.ZipFile, name: str) -> tuple[str, Message]:
    """Validate a wheel archive and return its metadata directory and WHEEL data."""
    try:
        info_dir = wheel_dist_info_dir(wheel_zip, name)
        metadata = wheel_metadata(wheel_zip, info_dir)
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
    return (
        interpreter_matches(
            supported.interpreter.lower(),
            candidate.interpreter.lower(),
            candidate.abi.lower(),
        )
        and supported.abi.lower() == candidate.abi.lower()
        and platform_matches(
            supported.platform.lower(),
            candidate.platform.lower(),
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


def platform_matches(runtime: str, wheel: str) -> bool:
    if runtime == wheel:
        return True
    if runtime == "any" or wheel == "any":
        return runtime == wheel
    if runtime.startswith("macosx_") and wheel.startswith("macosx_"):
        return macos_platform_matches(runtime, wheel)
    if runtime.startswith("ios_") and wheel.startswith("ios_"):
        return ios_platform_matches(runtime, wheel)
    if runtime.startswith("android_") and wheel.startswith("android_"):
        return android_platform_matches(runtime, wheel)
    return False


def macos_platform_matches(runtime: str, wheel: str) -> bool:
    runtime_parts = runtime.split("_", 3)
    wheel_parts = wheel.split("_", 3)
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False
    _, runtime_major, runtime_minor, runtime_arch = runtime_parts
    _, wheel_major, wheel_minor, wheel_arch = wheel_parts
    if (int(wheel_major), int(wheel_minor)) > (int(runtime_major), int(runtime_minor)):
        return False
    compatible_arches = {
        "x86_64": {"x86_64", "intel", "universal"},
        "i386": {"i386", "intel", "universal"},
        "intel": {"intel", "universal"},
        "arm64": {"arm64", "universal2"},
        "aarch64": {"aarch64", "universal2"},
        "ppc": {"ppc", "universal"},
        "ppc64": {"ppc64", "universal"},
        "universal": {"universal"},
        "universal2": {"universal2"},
    }
    return wheel_arch in compatible_arches.get(runtime_arch, {runtime_arch})


def ios_platform_matches(runtime: str, wheel: str) -> bool:
    runtime_parts = runtime.split("_", 4)
    wheel_parts = wheel.split("_", 4)
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


def android_platform_matches(runtime: str, wheel: str) -> bool:
    runtime_parts = runtime.split("_", 3)
    wheel_parts = wheel.split("_", 3)
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False
    _, runtime_api, runtime_arch_a, runtime_arch_b = runtime_parts
    _, wheel_api, wheel_arch_a, wheel_arch_b = wheel_parts
    if (runtime_arch_a, runtime_arch_b) != (wheel_arch_a, wheel_arch_b):
        return False
    return int(wheel_api) <= int(runtime_api)
