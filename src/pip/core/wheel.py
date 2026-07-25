from __future__ import annotations

import logging
import re
import sys
import sysconfig
import zipfile
from dataclasses import dataclass
from email.message import Message
from email.parser import Parser
from pathlib import Path

from .errors import InstallationError, InvalidWheelFilename, UnsupportedWheel
from .packaging import (
    InvalidVersion,
    Requirement,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)


@dataclass(frozen=True)
class WheelCandidate:
    name: str
    version: Version
    path: Path
    dependencies: tuple[Requirement, ...]
    provided_extras: frozenset[str] = frozenset()
    requires_python: str | None = None
    source_url: str | None = None
    source_hashes: dict[str, str] | None = None
    source_kind: str | None = None
    source_vcs: str | None = None
    from_cache: bool = False
    yanked_reason: str | None = None

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)


@dataclass(frozen=True)
class WheelTag:
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


@dataclass(frozen=True)
class WheelFile:
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
        self.build_tag = _legacy_build_tag(wheel.build_tag)
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


@dataclass(frozen=True)
class TargetContext:
    platforms: tuple[str, ...] = ()
    implementation: str | None = None
    python_version: str | None = None
    abis: tuple[str, ...] = ()


VERSION_COMPATIBLE = (1, 0)
logger = logging.getLogger(__name__)


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
        parsed_version = Version(version)
    except InvalidVersion:
        return None
    tags = tuple(
        WheelTag(interpreter, abi, platform)
        for interpreter in python_tags.split(".")
        for abi in abi_tags.split(".")
        for platform in platform_tags.split(".")
    )
    if not tags:
        return None
    return WheelFile(
        name=canonicalize_name(distribution),
        version=parsed_version,
        build_tag=build_tag,
        tags=tags,
    )


def parse_wheel_filename(path: str | Path) -> tuple[str, str] | None:
    wheel = parse_wheel_file(path)
    if wheel is None:
        return None
    return wheel.name, str(wheel.version)


def supported_wheel_tags(target: TargetContext | None = None) -> tuple[WheelTag, ...]:
    if target is None:
        major = sys.version_info.major
        minor = sys.version_info.minor
        implementation = "cp"
        version_digits = f"{major}{minor}"
        platform_tags = (sysconfig.get_platform().replace("-", "_").replace(".", "_"),)
        abi_tags = ()
    else:
        version = (
            target.python_version or f"{sys.version_info.major}{sys.version_info.minor}"
        )
        version_digits = version.replace(".", "")
        implementation = target.implementation or "cp"
        platform_tags = target.platforms or (
            sysconfig.get_platform().replace("-", "_").replace(".", "_"),
        )
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


def wheel_tag_rank(
    tags: tuple[WheelTag, ...], supported_tags: tuple[WheelTag, ...] | None = None
) -> int | None:
    supported = supported_wheel_tags() if supported_tags is None else supported_tags
    ranks = [
        index
        for index, supported_tag in enumerate(supported)
        for tag in tags
        if _tag_matches(supported_tag, tag)
    ]
    if not ranks:
        return None
    return min(ranks)


def wheel_candidate(path: str | Path, extras: set[str] | None = None) -> WheelCandidate:
    wheel_path = Path(path)
    parsed = parse_wheel_filename(wheel_path)
    if parsed is None:
        raise InvalidWheelFilename(f"Invalid wheel filename: {wheel_path}")
    name, version = parsed
    metadata = read_wheel_metadata(wheel_path)
    metadata_name = metadata.get("Name") or name
    metadata_version = metadata.get("Version") or version
    dependencies: list[Requirement] = []
    for value in metadata.get_all("Requires-Dist", []):
        req = parse_requirement(value)
        if marker_applies(req.marker, extras=extras or set()):
            dependencies.append(req)
    return WheelCandidate(
        name=metadata_name,
        version=Version(metadata_version),
        path=wheel_path,
        dependencies=tuple(dependencies),
        provided_extras=frozenset(
            value.strip()
            for value in metadata.get_all("Provides-Extra", [])
            if value.strip()
        ),
        requires_python=metadata.get("Requires-Python"),
    )


def read_wheel_metadata(path: str | Path):
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name
            for name in archive.namelist()
            if re.search(r"\.dist-info/METADATA$", name)
        ]
        if not metadata_names:
            raise InstallationError(f"Wheel has no METADATA: {path}")
        parsed = parse_wheel_filename(path)
        if parsed is not None:
            expected = canonicalize_name(parsed[0]).replace("-", "_")
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
        with archive.open(metadata_names[0]) as file:
            try:
                contents = file.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InstallationError(
                    f"Error decoding metadata for {path}: {metadata_names[0]}"
                ) from exc
            return Parser().parsestr(contents)


def wheel_dist_info_dir(source: zipfile.ZipFile, name: str) -> str:
    matches = sorted(
        {
            entry.split("/", 1)[0]
            for entry in source.namelist()
            if entry.endswith(".dist-info/WHEEL") and entry.count("/") == 1
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


def check_compatibility(version: tuple[int, ...], name: str) -> None:
    if version[0] > VERSION_COMPATIBLE[0]:
        raise UnsupportedWheel(
            "{}'s Wheel-Version ({}) is not compatible with this version of pip".format(
                name, ".".join(map(str, version))
            )
        )
    elif version > VERSION_COMPATIBLE:
        logger.warning(
            "Installing from a newer Wheel-Version (%s)",
            ".".join(map(str, version)),
        )


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


def _legacy_build_tag(value: str | None) -> tuple[int, str] | tuple[()]:
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


def _tag_matches(supported: WheelTag, candidate: WheelTag) -> bool:
    return (
        _interpreter_matches(
            supported.interpreter.lower(),
            candidate.interpreter.lower(),
            candidate.abi.lower(),
        )
        and supported.abi.lower() == candidate.abi.lower()
        and _platform_matches(
            supported.platform.lower(),
            candidate.platform.lower(),
        )
    )


def _interpreter_matches(runtime: str, wheel: str, abi: str) -> bool:
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


def _platform_matches(runtime: str, wheel: str) -> bool:
    if runtime == wheel:
        return True
    if runtime == "any" or wheel == "any":
        return runtime == wheel
    if runtime.startswith("macosx_") and wheel.startswith("macosx_"):
        return _macos_platform_matches(runtime, wheel)
    if runtime.startswith("ios_") and wheel.startswith("ios_"):
        return _ios_platform_matches(runtime, wheel)
    if runtime.startswith("android_") and wheel.startswith("android_"):
        return _android_platform_matches(runtime, wheel)
    return False


def _macos_platform_matches(runtime: str, wheel: str) -> bool:
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


def _ios_platform_matches(runtime: str, wheel: str) -> bool:
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


def _android_platform_matches(runtime: str, wheel: str) -> bool:
    runtime_parts = runtime.split("_", 3)
    wheel_parts = wheel.split("_", 3)
    if len(runtime_parts) != 4 or len(wheel_parts) != 4:
        return False
    _, runtime_api, runtime_arch_a, runtime_arch_b = runtime_parts
    _, wheel_api, wheel_arch_a, wheel_arch_b = wheel_parts
    if (runtime_arch_a, runtime_arch_b) != (wheel_arch_a, wheel_arch_b):
        return False
    return int(wheel_api) <= int(runtime_api)
