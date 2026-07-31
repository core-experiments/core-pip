"""Lightweight local pure-wheel resolver used before the full packaging stack."""

from __future__ import annotations

import os
import struct
import sys
from bisect import bisect_left, bisect_right
from typing import TYPE_CHECKING

from pip.index.wheel_metadata import parse_metadata_headers

if TYPE_CHECKING:
    from pip.index.metadata_cache import WheelMetadataCache


class LocalWheelVersion:
    __slots__ = ("release", "text", "_normalized")

    def __init__(self, release: tuple[int, ...], text: str) -> None:
        self.release = release
        self.text = text
        normalized = release
        while len(normalized) > 1 and normalized[-1] == 0:
            normalized = normalized[:-1]
        self._normalized = normalized

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, LocalWheelVersion):
            return NotImplemented
        return self._normalized < other._normalized

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        return not self <= other

    def __ge__(self, other: object) -> bool:
        return not self < other

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LocalWheelVersion) and self._normalized == other._normalized

    def __hash__(self) -> int:
        return hash(self._normalized)

    def __str__(self) -> str:
        return self.text


class LocalWheelSpecifier:
    __slots__ = ("values", "_compatible_upper", "_wildcard_prefix")

    def __init__(self, values: tuple[tuple[str, LocalWheelVersion | str], ...]) -> None:
        self.values = values
        compatible_upper: list[tuple[int, ...] | None] = []
        wildcard_prefix: list[str | None] = []
        for operator, expected in values:
            if operator == "~=" and isinstance(expected, LocalWheelVersion):
                upper = list(expected.release)
                if len(upper) == 1:
                    upper[0] += 1
                else:
                    upper[-2] += 1
                    upper = upper[:-1]
                compatible_upper.append(tuple(upper))
            else:
                compatible_upper.append(None)
            if operator in {"==", "!="} and isinstance(expected, str):
                wildcard_prefix.append(expected[:-2])
            else:
                wildcard_prefix.append(None)
        self._compatible_upper = tuple(compatible_upper)
        self._wildcard_prefix = tuple(wildcard_prefix)

    def contains(self, version: LocalWheelVersion) -> bool:
        for index, (operator, expected) in enumerate(self.values):
            if isinstance(expected, str):
                prefix = self._wildcard_prefix[index]
                assert prefix is not None
                matches = version.text == prefix or version.text.startswith(prefix + ".")
                result = matches if operator == "==" else not matches
            else:
                if operator == "===":
                    result = version.text == expected.text
                elif operator == "==":
                    result = version._normalized == expected._normalized
                elif operator == "!=":
                    result = version._normalized != expected._normalized
                elif operator == ">=":
                    result = version._normalized >= expected._normalized
                elif operator == "<=":
                    result = version._normalized <= expected._normalized
                elif operator == ">":
                    result = version._normalized > expected._normalized
                elif operator == "<":
                    result = version._normalized < expected._normalized
                elif operator == "~=":
                    upper = self._compatible_upper[index]
                    assert upper is not None
                    result = (
                        version._normalized >= expected._normalized
                        and version._normalized < upper
                    )
                else:
                    return False
            if not result:
                return False
        return True


class LocalWheelRequirement:
    __slots__ = (
        "name",
        "specifier",
        "extras",
        "marker",
        "_canonical_name",
        "_marker_value",
        "_marker_expected",
    )

    def __init__(
        self,
        name: str,
        specifier: LocalWheelSpecifier,
        extras: frozenset[str],
        marker: tuple[str, str] | None = None,
    ) -> None:
        self.name = name
        self.specifier = specifier
        self.extras = frozenset(
            item.replace("_", "-").lower() for item in extras
        )
        self.marker = marker
        self._canonical_name = name.replace("_", "-").replace(".", "-").lower()
        if marker is None:
            self._marker_value = ""
            self._marker_expected = frozenset()
        else:
            _, value = marker
            self._marker_value = value.replace("_", "-").lower()
            self._marker_expected = frozenset(
                item.replace("_", "-").lower() for item in value.split(",")
            )

    @property
    def canonical_name(self) -> str:
        return self._canonical_name

    def is_satisfied_by(self, version: LocalWheelVersion) -> bool:
        return self.specifier.contains(version)

    def marker_applies(self, context: frozenset[str] | None = None) -> bool:
        if not self.marker:
            return True
        operator, _ = self.marker
        values = self.extras if context is None else context
        if not values:
            values = {""}
        if operator == "==":
            return self._marker_value in values
        if operator == "!=":
            return self._marker_value not in values
        if operator == "in":
            return bool(values & self._marker_expected)
        return not values & self._marker_expected


class LocalWheelCandidate:
    __slots__ = (
        "name", "version", "path", "dependencies", "provided_extras",
        "requires_python", "source_url", "source_hashes", "source_kind",
        "source_vcs", "from_cache", "yanked_reason",
        "_canonical_name",
    )

    def __init__(
        self,
        name: str,
        version: LocalWheelVersion,
        path: str,
        dependencies: tuple[LocalWheelRequirement, ...],
        provided_extras: frozenset[str] = frozenset(),
        requires_python: str | None = None,
        source_url: str | None = None,
        source_hashes: dict[str, str] | None = None,
        source_kind: str | None = "wheel",
        source_vcs: str | None = None,
        from_cache: bool = False,
        yanked_reason: str | None = None,
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
        self._canonical_name = name.replace("_", "-").replace(".", "-").lower()

    @property
    def canonical_name(self) -> str:
        return self._canonical_name


class LocalWheelPlan:
    __slots__ = ("candidates",)

    def __init__(self, candidates: list[LocalWheelCandidate]) -> None:
        self.candidates = candidates


class WheelhouseUnavailable(Exception):
    pass


class WheelArchive:
    __slots__ = ("file", "members")

    def __init__(self, file, members=None) -> None:
        self.file = file
        self.members: dict[str, tuple[int, int, int, int, int]] = (
            {} if members is None else members
        )
        if members is None:
            self._read_central_directory()

    def _read_central_directory(self) -> None:
        self.file.seek(0, 2)
        size = self.file.tell()
        tail_size = min(size, 22 + 65535)
        self.file.seek(size - tail_size)
        tail = self.file.read(tail_size)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or marker + 22 > len(tail):
            raise WheelhouseUnavailable
        _, _, _, _, entries, directory_size, directory_offset, _ = struct.unpack_from(
            "<4s4H2LH", tail, marker
        )
        if entries == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
            raise WheelhouseUnavailable
        self.file.seek(directory_offset)
        for _ in range(entries):
            header = self.file.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise WheelhouseUnavailable
            (
                _, _, _, flags, compression, _, _, _, compressed_size,
                uncompressed_size, name_size, extra_size, comment_size, _, _, _,
                local_offset,
            ) = struct.unpack("<4s6H3L5H2L", header)
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise WheelhouseUnavailable
            name_bytes = self.file.read(name_size)
            self.file.seek(extra_size + comment_size, 1)
            try:
                name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError as exc:
                raise WheelhouseUnavailable from exc
            self.members[name] = (
                compression,
                int.from_bytes(header[16:20], "little"),
                compressed_size,
                uncompressed_size,
                local_offset,
            )

    def namelist(self) -> list[str]:
        return list(self.members)

    def read(self, name: str) -> bytes:
        try:
            compression, crc, compressed_size, uncompressed_size, local_offset = self.members[name]
        except KeyError as exc:
            raise WheelhouseUnavailable from exc
        self.file.seek(local_offset)
        header = self.file.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise WheelhouseUnavailable
        _, _, _, _, _, _, _, _, _, name_size, extra_size = struct.unpack(
            "<4s5H3L2H", header
        )
        self.file.seek(name_size + extra_size, 1)
        data = self.file.read(compressed_size)
        if len(data) != compressed_size:
            raise WheelhouseUnavailable
        import zlib

        if compression == 0:
            result = data
        elif compression == 8:
            try:
                result = zlib.decompress(data, -15)
            except zlib.error as exc:
                raise WheelhouseUnavailable from exc
        else:
            raise WheelhouseUnavailable
        if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
            raise WheelhouseUnavailable
        return result

    def read_many(self, names: list[str]) -> list[bytes]:
        """Read members in archive order while returning the requested order."""
        ordered = sorted(
            ((self.members[name][4], name) for name in names),
            key=lambda item: item[0],
        )
        results: dict[str, bytes] = {}
        position = -1
        import zlib

        for local_offset, name in ordered:
            compression, crc, compressed_size, uncompressed_size, _ = self.members[name]
            if local_offset != position:
                self.file.seek(local_offset)
            header = self.file.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            (
                _, _, _, _, _, _, _, _, _, name_size, extra_size
            ) = struct.unpack("<4s5H3L2H", header)
            self.file.seek(name_size + extra_size, 1)
            data = self.file.read(compressed_size)
            if len(data) != compressed_size:
                raise WheelhouseUnavailable
            if compression == 0:
                result = data
            elif compression == 8:
                try:
                    result = zlib.decompress(data, -15)
                except zlib.error as exc:
                    raise WheelhouseUnavailable from exc
            else:
                raise WheelhouseUnavailable
            if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
                raise WheelhouseUnavailable
            results[name] = result
            position = local_offset + 30 + name_size + extra_size + compressed_size
        return [results[name] for name in names]


_URL_SAFE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/:"
)
MetadataHeaders = dict[str, list[str]]
CachedValue = tuple[str, str, bool]
CachedDependency = tuple[
    str,
    tuple[CachedValue, ...],
    tuple[str, ...],
    tuple[str, str] | None,
]
CachedMetadata = tuple[
    str,
    str,
    tuple[CachedDependency, ...],
    tuple[str, ...],
    str | None,
]
MetadataCache = dict[str, tuple[int, int, MetadataHeaders | CachedMetadata]]
CatalogRecords = dict[str, list[tuple[str, LocalWheelVersion]]]
Domain = int
RangeIndex = tuple[
    tuple[tuple[int, ...], ...],
    tuple[tuple[str, LocalWheelVersion], ...],
]
DomainCache = dict[
    tuple[str, tuple[tuple[str, LocalWheelVersion | str], ...]], Domain
]
PreflightCache = dict[tuple[str, frozenset[str]], bool]
_CATALOG_CACHE_VERSION = 1


def _cache_metadata(candidate: LocalWheelCandidate) -> CachedMetadata:
    dependencies = tuple(
        (
            dependency.name,
            tuple(
                (
                    operator,
                    expected.text if isinstance(expected, LocalWheelVersion) else expected,
                    not isinstance(expected, LocalWheelVersion),
                )
                for operator, expected in dependency.specifier.values
            ),
            tuple(dependency.extras),
            dependency.marker,
        )
        for dependency in candidate.dependencies
    )
    return (
        candidate.name,
        candidate.version.text,
        dependencies,
        tuple(candidate.provided_extras),
        candidate.requires_python,
    )


def _candidate_from_cache(
    path: str,
    filename_name: str,
    filename_version: LocalWheelVersion,
    cached: CachedMetadata,
) -> LocalWheelCandidate | None:
    name, version_text, cached_dependencies, provided_extras, requires_python = cached
    version = (
        filename_version
        if version_text == filename_version.text
        else _version(version_text)
    )
    if version is None or version != filename_version:
        return None
    if name.replace("_", "-").replace(".", "-").lower() != filename_name:
        return None
    dependencies: list[LocalWheelRequirement] = []
    for dependency_name, values, extras, marker in cached_dependencies:
        specifier_values: list[tuple[str, LocalWheelVersion | str]] = []
        for operator, expected, is_prefix in values:
            parsed = expected if is_prefix else _version(expected)
            if parsed is None:
                return None
            specifier_values.append((operator, parsed))
        dependencies.append(
            LocalWheelRequirement(
                dependency_name,
                LocalWheelSpecifier(tuple(specifier_values)),
                frozenset(extras),
                marker,
            )
        )
    return LocalWheelCandidate(
        name=name,
        version=version,
        path=path,
        dependencies=tuple(dependencies),
        provided_extras=frozenset(provided_extras),
        requires_python=requires_python,
    )
def _version(value: str) -> LocalWheelVersion | None:
    text = value.strip()
    if text.startswith("v"):
        text = text[1:]
    if text.endswith(".*"):
        text = text[:-2]
    parts = text.split(".")
    if not text or any(not part.isdigit() for part in parts):
        return None
    return LocalWheelVersion(tuple(int(part) for part in parts), value.strip())


def _requirement(value: str) -> LocalWheelRequirement | None:
    text, separator, marker = value.partition(";")
    text = text.strip()
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
    values: list[tuple[str, LocalWheelVersion | str]] = []
    if rest:
        for part in rest.split(","):
            part = part.strip()
            operator = next(
                (
                    candidate
                    for candidate in ("===", "==", "!=", "~=", "<=", ">=", "<", ">")
                    if part.startswith(candidate)
                ),
                None,
            )
            if operator is None:
                return None
            raw_version = part[len(operator) :].strip()
            if not raw_version:
                return None
            parsed = (
                raw_version
                if raw_version.endswith(".*")
                else _version(raw_version)
            )
            if parsed is None:
                return None
            values.append((operator, parsed))
    parsed_marker: tuple[str, str] | None = None
    if separator:
        marker_text = marker.strip()
        operator = next(
            (
                candidate
                for candidate in ("not in", "==", "!=", "in")
                if candidate in marker_text.lower()
            ),
            None,
        )
        if operator is None:
            return None
        left, value = marker_text.lower().split(operator, 1)
        value = value.strip()
        if left.strip() != "extra" or len(value) < 2 or value[0] not in "'\"" or value[-1] != value[0]:
            return None
        parsed_marker = (operator, value[1:-1])
    return LocalWheelRequirement(
        name=name,
        specifier=LocalWheelSpecifier(tuple(values)),
        extras=extras,
        marker=parsed_marker,
    )


def _headers(archive: WheelArchive, member: str) -> dict[str, list[str]] | None:
    try:
        text = archive.read(member).decode("utf-8")
    except (UnicodeDecodeError, WheelhouseUnavailable):
        return None
    return parse_metadata_headers(text)


def _wheel_name(path: str) -> tuple[str, LocalWheelVersion] | None:
    filename = os.path.basename(path)
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
    if (
        not name
        or abi != "none"
        or platform != "any"
        or not {
            f"py{sys.version_info.major}",
            f"py{sys.version_info.major}{sys.version_info.minor}",
        }.intersection(python_tags.split("."))
    ):
        return None
    parsed = _version(version)
    return (name.replace("_", "-").replace(".", "-").lower(), parsed) if parsed else None


def _read_wheel_metadata(path: str) -> dict[str, list[str]]:
    try:
        with open(path, "rb") as file:
            archive = WheelArchive(file)
            metadata_paths = sorted(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            )
            if len(metadata_paths) != 1:
                raise WheelhouseUnavailable
            dist_info = metadata_paths[0].partition("/")[0]
            wheel = archive.read(f"{dist_info}/WHEEL").decode("utf-8")
            if not any(line.lower().startswith("wheel-version:") for line in wheel.splitlines()):
                raise WheelhouseUnavailable
            metadata = _headers(archive, metadata_paths[0])
            if metadata is None:
                raise WheelhouseUnavailable
            return metadata
    except (OSError, UnicodeDecodeError, struct.error) as exc:
        raise WheelhouseUnavailable from exc


def _load(
    path: str,
    metadata_cache: MetadataCache | None = None,
    parsed: tuple[str, LocalWheelVersion] | None = None,
    persistent_cache: WheelMetadataCache | None = None,
) -> LocalWheelCandidate:
    parsed = parsed or _wheel_name(path)
    if parsed is None:
        raise WheelhouseUnavailable
    filename_name, filename_version = parsed
    metadata = None
    cache_key = os.path.abspath(path)
    if metadata_cache is not None:
        try:
            stat = os.stat(path)
            entry = metadata_cache.get(cache_key)
            if (
                isinstance(entry, tuple)
                and len(entry) == 3
                and entry[0] == stat.st_mtime_ns
                and entry[1] == stat.st_size
            ):
                cached = entry[2]
                if isinstance(cached, tuple):
                    candidate = _candidate_from_cache(
                        path, filename_name, filename_version, cached
                    )
                    if candidate is not None:
                        return candidate
                elif isinstance(cached, dict):
                    metadata = cached
        except OSError as exc:
            raise WheelhouseUnavailable from exc
    if metadata is None and persistent_cache is not None:
        from pip.index.metadata_cache import metadata_identity

        identity = metadata_identity(path)
        if identity is not None:
            metadata = persistent_cache.get(identity)
    if metadata is None:
        metadata = _read_wheel_metadata(path)
        if persistent_cache is not None:
            from pip.index.metadata_cache import metadata_identity

            identity = metadata_identity(path)
            if identity is not None:
                persistent_cache.put(identity, metadata)
    name = metadata.get("name", [filename_name])[0]
    version = _version(metadata.get("version", [str(filename_version)])[0])
    if version is None or version != filename_version:
        raise WheelhouseUnavailable
    if name.replace("_", "-").replace(".", "-").lower() != filename_name:
        raise WheelhouseUnavailable
    dependencies = tuple(
        requirement
        for value in metadata.get("requires-dist", ())
        if (requirement := _requirement(value)) is not None
    )
    if len(dependencies) != len(metadata.get("requires-dist", ())):
        raise WheelhouseUnavailable
    candidate = LocalWheelCandidate(
        name=name,
        version=version,
        path=path,
        dependencies=dependencies,
        provided_extras=frozenset(metadata.get("provides-extra", ())),
        requires_python=(metadata.get("requires-python") or [None])[0],
    )
    if metadata_cache is not None:
        metadata_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, _cache_metadata(candidate))
    return candidate


def _quote_path(path: str) -> str:
    if os.name == "nt":
        path = "/" + path.replace("\\", "/").lstrip("/")
    return "".join(
        chr(byte) if byte in _URL_SAFE else f"%{byte:02X}"
        for byte in path.encode("utf-8")
    )


def _preflight_exact_dependencies(
    candidate: LocalWheelCandidate,
    exact_index: dict[
        str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]
    ],
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
    preflight_cache: PreflightCache,
    loaded: dict[str, LocalWheelCandidate | None],
    metadata_cache: MetadataCache | None,
    persistent_cache: WheelMetadataCache | None,
    extras: frozenset[str],
) -> bool:
    """Reject exact dependency fan-outs with an impossible shared domain.

    This is deliberately conservative: if any dependency is not a unique
    exact candidate, the normal resolver remains authoritative.
    """
    cache_key = (candidate.path, extras)
    cached = preflight_cache.get(cache_key)
    if cached is not None:
        return cached

    def finish(result: bool) -> bool:
        preflight_cache[cache_key] = result
        return result

    children: list[tuple[LocalWheelCandidate, frozenset[str]]] = []
    for dependency in candidate.dependencies:
        if not dependency.marker_applies(extras):
            continue
        values = dependency.specifier.values
        if (
            len(values) != 1
            or values[0][0] != "=="
            or not isinstance(values[0][1], LocalWheelVersion)
        ):
            return finish(True)
        entries = exact_index.get(dependency.canonical_name, {}).get(
            values[0][1]._normalized, []
        )
        if len(entries) != 1:
            return finish(True)
        path, version = entries[0]
        if path not in loaded:
            try:
                loaded[path] = _load(
                    path,
                    metadata_cache,
                    (dependency.canonical_name, version),
                    persistent_cache,
                )
            except WheelhouseUnavailable:
                loaded[path] = None
        child = loaded[path]
        if child is None:
            return finish(False)
        children.append((child, dependency.extras))

    emitted: dict[str, list[LocalWheelRequirement]] = {}
    for child, child_extras in children:
        for dependency in child.dependencies:
            if dependency.marker_applies(child_extras):
                emitted.setdefault(dependency.canonical_name, []).append(dependency)
    for name, constraints in emitted.items():
        domain: Domain | None = None
        for constraint in constraints:
            matching = _matching_domain(
                name, constraint, exact_masks, range_index, matching_domains
            )
            domain = matching if domain is None else domain & matching
            if not domain:
                break
        if not domain:
            return finish(False)
    return finish(True)


def _matching_domain(
    name: str,
    requirement: LocalWheelRequirement,
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
) -> Domain:
    cache_key = (name, requirement.specifier.values)
    cached = matching_domains.get(cache_key)
    if cached is not None:
        return cached
    range_values = range_index.get(name)
    if range_values is None:
        matching_domains[cache_key] = 0
        return 0
    keys, values = range_values
    operator_values = requirement.specifier.values
    if (
        len(operator_values) == 1
        and operator_values[0][0] == "=="
        and isinstance(operator_values[0][1], LocalWheelVersion)
    ):
        domain = exact_masks.get(name, {}).get(
            operator_values[0][1]._normalized, 0
        )
        matching_domains[cache_key] = domain
        return domain
    all_versions = (1 << len(values)) - 1
    domain = all_versions
    for index, (operator, expected) in enumerate(requirement.specifier.values):
        if isinstance(expected, LocalWheelVersion):
            if operator == "==":
                matching = exact_masks.get(name, {}).get(expected._normalized, 0)
            elif operator == "!=":
                matching = all_versions & ~exact_masks.get(name, {}).get(
                    expected._normalized, 0
                )
            elif operator == "===":
                matching = 0
                for index, (_, version) in enumerate(values):
                    if version.text == expected.text:
                        matching |= 1 << index
            elif operator in {"<", "<=", ">", ">=", "~="}:
                if operator == ">=":
                    start, end = bisect_left(keys, expected._normalized), len(values)
                elif operator == ">":
                    start, end = bisect_right(keys, expected._normalized), len(values)
                elif operator == "<=":
                    start, end = 0, bisect_right(keys, expected._normalized)
                elif operator == "<":
                    start, end = 0, bisect_left(keys, expected._normalized)
                else:
                    upper = requirement.specifier._compatible_upper[index]
                    assert upper is not None
                    start = bisect_left(keys, expected._normalized)
                    end = bisect_left(keys, upper)
                matching = ((1 << end) - 1) ^ ((1 << start) - 1)
            else:
                matching = 0
        elif isinstance(expected, str) and operator in {"==", "!="}:
            prefix = expected[:-2]
            matching = 0
            for index, (_, version) in enumerate(values):
                matches = version.text == prefix or version.text.startswith(prefix + ".")
                if matches == (operator == "=="):
                    matching |= 1 << index
        else:
            matching = 0
        domain &= matching
        if not domain:
            matching_domains[cache_key] = 0
            return 0
    matching_domains[cache_key] = domain
    return domain


def _load_metadata_cache(cache_dir: str | None = None) -> tuple[str | None, MetadataCache | None]:
    root = cache_dir if cache_dir is not None else os.environ.get("PIP_CACHE_DIR")
    if not root:
        return None, None
    path = os.path.join(root, "fast-lock-metadata-v1.marshal")
    try:
        import marshal

        with open(path, "rb") as file:
            cache = marshal.load(file)
        if not isinstance(cache, dict):
            cache = {}
    except (EOFError, OSError, TypeError, ValueError):
        cache = {}
    return path, cache


def _save_metadata_cache(path: str | None, cache: MetadataCache | None) -> None:
    if path is None or cache is None:
        return
    try:
        import marshal

        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "wb") as file:
            marshal.dump(cache, file)
        os.replace(temporary, path)
    except OSError:
        pass


def _cache_root(cache_dir: str | None) -> str | None:
    return cache_dir if cache_dir is not None else os.environ.get("PIP_CACHE_DIR")


def _source_signatures(
    find_links: list[str],
) -> tuple[tuple[str, str, int, int, int], ...] | None:
    signatures: list[tuple[str, str, int, int, int]] = []
    for value in find_links:
        path = os.path.abspath(value)
        try:
            stat = os.stat(path)
        except OSError:
            return None
        kind = "directory" if os.path.isdir(path) else "file"
        signatures.append((kind, path, stat.st_mtime_ns, stat.st_size, stat.st_ino))
    return tuple(signatures)


def _path_belongs_to_source(path: str, find_links: list[str]) -> bool:
    candidate = os.path.realpath(path)
    for value in find_links:
        source = os.path.realpath(value)
        if os.path.isdir(source):
            try:
                if os.path.commonpath((candidate, source)) == source:
                    return True
            except ValueError:
                continue
        elif candidate == source:
            return True
    return False


def _catalog_cache_path(cache_dir: str | None) -> str | None:
    root = _cache_root(cache_dir)
    if root is None:
        return None
    return os.path.join(root, "fast-wheelhouse-catalog-v1.marshal")


def _load_catalog(
    cache_dir: str | None,
    find_links: list[str],
) -> tuple[str | None, CatalogRecords | None]:
    path = _catalog_cache_path(cache_dir)
    signatures = _source_signatures(find_links)
    if path is None or signatures is None:
        return path, None
    try:
        import marshal

        with open(path, "rb") as file:
            payload = marshal.load(file)
    except (EOFError, OSError, TypeError, ValueError):
        return path, None
    if not isinstance(payload, dict):
        return path, None
    if payload.get("version") != _CATALOG_CACHE_VERSION:
        return path, None
    if payload.get("sources") != signatures:
        return path, None
    raw_records = payload.get("records")
    if not isinstance(raw_records, dict):
        return path, None
    records: CatalogRecords = {}
    for name, raw_values in raw_records.items():
        if not isinstance(name, str) or not isinstance(raw_values, list):
            return path, None
        values: list[tuple[str, LocalWheelVersion]] = []
        for raw_value in raw_values:
            if (
                not isinstance(raw_value, tuple)
                or len(raw_value) != 2
                or not isinstance(raw_value[0], str)
                or not isinstance(raw_value[1], str)
                or not raw_value[1]
                or not raw_value[0].endswith(".whl")
                or not _path_belongs_to_source(raw_value[0], find_links)
                or not os.path.isfile(raw_value[0])
            ):
                return path, None
            version = _version(raw_value[1])
            if version is None:
                return path, None
            values.append((raw_value[0], version))
        records[name] = values
    return path, records


def _save_catalog(
    path: str | None,
    find_links: list[str],
    records: CatalogRecords,
) -> None:
    if path is None:
        return
    signatures = _source_signatures(find_links)
    if signatures is None:
        return
    payload = {
        "version": _CATALOG_CACHE_VERSION,
        "sources": signatures,
        "records": {
            name: [(candidate_path, version.text) for candidate_path, version in values]
            for name, values in records.items()
        },
    }
    try:
        import marshal

        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "wb") as file:
            marshal.dump(payload, file)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        pass


def _scan_catalog(find_links: list[str]) -> CatalogRecords | None:
    paths: list[str] = []
    for value in find_links:
        if os.path.isdir(value):
            with os.scandir(value) as entries:
                paths.extend(
                    os.path.abspath(entry.path)
                    for entry in entries
                    if entry.is_file()
                )
        elif os.path.isfile(value):
            paths.append(os.path.abspath(value))
        else:
            return None
    records: CatalogRecords = {}
    for path in paths:
        if not path.endswith(".whl"):
            continue
        parsed = _wheel_name(path)
        if parsed is None:
            return None
        records.setdefault(parsed[0], []).append((path, parsed[1]))
    if not records:
        return None
    for values in records.values():
        values.sort(key=lambda item: item[1], reverse=True)
    return records


def resolve(
    find_links: list[str], values: list[str], *, cache_dir: str | None = None
) -> LocalWheelPlan | None:
    requirements: list[LocalWheelRequirement] = []
    for value in values:
        requirement = _requirement(value)
        if requirement is None or not requirement.marker_applies():
            return None
        requirements.append(requirement)
    catalog_path, records = _load_catalog(cache_dir, find_links)
    if records is None:
        records = _scan_catalog(find_links)
        if records is None:
            return None
        _save_catalog(catalog_path, find_links, records)
    exact_index: dict[
        str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]
    ] = {}
    exact_masks: dict[str, dict[tuple[int, ...], Domain]] = {}
    range_index: dict[str, RangeIndex] = {}
    for name, values_for_name in records.items():
        versions = exact_index.setdefault(name, {})
        masks = exact_masks.setdefault(name, {})
        for item in values_for_name:
            versions.setdefault(item[1]._normalized, []).append(item)
        ordered = sorted(
            (item[1]._normalized, item) for item in values_for_name
        )
        for index, (key, item) in enumerate(ordered):
            masks[key] = masks.get(key, 0) | (1 << index)
        range_index[name] = (
            tuple(key for key, _ in ordered),
            tuple(item for _, item in ordered),
        )
    loaded: dict[str, LocalWheelCandidate | None] = {}
    matching_domains: DomainCache = {}
    preflight_cache: PreflightCache = {}
    current_python = LocalWheelVersion(
        tuple(sys.version_info[:3]),
        ".".join(str(part) for part in sys.version_info[:3]),
    )
    cache_path, metadata_cache = _load_metadata_cache(cache_dir)
    persistent_cache: WheelMetadataCache | None = None
    if cache_dir is not None:
        from pip.index.metadata_cache import get_wheel_metadata_cache

        persistent_cache = get_wheel_metadata_cache(cache_dir)
    try:
        selected = _search(
            records,
            requirements,
            {},
            {},
            {},
            exact_index,
            exact_masks,
            range_index,
            matching_domains,
            preflight_cache,
            current_python,
            loaded,
            metadata_cache,
            persistent_cache,
            [],
            [],
        )
    finally:
        _save_metadata_cache(cache_path, metadata_cache)
    if selected is None:
        return None
    for candidate in selected.values():
        candidate.source_url = "file://" + _quote_path(os.path.abspath(candidate.path))
    return LocalWheelPlan(list(selected.values()))


def _search(
    records: dict[str, list[tuple[str, LocalWheelVersion]]],
    pending: list[LocalWheelRequirement],
    selected: dict[str, LocalWheelCandidate],
    constraints: dict[str, list[LocalWheelRequirement]],
    domains: dict[str, Domain],
    exact_index: dict[
        str, dict[tuple[int, ...], list[tuple[str, LocalWheelVersion]]]
    ],
    exact_masks: dict[str, dict[tuple[int, ...], Domain]],
    range_index: dict[str, RangeIndex],
    matching_domains: DomainCache,
    preflight_cache: PreflightCache,
    current_python: LocalWheelVersion,
    loaded: dict[str, LocalWheelCandidate | None],
    metadata_cache: MetadataCache | None,
    persistent_cache: WheelMetadataCache | None,
    trail: list[tuple[str, LocalWheelRequirement]],
    domain_trail: list[tuple[str, Domain | None]],
) -> dict[str, LocalWheelCandidate] | None:
    checkpoint = len(trail)
    domain_checkpoint = len(domain_trail)

    def rollback() -> None:
        while len(domain_trail) > domain_checkpoint:
            domain_name, previous = domain_trail.pop()
            if previous is None:
                del domains[domain_name]
            else:
                domains[domain_name] = previous
        while len(trail) > checkpoint:
            constraint_name, _ = trail.pop()
            values = constraints[constraint_name]
            values.pop()
            if not values:
                del constraints[constraint_name]

    while pending:
        requirement = pending.pop()
        if not requirement.marker_applies():
            continue
        name = requirement.canonical_name
        package_constraints = constraints.setdefault(name, [])
        package_constraints.append(requirement)
        trail.append((name, requirement))
        previous_domain = domains.get(name)
        domain = _matching_domain(
            name, requirement, exact_masks, range_index, matching_domains
        )
        if previous_domain is not None:
            domain &= previous_domain
        values = range_index.get(name, ((), ()))[1]
        domains[name] = domain
        domain_trail.append((name, previous_domain))
        existing = selected.get(name)
        if existing is not None:
            if not all(
                constraint.is_satisfied_by(existing.version)
                for constraint in package_constraints
            ):
                rollback()
                return None
            continue
        if not domain:
            rollback()
            return None
        while domain:
            index = domain.bit_length() - 1
            domain &= ~(1 << index)
            path, version = values[index]
            if path not in loaded:
                try:
                    loaded[path] = _load(
                        path, metadata_cache, (name, version), persistent_cache
                    )
                except WheelhouseUnavailable:
                    loaded[path] = None
            candidate = loaded[path]
            if candidate is None:
                continue
            if candidate.requires_python:
                python_requirement = _requirement(
                    f"python{candidate.requires_python}"
                )
                if python_requirement is None:
                    raise WheelhouseUnavailable
                if not python_requirement.specifier.contains(current_python):
                    continue
            dependencies = [
                LocalWheelRequirement(
                    dependency.name,
                    dependency.specifier,
                    dependency.extras,
                    None,
                )
                for dependency in candidate.dependencies
                if dependency.marker_applies(requirement.extras)
            ]
            if not _preflight_exact_dependencies(
                candidate,
                exact_index,
                exact_masks,
                range_index,
                matching_domains,
                preflight_cache,
                loaded,
                metadata_cache,
                persistent_cache,
                requirement.extras,
            ):
                continue
            selected[name] = candidate
            result = _search(
                records,
                [*pending, *dependencies],
                selected,
                constraints,
                domains,
                exact_index,
                exact_masks,
                range_index,
                matching_domains,
                preflight_cache,
                current_python,
                loaded,
                metadata_cache,
                persistent_cache,
                trail,
                domain_trail,
            )
            if result is not None:
                return result
            selected.pop(name, None)
        rollback()
        return None
    return selected
