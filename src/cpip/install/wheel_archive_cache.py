"""Versioned cache of unpacked wheels for fresh target installations.



The compressed artifact cache avoids downloads. This cache avoids repeating

ZIP extraction and lets supported filesystems clone immutable wheel trees into

an installation target with copy-on-write semantics.

"""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import marshal
import os
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, Generator

from cpip.core.errors import InstallationError
from cpip.core.packaging import Version, parse_requirement
from cpip.core.wheel import WheelCandidate, parse_wheel
from cpip.install.wheel_archive import (
    copy_member_with_metadata,
    record_metadata_internal,
    validate_member_parts,
    zip_mode,
)
from cpip.install.wheel_scripts import (
    entry_point_scripts,
    generate_entry_point_files,
    rewrite_shebang,
)
from cpip.platform.clone import clone_path
from cpip.resolution.models import ResolutionResult

if TYPE_CHECKING:
    from typing import Protocol, TypeVar

    from cpip.build.metadata import InstalledMetadataDistribution
    from cpip.core.direct_url import DirectUrl
    from cpip.install.target import InstallTarget
    from cpip.install.wheel_state import InstalledWheelDistribution

    class WheelInstallCandidate(Protocol):
        """Read-only candidate boundary required by the archive installer."""

        @property
        def canonical_name(self) -> str: ...

        @property
        def name(self) -> str: ...

        @property
        def path(self) -> str: ...

        @property
        def source_hashes(self) -> dict[str, str] | None: ...

        @property
        def source_kind(self) -> str | None: ...

        @property
        def version(self) -> object: ...

        @property
        def wheel_layout(self) -> object | None: ...

    InstallCandidate = TypeVar("InstallCandidate", bound=WheelInstallCandidate)

    WheelRequest = tuple[str, bool, DirectUrl | None]

else:
    WheelRequest = tuple[str, bool, object | None]


ARCHIVE_CACHE_BUCKET = "archive-v1"

ARCHIVE_CACHE_FORMAT = 1

RESOLUTION_CACHE_BUCKET = "resolution-v2"

RESOLUTION_CACHE_FORMAT = 2

RESOLUTION_CACHE_TTL_SECONDS = 600.0

_LOCK_WAIT_SECONDS = 30.0

_STALE_LOCK_SECONDS = 300.0

_INSTALL_WORKERS = 4


# relative archive path, RECORD hash, RECORD size, source mode

ArchiveEntry = tuple[str, str, str, int]


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


class CachedWheelArchive:
    __slots__ = ("digest", "dist_info", "entries", "tree")

    def __init__(
        self,
        digest: str,
        tree: str,
        dist_info: str,
        entries: tuple[ArchiveEntry, ...],
    ) -> None:
        self.digest = digest

        self.tree = tree

        self.dist_info = dist_info

        self.entries = entries


class _WheelInstallPlan:
    __slots__ = ("archive", "candidate", "direct_url", "requested", "scripts")

    def __init__(
        self,
        archive: CachedWheelArchive,
        candidate: WheelInstallCandidate,
        *,
        requested: bool,
        direct_url: DirectUrl | None,
        scripts: dict[str, tuple[str, bool]],
    ) -> None:
        self.archive = archive

        self.candidate = candidate

        self.requested = requested

        self.direct_url = direct_url

        self.scripts = scripts


class _DestinationNode:
    """Typed prefix tree for detecting colliding wheel destinations."""

    __slots__ = ("children", "owner")

    def __init__(self) -> None:
        self.children: dict[str, _DestinationNode] = {}

        self.owner: int | None = None


def _wheel_digest(candidate: WheelInstallCandidate) -> str:
    supplied = (
        (candidate.source_hashes or {}).get("sha256")
        if candidate.source_kind in {None, "wheel"}
        else None
    )

    if isinstance(supplied, str) and _valid_sha256(supplied):
        return supplied.lower()

    digest = hashlib.sha256()

    with open(candidate.path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)

    return digest.hexdigest()


def _entry_root(cache_dir: str, digest: str) -> str:
    return os.path.join(cache_dir, ARCHIVE_CACHE_BUCKET, digest[:2], digest)


def _valid_archive_entries(entries: object) -> bool:
    return isinstance(entries, tuple) and all(
        isinstance(item, tuple)
        and len(item) == 4
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        and isinstance(item[2], str)
        and isinstance(item[3], int)
        for item in entries
    )


def _load_archive(entry_root: str, digest: str) -> CachedWheelArchive | None:
    tree = os.path.join(entry_root, "tree")

    manifest = os.path.join(entry_root, "manifest.bin")

    if not os.path.isdir(tree) or not os.path.isfile(manifest):
        return None

    try:
        with open(manifest, "rb") as file:
            value = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if not (
        isinstance(value, tuple)
        and len(value) == 4
        and value[0] == ARCHIVE_CACHE_FORMAT
        and value[1] == digest
        and isinstance(value[2], str)
        and isinstance(value[3], tuple)
    ):
        return None

    entries = value[3]

    if not _valid_archive_entries(entries):
        return None

    return CachedWheelArchive(digest, tree, value[2], entries)


def _remove_cache_path(path: str) -> None:
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)

        else:
            shutil.rmtree(path)

    except FileNotFoundError:
        pass


@contextmanager
def _entry_lock(path: str, entry_root: str, digest: str) -> Generator[None, None, None]:
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    descriptor: int | None = None

    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        except FileExistsError:
            if _load_archive(entry_root, digest) is not None:
                # The caller will recheck after entering the no-op lock scope.

                yield

                return

            try:
                stale = time.time() - os.stat(path, follow_symlinks=False).st_mtime

            except FileNotFoundError:
                continue

            if stale > _STALE_LOCK_SECONDS:
                try:
                    os.unlink(path)

                except FileNotFoundError:
                    pass

                continue

            if time.monotonic() >= deadline:
                raise OSError(errno.EBUSY, "timed out waiting for wheel cache", path)

            time.sleep(0.05)

    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))

        yield

    finally:
        os.close(descriptor)

        try:
            os.unlink(path)

        except FileNotFoundError:
            pass


def _record_metadata(
    archive: zipfile.ZipFile, dist_info: str
) -> dict[str, tuple[str, str]]:
    try:
        text = archive.read(f"{dist_info}/RECORD").decode("utf-8")

    except (KeyError, UnicodeDecodeError):
        return {}

    result: dict[str, tuple[str, str]] = {}

    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[1].startswith("sha256=") and row[2].isdigit():
            result[row[0]] = (row[1], row[2])

    return result


def _extract_archive(
    candidate: WheelInstallCandidate,
    digest: str,
    entry_root: str,
) -> CachedWheelArchive:
    shard = os.path.dirname(entry_root)

    temporary = tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=shard)

    tree = os.path.join(temporary, "tree")

    os.mkdir(tree)

    try:
        with zipfile.ZipFile(candidate.path) as archive:
            layout = candidate.wheel_layout

            if isinstance(layout, tuple) and layout and isinstance(layout[0], str):
                dist_info = layout[0]

            else:
                dist_info, _ = parse_wheel(
                    archive,
                    os.path.basename(candidate.path)[:-4].split("-", 1)[0],
                )

            wheel_metadata = _record_metadata(archive, dist_info)

            entries: list[ArchiveEntry] = []

            seen: set[str] = set()

            for member in archive.infolist():
                if member.is_dir():
                    continue

                parts = validate_member_parts(member.filename)

                if not parts:
                    raise InstallationError(
                        f"wheel member has an empty path: {member.filename!r}",
                    )

                relative = "/".join(parts)

                if relative in seen:
                    raise InstallationError(
                        f"Wheel {candidate.path} contains duplicate member {relative!r}",
                    )

                seen.add(relative)

                destination = os.path.join(tree, *parts)

                os.makedirs(os.path.dirname(destination), exist_ok=True)

                metadata = wheel_metadata.get(relative)

                if metadata is not None and metadata[1] != str(member.file_size):
                    metadata = None

                metadata = copy_member_with_metadata(
                    archive,
                    member,
                    destination,
                    metadata=metadata,
                )

                mode = zip_mode(member)

                if mode is not None:
                    os.chmod(destination, mode)

                entries.append(
                    (relative, metadata[0], metadata[1], mode or 0),
                )

        if f"{dist_info}/RECORD" not in seen:
            raise InstallationError(
                f"Wheel {candidate.path} has no valid dist-info metadata",
            )

        manifest = (
            ARCHIVE_CACHE_FORMAT,
            digest,
            dist_info,
            tuple(entries),
        )

        with open(os.path.join(temporary, "manifest.bin"), "wb") as file:
            marshal.dump(manifest, file)

        _remove_cache_path(entry_root)

        os.rename(temporary, entry_root)

        temporary = ""

        loaded = _load_archive(entry_root, digest)

        if loaded is None:
            raise OSError(
                errno.EIO, "failed to publish wheel archive cache", entry_root
            )

        return loaded

    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare_cached_wheel(
    candidate: WheelInstallCandidate,
    cache_dir: str,
) -> CachedWheelArchive:
    layout = candidate.wheel_layout

    if isinstance(layout, CachedWheelArchive):
        return layout

    digest = _wheel_digest(candidate)

    entry_root = _entry_root(cache_dir, digest)

    cached = _load_archive(entry_root, digest)

    if cached is not None:
        return cached

    shard = os.path.dirname(entry_root)

    os.makedirs(shard, exist_ok=True)

    lock = f"{entry_root}.lock"

    with _entry_lock(lock, entry_root, digest):
        cached = _load_archive(entry_root, digest)

        if cached is not None:
            return cached

        return _extract_archive(candidate, digest, entry_root)


def _prepare_cached_wheels(
    candidates: tuple[WheelInstallCandidate, ...],
    cache_dir: str,
) -> tuple[CachedWheelArchive, ...]:
    if len(candidates) < _INSTALL_WORKERS:
        return tuple(
            prepare_cached_wheel(candidate, cache_dir) for candidate in candidates
        )

    with ThreadPoolExecutor(
        max_workers=min(_INSTALL_WORKERS, len(candidates)),
        thread_name_prefix="cpip-archive",
    ) as pool:
        return tuple(
            pool.map(
                lambda candidate: prepare_cached_wheel(candidate, cache_dir),
                candidates,
            ),
        )


def exact_install_plan_key(
    requirements: tuple[object, ...],
    context: tuple[object, ...],
) -> str | None:
    """Return a stable key when every root is an ordinary exact pin."""

    normalized: list[tuple[str, str, tuple[str, ...]]] = []

    seen: set[str] = set()

    for item in requirements:
        requirement = getattr(item, "req", None)

        if (
            requirement is None
            or requirement.url is not None
            or getattr(item, "link", None) is not None
            or getattr(item, "hash_options", None)
            or getattr(item, "config_settings", None)
        ):
            return None

        item_normalized = _normalized_exact_requirement(requirement)

        if item_normalized is None:
            return None

        name = item_normalized[0]

        if name in seen:
            return None

        seen.add(name)

        normalized.append(item_normalized)

    return _exact_install_plan_key(normalized, context)


def exact_install_plan_key_from_strings(
    requirements: tuple[str, ...],
    context: tuple[object, ...],
) -> tuple[str, frozenset[str]] | None:
    """Build the same key from a conservative plain exact-pin command shape."""

    normalized: list[tuple[str, str, tuple[str, ...]]] = []

    seen: set[str] = set()

    try:
        for raw in requirements:
            if ";" in raw or "#" in raw or "\\" in raw:
                return None

            requirement = parse_requirement(raw)

            item_normalized = _normalized_exact_requirement(requirement)

            if item_normalized is None or item_normalized[0] in seen:
                return None

            seen.add(item_normalized[0])

            normalized.append(item_normalized)

    except (TypeError, ValueError):
        return None

    key = _exact_install_plan_key(normalized, context)

    return None if key is None else (key, frozenset(seen))


def _normalized_exact_requirement(
    requirement: object,
) -> tuple[str, str, tuple[str, ...]] | None:
    if getattr(requirement, "url", None) is not None:
        return None

    specifier = getattr(requirement, "specifier", None)

    specifiers = getattr(specifier, "specifiers", ())

    if (
        len(specifiers) != 1
        or specifiers[0].operator != "=="
        or specifiers[0].version.endswith(".*")
    ):
        return None

    canonical_name = getattr(requirement, "canonical_name", None)

    extras = getattr(requirement, "extras", ())

    if not isinstance(canonical_name, str) or not isinstance(
        extras,
        (frozenset, list, set, tuple),
    ):
        return None

    if not all(isinstance(extra, str) for extra in extras):
        return None

    return (
        canonical_name,
        str(Version(specifiers[0].version)),
        tuple(sorted(extra for extra in extras if isinstance(extra, str))),
    )


def _exact_install_plan_key(
    normalized: list[tuple[str, str, tuple[str, ...]]],
    context: tuple[object, ...],
) -> str | None:
    if not normalized:
        return None

    payload = json.dumps(
        (RESOLUTION_CACHE_FORMAT, tuple(sorted(normalized)), context),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")

    return hashlib.sha256(payload).hexdigest()


def _resolution_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, RESOLUTION_CACHE_BUCKET, key[:2], f"{key}.bin")


def save_cached_install_plan(
    cache_dir: str,
    key: str,
    candidates: tuple[WheelCandidate, ...],
    graph: object,
) -> bool:
    """Publish a short-lived plan receipt after a successful installation."""

    if not _valid_sha256(key) or not candidates:
        return False

    records = []

    try:
        for candidate in candidates:
            if candidate.source_kind != "wheel":
                return False

            digest = _wheel_digest(candidate)

            archive = _load_archive(_entry_root(cache_dir, digest), digest)

            if archive is None:
                return False

            source_hashes = dict(candidate.source_hashes or {})

            source_hashes["sha256"] = digest

            records.append(
                (
                    candidate.name,
                    str(candidate.version),
                    digest,
                    tuple(str(dependency) for dependency in candidate.dependencies),
                    tuple(sorted(candidate.provided_extras)),
                    candidate.requires_python,
                    candidate.source_url,
                    tuple(sorted(source_hashes.items())),
                    candidate.source_kind,
                    candidate.source_vcs,
                    candidate.yanked_reason,
                    archive.dist_info,
                    archive.entries,
                ),
            )

        graph_items = tuple(
            sorted(
                (str(name), tuple(sorted(str(child) for child in children)))
                for name, children in getattr(graph, "items", lambda: ())()
            ),
        )

        value = (
            RESOLUTION_CACHE_FORMAT,
            time.time(),
            key,
            tuple(records),
            graph_items,
        )

        path = _resolution_path(cache_dir, key)

        directory = os.path.dirname(path)

        os.makedirs(directory, exist_ok=True)

        descriptor, temporary = tempfile.mkstemp(prefix=f".{key[:12]}-", dir=directory)

        try:
            with os.fdopen(descriptor, "wb") as file:
                marshal.dump(value, file)

            os.replace(temporary, path)

        except BaseException:
            try:
                os.unlink(temporary)

            except FileNotFoundError:
                pass

            raise

    except (OSError, TypeError, ValueError):
        return False

    return True


def load_cached_install_plan(
    cache_dir: str,
    key: str,
) -> ResolutionResult | None:
    """Load and validate a fresh plan receipt and all referenced archives."""

    if not _valid_sha256(key):
        return None

    path = _resolution_path(cache_dir, key)

    try:
        if time.time() - os.stat(path, follow_symlinks=False).st_mtime > (
            RESOLUTION_CACHE_TTL_SECONDS
        ):
            return None

        with open(path, "rb") as file:
            value = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if not (
        isinstance(value, tuple)
        and len(value) == 5
        and value[0] == RESOLUTION_CACHE_FORMAT
        and isinstance(value[1], float)
        and value[2] == key
        and isinstance(value[3], tuple)
        and isinstance(value[4], tuple)
    ):
        return None

    if time.time() - value[1] > RESOLUTION_CACHE_TTL_SECONDS:
        return None

    candidates: list[WheelCandidate] = []

    try:
        for record in value[3]:
            if not (
                isinstance(record, tuple)
                and len(record) == 13
                and isinstance(record[0], str)
                and isinstance(record[1], str)
                and isinstance(record[2], str)
                and _valid_sha256(record[2])
                and isinstance(record[3], tuple)
                and all(isinstance(item, str) for item in record[3])
                and isinstance(record[4], tuple)
                and all(isinstance(item, str) for item in record[4])
                and (record[5] is None or isinstance(record[5], str))
                and (record[6] is None or isinstance(record[6], str))
                and isinstance(record[7], tuple)
                and all(
                    isinstance(item, tuple)
                    and len(item) == 2
                    and all(isinstance(part, str) for part in item)
                    for item in record[7]
                )
                and record[8] == "wheel"
                and (record[9] is None or isinstance(record[9], str))
                and (record[10] is None or isinstance(record[10], str))
                and isinstance(record[11], str)
                and _valid_archive_entries(record[12])
            ):
                return None

            tree = os.path.join(
                _entry_root(cache_dir, record[2]),
                "tree",
            )

            if not os.path.isdir(tree):
                return None

            archive = CachedWheelArchive(
                record[2],
                tree,
                record[11],
                record[12],
            )

            candidates.append(
                WheelCandidate(
                    name=record[0],
                    version=Version(record[1]),
                    path=archive.tree,
                    dependencies=tuple(parse_requirement(item) for item in record[3]),
                    provided_extras=frozenset(
                        item for item in record[4] if isinstance(item, str)
                    ),
                    requires_python=record[5],
                    source_url=record[6],
                    source_hashes=dict(record[7]),
                    source_kind=record[8],
                    source_vcs=record[9],
                    from_cache=True,
                    yanked_reason=record[10],
                    wheel_layout=archive,
                ),
            )

        graph: dict[str, set[str]] = {}

        for graph_record in value[4]:
            if not (
                isinstance(graph_record, tuple)
                and len(graph_record) == 2
                and isinstance(graph_record[0], str)
                and isinstance(graph_record[1], tuple)
                and all(isinstance(child, str) for child in graph_record[1])
            ):
                return None

            graph[graph_record[0]] = {
                child for child in graph_record[1] if isinstance(child, str)
            }

    except (TypeError, ValueError):
        return None

    if len(graph) != len(value[4]):
        return None

    selected = {candidate.canonical_name: candidate for candidate in candidates}

    if len(selected) != len(candidates):
        return None

    for candidate in candidates:
        for dependency in candidate.dependencies:
            selected_dependency = selected.get(dependency.canonical_name)

            if selected_dependency is None or not dependency.is_satisfied_by(
                selected_dependency.version,
            ):
                return None

    return ResolutionResult(
        candidates=tuple(candidates),
        graph={name: frozenset(children) for name, children in graph.items()},
        metrics=MappingProxyType({"warm_resolution_cache_hit": 1}),
    )


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def _internal_comparison_path(path: str) -> str:
    """Normalize a validated target path without resolving every parent."""

    return os.path.normcase(os.path.normpath(path))


def _eligible_target(target: InstallTarget, cache_dir: str) -> str | None:
    root = _normalized_path(target.purelib)

    if os.path.lexists(root) and (not os.path.isdir(root) or os.path.islink(root)):
        return None

    if any(
        _normalized_path(path) != root
        for path in (target.platlib, target.headers, target.data)
    ):
        return None

    expected_scripts = _normalized_path(
        os.path.join(root, "Scripts" if os.name == "nt" else "bin"),
    )

    if _normalized_path(target.scripts) != expected_scripts:
        return None

    cache = _normalized_path(cache_dir)

    try:
        if os.path.commonpath((cache, root)) == root:
            return None

    except ValueError:
        pass

    return root


def _mapped_parts(relative: str) -> tuple[str, ...]:
    parts = validate_member_parts(relative)

    if not parts:
        raise InstallationError(f"wheel member has an empty path: {relative!r}")

    if not parts[0].endswith(".data"):
        return parts

    if len(parts) < 3 or parts[1] not in {
        "purelib",
        "platlib",
        "scripts",
        "data",
        "headers",
    }:
        raise InstallationError(f"invalid wheel data path: {relative}")

    if parts[1] == "scripts":
        return ("Scripts" if os.name == "nt" else "bin", *parts[2:])

    return parts[2:]


def _normalized_destination(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(os.path.normcase(part) for part in parts)


def _reserve_destination(
    trie: _DestinationNode,
    parts: tuple[str, ...],
    owner: int,
    candidate: WheelInstallCandidate,
    *,
    allow_same_owner: bool = False,
) -> None:
    node = trie

    normalized = _normalized_destination(parts)

    for part in normalized:
        if node.owner is not None:
            raise InstallationError(
                f"Cannot install {candidate.canonical_name}: "
                f"duplicate installation destination: {'/'.join(parts)}",
            )

        child = node.children.get(part)

        if child is None:
            child = _DestinationNode()

            node.children[part] = child

        node = child

    terminal = node.owner

    has_children = bool(node.children)

    if (
        terminal is not None and not (allow_same_owner and terminal == owner)
    ) or has_children:
        raise InstallationError(
            f"Cannot install {candidate.canonical_name}: "
            f"duplicate installation destination: {'/'.join(parts)}",
        )

    node.owner = owner


def _build_plans(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[WheelInstallCandidate, ...],
    archives: tuple[CachedWheelArchive, ...],
    *,
    prevalidated: bool = False,
) -> tuple[_WheelInstallPlan, ...]:
    trie = _DestinationNode()

    plans: list[_WheelInstallPlan] = []

    for owner, (request, candidate, archive) in enumerate(
        zip(requests, candidates, archives),
    ):
        if not prevalidated:
            for entry in archive.entries:
                _reserve_destination(
                    trie,
                    _mapped_parts(entry[0]),
                    owner,
                    candidate,
                )

        scripts = entry_point_scripts(
            os.path.join(archive.tree, archive.dist_info, "entry_points.txt"),
        )

        for name in scripts:
            if os.path.basename(name) != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory",
                )

            if not prevalidated:
                for generated in (name, f"{name}-script.py", f"{name}.exe"):
                    _reserve_destination(
                        trie,
                        ("Scripts" if os.name == "nt" else "bin", generated),
                        owner,
                        candidate,
                        allow_same_owner=True,
                    )

        plans.append(
            _WheelInstallPlan(
                archive,
                candidate,
                requested=request[1],
                direct_url=request[2],
                scripts=scripts,
            ),
        )

    return tuple(plans)


def _merge_move(source: str, destination: str) -> None:
    if not os.path.lexists(source):
        return

    if not os.path.lexists(destination):
        os.rename(source, destination)

        return

    if not (
        os.path.isdir(source)
        and not os.path.islink(source)
        and os.path.isdir(destination)
        and not os.path.islink(destination)
    ):
        raise FileExistsError(destination)

    with os.scandir(source) as entries:
        names = tuple(entry.name for entry in entries)

    for name in names:
        _merge_move(
            os.path.join(source, name),
            os.path.join(destination, name),
        )

    os.rmdir(source)


def _relocate_data(stage: str, archive: CachedWheelArchive) -> None:
    data_roots = {
        parts[0]
        for relative, _, _, _ in archive.entries
        if (parts := validate_member_parts(relative)) and parts[0].endswith(".data")
    }

    for data_root in data_roots:
        root = os.path.join(stage, data_root)

        for scheme in ("purelib", "platlib", "data", "headers", "scripts"):
            source = os.path.join(root, scheme)

            destination = (
                os.path.join(stage, "Scripts" if os.name == "nt" else "bin")
                if scheme == "scripts"
                else stage
            )

            _merge_move(source, destination)

        if os.path.lexists(root):
            shutil.rmtree(root)


def _write_new_file(path: str, contents: bytes) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)

        else:
            os.unlink(path)

    except FileNotFoundError:
        pass

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "wb") as file:
        file.write(contents)


def _rewrite_metadata(path: str, candidate: WheelInstallCandidate) -> bool:
    if not candidate.name.isalpha():
        return False

    with open(path, "rb") as file:
        contents = file.read()

    lines = contents.decode("utf-8").splitlines(keepends=True)

    rewritten = contents

    for index, line in enumerate(lines):
        if line.lower().startswith("name:"):
            ending = "\n" if line.endswith("\n") else ""

            lines[index] = f"Name: {candidate.name.lower()}{ending}"

            rewritten = "".join(lines).encode("utf-8")

            break

    if rewritten != contents:
        with open(path, "wb") as file:
            file.write(rewritten)

        return True

    return False


def _file_metadata(path: str) -> tuple[str, str]:
    with open(path, "rb") as file:
        return record_metadata_internal(file.read())


def _finalize_wheel(
    stage: str,
    plan: _WheelInstallPlan,
    *,
    script_executable: str | None,
) -> None:
    archive = plan.archive

    candidate = plan.candidate

    dist_info = archive.dist_info

    dist_info_root = os.path.join(stage, dist_info)

    metadata_path = os.path.join(dist_info_root, "METADATA")

    metadata_rewritten = _rewrite_metadata(metadata_path, candidate)

    script_members: set[str] = set()

    for relative, _, _, _ in archive.entries:
        parts = validate_member_parts(relative)

        if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] == "scripts":
            mapped = _mapped_parts(relative)

            path = os.path.join(stage, *mapped)

            rewrite_shebang(path, script_executable)

            script_members.add("/".join(mapped))

    installer = os.path.join(dist_info_root, "INSTALLER")

    requested = os.path.join(dist_info_root, "REQUESTED")

    direct_url = os.path.join(dist_info_root, "direct_url.json")

    _write_new_file(installer, b"cpip\n")

    if plan.requested:
        _write_new_file(requested, b"")

    else:
        try:
            os.unlink(requested)

        except FileNotFoundError:
            pass

    if plan.direct_url is not None:
        _write_new_file(direct_url, plan.direct_url.to_json().encode("utf-8"))

    else:
        try:
            os.unlink(direct_url)

        except FileNotFoundError:
            pass

    generated_names = {
        generated
        for name in plan.scripts
        for generated in (name, f"{name}-script.py", f"{name}.exe")
    }

    scripts_root = os.path.join(stage, "Scripts" if os.name == "nt" else "bin")

    for name in generated_names:
        path = os.path.join(scripts_root, name)

        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)

            else:
                os.unlink(path)

        except FileNotFoundError:
            pass

    generated_paths: list[str] = []

    if plan.scripts:
        with tempfile.TemporaryDirectory(prefix=".cpip-scripts-", dir=stage) as temp:
            generated = generate_entry_point_files(
                plan.scripts,
                temp,
                script_executable,
            )

            os.makedirs(scripts_root, exist_ok=True)

            for source, _ in generated:
                destination = os.path.join(scripts_root, os.path.basename(source))

                os.rename(source, destination)

                generated_paths.append(destination)

    managed = {
        f"{dist_info}/INSTALLER",
        f"{dist_info}/REQUESTED",
        f"{dist_info}/direct_url.json",
    }

    record_relative = f"{dist_info}/RECORD"

    rows: list[tuple[str, str, str]] = []

    for relative, digest, size, _ in archive.entries:
        mapped = _mapped_parts(relative)

        installed_relative = "/".join(mapped)

        if installed_relative in managed:
            continue

        if mapped[0] in {"bin", "Scripts"} and mapped[-1] in generated_names:
            continue

        if installed_relative == record_relative:
            rows.append((installed_relative, "", ""))

            continue

        path = os.path.join(stage, *mapped)

        if (
            installed_relative == f"{dist_info}/METADATA" and metadata_rewritten
        ) or installed_relative in script_members:
            digest, size = _file_metadata(path)

        rows.append((installed_relative, digest, size))

    installer_metadata = _file_metadata(installer)

    rows.append((f"{dist_info}/INSTALLER", *installer_metadata))

    if plan.requested:
        requested_metadata = _file_metadata(requested)

        rows.append((f"{dist_info}/REQUESTED", *requested_metadata))

    if plan.direct_url is not None:
        direct_url_metadata = _file_metadata(direct_url)

        rows.append((f"{dist_info}/direct_url.json", *direct_url_metadata))

    for path in generated_paths:
        generated_metadata = _file_metadata(path)

        rows.append(
            (
                "/".join(
                    (
                        "Scripts" if os.name == "nt" else "bin",
                        os.path.basename(path),
                    ),
                ),
                *generated_metadata,
            ),
        )

    rows.sort()

    record = io.StringIO(newline="")

    csv.writer(record).writerows(rows)

    _write_new_file(
        os.path.join(dist_info_root, "RECORD"),
        record.getvalue().encode("utf-8"),
    )


def _plan_destinations(root: str, plan: _WheelInstallPlan) -> set[str]:
    destinations = {
        os.path.join(root, *_mapped_parts(relative))
        for relative, _, _, _ in plan.archive.entries
    }

    scripts_root = os.path.join(root, "Scripts" if os.name == "nt" else "bin")

    for name in plan.scripts:
        destinations.update(
            os.path.join(scripts_root, generated)
            for generated in (name, f"{name}-script.py", f"{name}.exe")
        )

    return destinations


def _stage_path(root: str, stage: str, path: str) -> str | None:
    try:
        relative = os.path.relpath(path, root)

    except ValueError:
        return None

    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None

    return os.path.join(stage, relative)


def _remove_stage_files(stage: str, paths: set[str]) -> None:
    """Remove an owned file batch and prune each parent at most once."""

    parents: set[str] = set()

    for path in sorted(paths, key=lambda item: item.count(os.sep), reverse=True):
        try:
            os.unlink(path)

        except FileNotFoundError:
            pass

        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EISDIR, errno.EPERM} or not (
                os.path.isdir(path) and not os.path.islink(path)
            ):
                raise

            try:
                shutil.rmtree(path)

            except FileNotFoundError:
                pass

        parent = os.path.dirname(path)

        while parent != stage and parent.startswith(stage + os.sep):
            parents.add(parent)

            parent = os.path.dirname(parent)

    for parent in sorted(
        parents,
        key=lambda item: item.count(os.sep),
        reverse=True,
    ):
        try:
            os.rmdir(parent)

        except OSError:
            pass


def install_wheels_from_archive_cache(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[InstallCandidate, ...],
    *,
    target: InstallTarget,
    cache_dir: str,
    script_executable: str | None = None,
    force: bool = False,
    preserve_existing: bool = False,
    report: bool = True,
) -> tuple[InstallCandidate, ...] | None:
    """Install into a self-contained target from unpacked archives.



    ``None`` means that the target or cache cannot use this optimization and

    the caller should retain the legacy transactional installation path.

    """

    root = _eligible_target(target, cache_dir)

    if root is None:
        return None

    try:
        archives = _prepare_cached_wheels(candidates, cache_dir)

    except OSError:
        return None

    plans = _build_plans(
        requests,
        candidates,
        archives,
        prevalidated=all(
            candidate.wheel_layout is archive
            for candidate, archive in zip(candidates, archives)
        ),
    )

    parent = os.path.dirname(root)

    os.makedirs(parent, exist_ok=True)

    staging_parent = tempfile.mkdtemp(prefix=".cpip-install-", dir=parent)

    stage = os.path.join(staging_parent, "target")

    pool = None

    try:
        root_existed = os.path.isdir(root)

        active_plans = plans

        uninstalling: list[
            InstalledMetadataDistribution | InstalledWheelDistribution
        ] = []

        destinations_by_plan: dict[_WheelInstallPlan, set[str]] = {}

        if root_existed:
            from cpip.build.metadata import InstalledDistributionStore
            from cpip.install.wheel_state import (
                discover_installed_wheels,
                existing_paths,
            )

            clone_path(root, stage)

            names = {plan.candidate.canonical_name for plan in plans}

            existing = discover_installed_wheels((root,), names=names)

            if existing is None:
                existing = {
                    distribution.canonical_name: distribution
                    for distribution in InstalledDistributionStore(paths=[root]).iter(
                        names=names,
                    )
                }

            selected: list[_WheelInstallPlan] = []

            allowed_existing: set[str] = set()

            removals: set[str] = set()

            for plan in plans:
                distribution = existing.get(plan.candidate.canonical_name)

                if (
                    distribution is not None
                    and str(distribution.version) == str(plan.candidate.version)
                    and not force
                    and not preserve_existing
                ):
                    continue

                selected.append(plan)

                if distribution is None:
                    continue

                owned_paths, old_paths = existing_paths(distribution)

                allowed_existing.update(
                    normalized
                    for path in owned_paths
                    if (normalized := _internal_comparison_path(path))
                )

                destinations = _plan_destinations(root, plan)

                destinations_by_plan[plan] = destinations

                normalized_destinations = {
                    _internal_comparison_path(path) for path in destinations
                }

                removals.update(
                    old_paths
                    if not preserve_existing
                    else {
                        path
                        for path in owned_paths
                        if _internal_comparison_path(path) in normalized_destinations
                    }
                )

                uninstalling.append(distribution)

            active_plans = tuple(selected)

            if not active_plans:
                return candidates

            for plan in active_plans:
                destinations = destinations_by_plan.get(plan)

                if destinations is None:
                    destinations = _plan_destinations(root, plan)

                for destination in destinations:
                    if (
                        os.path.lexists(destination)
                        and _internal_comparison_path(destination)
                        not in allowed_existing
                    ):
                        return None

            staged_removals: set[str] = set()

            for path in removals:
                staged = _stage_path(root, stage, path)

                if staged is None:
                    return None

                staged_removals.add(staged)

            _remove_stage_files(stage, staged_removals)

        active_archives = tuple(plan.archive for plan in active_plans)

        if len(archives) >= 4 or len(plans) >= 4:
            pool = ThreadPoolExecutor(
                max_workers=min(
                    _INSTALL_WORKERS,
                    max(len(active_archives), len(active_plans)),
                ),
            )

        try:
            if len(active_archives) >= 4 and pool is not None:
                tuple(
                    pool.map(
                        lambda archive: clone_path(archive.tree, stage),
                        active_archives,
                    )
                )

            else:
                for archive in active_archives:
                    clone_path(archive.tree, stage)

            for archive in active_archives:
                _relocate_data(stage, archive)

        except FileExistsError as exc:
            raise InstallationError(
                "duplicate installation destination while linking cached wheels",
            ) from exc

        if len(active_plans) >= 4 and pool is not None:

            def finalize(plan: _WheelInstallPlan) -> None:
                _finalize_wheel(
                    stage,
                    plan,
                    script_executable=script_executable,
                )

            tuple(pool.map(finalize, active_plans))

        else:
            for plan in active_plans:
                _finalize_wheel(
                    stage,
                    plan,
                    script_executable=script_executable,
                )

        if os.path.lexists(root) != root_existed:
            return None

        if root_existed:
            backup = os.path.join(staging_parent, "previous")

            os.rename(root, backup)

            try:
                os.rename(stage, root)

            except BaseException:
                os.rename(backup, root)

                raise

            shutil.rmtree(backup, ignore_errors=True)

        else:
            os.rename(stage, root)

        if report:
            for distribution in uninstalling:
                print(
                    f"Uninstalling {distribution.raw_name}-{distribution.raw_version}",
                )

                print(
                    f"Successfully uninstalled {distribution.raw_name}-{distribution.raw_version}",
                )

        return candidates

    finally:
        if pool is not None:
            pool.shutdown()

        shutil.rmtree(staging_parent, ignore_errors=True)
