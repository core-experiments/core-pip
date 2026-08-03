"""Wheel filename and metadata loading for the fast wheelhouse path."""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import quote

from cpip.core.python import CURRENT_PYTHON_FULL_TAG, CURRENT_PYTHON_MAJOR_TAG
from cpip.core.wheel_metadata import (
    metadata_paths,
    parse_metadata_headers,
    parse_metadata_member,
)
from cpip.resolution.engine.sources.wheelhouse.archive import (
    CENTRAL_DIRECTORY_HEADER,
    END_OF_CENTRAL_DIRECTORY,
    LOCAL_FILE_HEADER,
    WheelArchive,
    WheelhouseUnavailable,
)
from cpip.resolution.engine.sources.wheelhouse.cache import (
    CachedCandidateParts,
    CachedMetadata,
    MetadataCache,
    cache_candidate,
    cache_metadata,
    candidate_cache,
    artifact_identity_cache,
    metadata_cache_dirty,
    metadata_cache_paths,
)
from cpip.resolution.engine.sources.wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelRequirement,
    LocalWheelSpecifier,
    LocalWheelVersion,
    canonicalize_name,
)

if TYPE_CHECKING:
    from cpip.index.metadata_cache import WheelMetadataCache

SMALL_WHEEL_SIZE = 64 * 1024
SPECIFIER_OPERATORS = ("===", "==", "!=", "~=", "<=", ">=", "<", ">")
MARKER_OPERATORS = ("not in", "==", "!=", "in")


def cached_candidate_parts(cached: CachedMetadata) -> CachedCandidateParts | None:
    name, version_text, cached_dependencies, provided_extras, requires_python = cached
    version = parse_version(version_text)
    if version is None:
        return None
    dependencies: list[LocalWheelRequirement] = []
    for dependency_name, values, extras, marker in cached_dependencies:
        specifier_values: list[tuple[str, LocalWheelVersion | str]] = []
        for operator, expected, is_prefix in values:
            parsed = expected if is_prefix else parse_version(expected)
            if parsed is None:
                return None
            specifier_values.append((operator, parsed))
        dependencies.append(
            LocalWheelRequirement(
                dependency_name,
                LocalWheelSpecifier(tuple(specifier_values)),
                frozenset(extras),
                marker,
                _normalized_extras=True,
            ),
        )
    return (
        name,
        version,
        tuple(dependencies),
        frozenset(provided_extras),
        requires_python,
    )


@lru_cache(maxsize=2048)
def candidate_from_cache(
    path: str,
    filename_name: str,
    filename_version: LocalWheelVersion,
    cached: CachedMetadata,
) -> LocalWheelCandidate | None:
    """Materialize a validated cached candidate.

    The inputs are immutable cache data and the wheel identity. Candidates
    produced here are reused by the fast resolver; resolution only updates
    their derived file URL after selection.
    """
    parts = cached_candidate_parts(cached)
    if parts is None:
        return None
    name, version, dependencies, provided_extras, requires_python = parts
    if version != filename_version:
        return None
    if canonicalize_name(name) != filename_name:
        return None
    return LocalWheelCandidate(
        name=name,
        version=version,
        path=path,
        dependencies=dependencies,
        provided_extras=provided_extras,
        requires_python=requires_python,
    )


@lru_cache(maxsize=2048)
def parse_version(value: str) -> LocalWheelVersion | None:
    text = value.strip()
    display_text = text
    text = text.removeprefix("v")
    text = text.removesuffix(".*")
    parts = text.split(".")
    if not text:
        return None
    for part in parts:
        if not part.isdigit():
            return None
    return LocalWheelVersion(tuple(map(int, parts)), display_text)


@lru_cache(maxsize=4096)
def parse_requirement(value: str) -> LocalWheelRequirement | None:
    text = value.strip()
    if ";" not in text and "[" not in text:
        separator = text.find("==")
        if separator > 0 and "=" not in text[separator + 2 :]:
            name = text[:separator].rstrip()
            valid_name = bool(name)
            for character in name:
                if not (character.isalnum() or character in "._-"):
                    valid_name = False
                    break
            if valid_name:
                raw_version = text[separator + 2 :].strip()
            else:
                raw_version = ""
            if raw_version:
                parsed = (
                    raw_version
                    if raw_version.endswith(".*")
                    else parse_version(raw_version)
                )
                if parsed is not None:
                    return LocalWheelRequirement(
                        name=name,
                        specifier=LocalWheelSpecifier((("==", parsed),)),
                        extras=frozenset(),
                        _normalized_extras=True,
                    )
    text, separator, marker = text.partition(";")
    end = 0
    while end < len(text) and (text[end].isalnum() or text[end] in "._-"):
        end += 1
    if end == 0:
        return None
    name = text[:end]
    rest = text[end:].strip()
    extras: frozenset[str] = frozenset()
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            return None
        extras = frozenset(
            item.replace("_", "-").lower()
            for item in rest[1:end].split(",")
            if item.strip()
        )
        rest = rest[end + 1 :].strip()
    if rest.startswith("@"):
        return None
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()
    if (
        not separator
        and not extras
        and rest.startswith("==")
        and not rest.startswith("===")
        and "," not in rest
    ):
        raw_version = rest[2:].strip()
        if raw_version:
            parsed = (
                raw_version
                if raw_version.endswith(".*")
                else parse_version(raw_version)
            )
            if parsed is not None:
                return LocalWheelRequirement(
                    name=name,
                    specifier=LocalWheelSpecifier((("==", parsed),)),
                    extras=frozenset(),
                    _normalized_extras=True,
                )
    values: list[tuple[str, LocalWheelVersion | str]] = []
    if rest:
        for part in rest.split(","):
            part = part.strip()
            operator = None
            for candidate in SPECIFIER_OPERATORS:
                if part.startswith(candidate):
                    operator = candidate
                    break
            if operator is None:
                return None
            raw_version = part[len(operator) :].strip()
            if not raw_version:
                return None
            parsed = (
                raw_version
                if raw_version.endswith(".*")
                else parse_version(raw_version)
            )
            if parsed is None:
                return None
            values.append((operator, parsed))
    parsed_marker: tuple[str, str] | None = None
    if separator:
        marker_text = marker.strip()
        marker_lower = marker_text.lower()
        operator = None
        for candidate in MARKER_OPERATORS:
            if candidate in marker_lower:
                operator = candidate
                break
        if operator is None:
            return None
        left, value = marker_lower.split(operator, 1)
        value = value.strip()
        if (
            left.strip() != "extra"
            or len(value) < 2
            or value[0] not in "'\""
            or value[-1] != value[0]
        ):
            return None
        parsed_marker = (operator, value[1:-1])
    return LocalWheelRequirement(
        name=name,
        specifier=LocalWheelSpecifier(tuple(values)),
        extras=extras,
        marker=parsed_marker,
        _normalized_extras=True,
    )


def parse_headers(archive: WheelArchive, member: str) -> dict[str, list[str]] | None:
    try:
        return parse_metadata_member(archive.read, member)
    except (UnicodeDecodeError, WheelhouseUnavailable):
        return None


def parse_wheel_filename(filename: str) -> tuple[str, LocalWheelVersion] | None:
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    name, version, python_tags, abi, platform = (
        parts[0],
        parts[1],
        parts[-3],
        parts[-2],
        parts[-1],
    )
    if not name or abi != "none" or platform != "any":
        return None
    if (
        python_tags != CURRENT_PYTHON_MAJOR_TAG
        and python_tags != CURRENT_PYTHON_FULL_TAG
    ):
        tags = python_tags.split(".")
        if CURRENT_PYTHON_MAJOR_TAG not in tags and CURRENT_PYTHON_FULL_TAG not in tags:
            return None
    parsed = parse_version(version)
    return (canonicalize_name(name), parsed) if parsed else None


def wheel_name(path: str) -> tuple[str, LocalWheelVersion] | None:
    return parse_wheel_filename(os.path.basename(path))


def read_wheel_metadata(
    path: str,
    *,
    file_size: int | None = None,
    source_hashes: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    try:
        filename_parts = os.path.basename(path)[:-4].split("-")
        target = (
            f"{filename_parts[0]}-{filename_parts[1]}.dist-info/METADATA"
            if len(filename_parts) >= 2
            else None
        )
        file_descriptor: int | None = None
        file_object = None
        if file_size is None:
            file_descriptor = os.open(path, os.O_RDONLY)
            try:
                file_size = os.fstat(file_descriptor).st_size
            except BaseException:
                os.close(file_descriptor)
                file_descriptor = None
                raise
        elif target is not None and file_size <= SMALL_WHEEL_SIZE:
            file_descriptor = os.open(path, os.O_RDONLY)
        if target is not None and file_size <= SMALL_WHEEL_SIZE:
            assert file_descriptor is not None
            descriptor = file_descriptor
            try:
                contents = os.read(descriptor, file_size)
                if source_hashes is not None and len(contents) == file_size:
                    source_hashes["sha256"] = hashlib.sha256(contents).hexdigest()
                metadata = read_small_wheel_metadata(contents, target)
                if metadata is not None:
                    os.close(descriptor)
                    file_descriptor = None
                    return metadata
                os.lseek(descriptor, 0, os.SEEK_SET)
                file_object = os.fdopen(descriptor, "rb")
                file_descriptor = None
            except BaseException:
                os.close(descriptor)
                file_descriptor = None
                raise
        elif file_descriptor is not None:
            file_object = os.fdopen(file_descriptor, "rb")
            file_descriptor = None
        if file_object is None:
            file_object = open(path, "rb")
        with file_object as file:
            archive = WheelArchive(file, target=target)
            if target is not None and target in archive.members:
                metadata = parse_headers(archive, target)
                if metadata is None:
                    raise WheelhouseUnavailable
                return metadata
            if target is not None:
                archive = WheelArchive(file)
            paths = metadata_paths(archive.namelist())
            if len(paths) != 1:
                raise WheelhouseUnavailable
            metadata_path = paths[0]
            metadata = parse_headers(archive, metadata_path)
            if metadata is None:
                raise WheelhouseUnavailable
            return metadata
    except (OSError, UnicodeDecodeError, struct.error) as exc:
        raise WheelhouseUnavailable from exc


def read_small_wheel_metadata(
    contents: bytes,
    target: str,
) -> dict[str, list[str]] | None:
    """Read one metadata member from a small wheel already held in memory."""
    tail_size = min(len(contents), 22 + 65535)
    tail_start = len(contents) - tail_size
    marker = contents.rfind(b"PK\x05\x06", tail_start)
    if marker < 0 or marker + 22 > len(contents):
        return None
    try:
        (_, _, _, _, entries, directory_size, directory_offset, _) = (
            END_OF_CENTRAL_DIRECTORY.unpack_from(contents, marker)
        )
    except struct.error:
        return None
    if (
        entries == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
        or directory_offset + directory_size > len(contents)
    ):
        return None
    target_bytes = target.encode("utf-8")
    target_size = len(target_bytes)
    offset = directory_offset
    member: tuple[int, int, int, int, int] | None = None
    directory_end = directory_offset + directory_size
    unpack_central_directory = CENTRAL_DIRECTORY_HEADER.unpack_from
    try:
        for _ in range(entries):
            if offset + 46 > directory_end:
                return None
            (
                signature,
                _,
                _,
                flags,
                compression,
                _,
                _,
                crc,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
                comment_size,
                _,
                _,
                _,
                local_offset,
            ) = unpack_central_directory(contents, offset)
            if (
                signature != b"PK\x01\x02"
                or flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                return None
            offset += 46
            name_end = offset + name_size
            entry_end = name_end + extra_size + comment_size
            if entry_end > directory_end:
                return None
            if name_size == target_size and contents.startswith(
                target_bytes,
                offset,
                name_end,
            ):
                member = (
                    compression,
                    crc,
                    compressed_size,
                    uncompressed_size,
                    local_offset,
                )
                break
            offset = entry_end
    except struct.error:
        return None
    if member is None:
        return None
    compression, crc, compressed_size, uncompressed_size, local_offset = member
    try:
        if local_offset + 30 > len(contents):
            return None
        header = LOCAL_FILE_HEADER.unpack_from(contents, local_offset)
        if header[0] != b"PK\x03\x04":
            return None
        name_size, extra_size = header[-2:]
        data_start = local_offset + 30 + name_size + extra_size
        data_end = data_start + compressed_size
        if data_end > len(contents):
            return None
        data = contents[data_start:data_end]
        if compression == 0:
            result = data
        elif compression == 8:
            result = zlib.decompress(data, -15)
        else:
            return None
        if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
            return None
        return parse_metadata_headers(result.decode("utf-8"))
    except (UnicodeDecodeError, struct.error, zlib.error):
        return None


def load_candidate(
    path: str,
    metadata_cache: MetadataCache | None = None,
    parsed: tuple[str, LocalWheelVersion] | None = None,
    persistent_cache: WheelMetadataCache | None = None,
    *,
    path_is_absolute: bool = False,
    compute_source_hashes: bool = False,
) -> LocalWheelCandidate:
    parsed = parsed or wheel_name(path)
    if parsed is None:
        raise WheelhouseUnavailable
    filename_name, filename_version = parsed
    metadata = None
    source_hashes: dict[str, str] = {}
    cache_key = (
        path if path_is_absolute or os.path.isabs(path) else os.path.abspath(path)
    )
    file_identity = artifact_identity_cache.get(cache_key)
    identity = None
    if metadata_cache is not None or persistent_cache is not None:
        if file_identity is None:
            try:
                stat = os.stat(path)
            except OSError as exc:
                if metadata_cache is not None:
                    raise WheelhouseUnavailable from exc
            else:
                file_identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                artifact_identity_cache[cache_key] = file_identity
        if file_identity is not None:
            identity = (cache_key, file_identity[1], file_identity[2])
    candidate_key = None
    if file_identity is not None:
        candidate_key = (cache_key, *file_identity)
        cached_candidate = candidate_cache.get(candidate_key)
        if (
            cached_candidate is not None
            and cached_candidate.canonical_name == filename_name
            and cached_candidate.version == filename_version
        ):
            return cached_candidate

    if metadata_cache is not None and file_identity is not None:
        entry = metadata_cache.get(cache_key)
        if (
            isinstance(entry, tuple)
            and len(entry) == 3
            and entry[0] == file_identity[2]
            and entry[1] == file_identity[1]
        ):
            cached = entry[2]
            if isinstance(cached, tuple):
                candidate = candidate_from_cache(
                    path,
                    filename_name,
                    filename_version,
                    cached,
                )
                if candidate is not None:
                    assert candidate_key is not None
                    cache_candidate(candidate_key, candidate)
                    return candidate
            elif isinstance(cached, dict):
                metadata = cached
    if metadata is None and persistent_cache is not None and identity is not None:
        metadata = persistent_cache.get_reference(identity)
    if metadata is None:
        metadata = read_wheel_metadata(
            path,
            file_size=file_identity[1] if file_identity is not None else None,
            source_hashes=source_hashes if compute_source_hashes else None,
        )
        if persistent_cache is not None and identity is not None:
            persistent_cache.put_reference(identity, metadata)
    names = metadata.get("name")
    name = names[0] if names else filename_name
    versions = metadata.get("version")
    metadata_version = versions[0] if versions else str(filename_version)
    version = (
        filename_version
        if metadata_version == filename_version.text
        else parse_version(metadata_version)
    )
    if version is None or version != filename_version:
        raise WheelhouseUnavailable
    if canonicalize_name(name) != filename_name:
        raise WheelhouseUnavailable
    requires_dist = metadata.get("requires-dist", ())
    dependencies = tuple(
        requirement
        for value in requires_dist
        if (requirement := parse_requirement(value)) is not None
    )
    if len(dependencies) != len(requires_dist):
        raise WheelhouseUnavailable
    provided_extras = metadata.get("provides-extra", ())
    requires_python_values = metadata.get("requires-python")
    candidate = LocalWheelCandidate(
        name=name,
        version=version,
        path=path,
        dependencies=dependencies,
        provided_extras=frozenset(provided_extras),
        requires_python=(requires_python_values[0] if requires_python_values else None),
        source_hashes=source_hashes or None,
    )
    if metadata_cache is not None and file_identity is not None:
        metadata_cache[cache_key] = (
            file_identity[2],
            file_identity[1],
            cache_metadata(candidate),
        )
        owner_path = metadata_cache_paths.get(id(metadata_cache))
        if owner_path is not None:
            metadata_cache_dirty.add(owner_path)
    if candidate_key is not None:
        cache_candidate(candidate_key, candidate)
    return candidate


def quote_path(path: str) -> str:
    if os.name == "nt":
        path = "/" + path.replace("\\", "/").lstrip("/")
    return quote(path, safe="/")
