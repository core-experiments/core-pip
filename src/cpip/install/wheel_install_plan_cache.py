"""Short-lived exact-pin install-plan receipts backed by the archive cache.

An exact-pin command (every root requirement is a plain ``==`` specifier with
no URL, hash, or config-settings override) can skip resolution entirely on a
warm cache: the receipt records which archive-cache entries satisfied the
previous resolve, and loading it revalidates the receipt's shape and confirms
each referenced archive tree still exists before reuse -- it trusts, rather
than re-hashes, an existing tree's contents against the recorded entries.
"""

from __future__ import annotations

import hashlib
import marshal
import os
import time
from types import MappingProxyType

from cpip.core.packaging import parse_requirement
from cpip.core.versions import Version
from cpip.core.utils import CACHE_INTERPRETER_TAG
from cpip.core.wheel import WheelCandidate
from cpip.install.wheel_archive_cache import (
    CachedWheelArchive,
    archive_entry_root,
    load_archive,
    valid_archive_entries,
    valid_sha256,
    wheel_digest,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.resolution.models import ResolutionResult

# Every caller today already folds interpreter/platform identity into the
# key's context tuple (see cli/install.py:cached_remote_plan_key and
# cli/fast_install.py), but the cache should not depend on every future
# caller remembering that -- receipts are cheap to regenerate, so scope the
# bucket itself and make it a guarantee rather than a convention.
RESOLUTION_CACHE_BUCKET = f"resolution-{CACHE_INTERPRETER_TAG}"

# First element of the key context every remote exact-pin caller builds.
REMOTE_EXACT_CONTEXT = "remote-exact"

RESOLUTION_CACHE_TTL_SECONDS = 600.0


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
    # A URL or a local path locates one artifact; a plan keyed by name and
    # version alone must never be reused for (or from) it.
    if getattr(requirement, "url", None) is not None or getattr(
        requirement, "is_unnamed_direct", False
    ):
        return None

    exact_version = getattr(
        getattr(requirement, "specifier", None), "exact_version", None
    )

    if exact_version is None:
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
        str(exact_version),
        tuple(sorted(extra for extra in extras if isinstance(extra, str))),
    )


def _exact_install_plan_key(
    normalized: list[tuple[str, str, tuple[str, ...]]],
    context: tuple[object, ...],
) -> str | None:
    if not normalized:
        return None

    # Deferred: json only when a receipt key is built.
    import json

    payload = json.dumps(
        (tuple(sorted(normalized)), context),
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

    if not valid_sha256(key) or not candidates:
        return False

    records = []

    try:
        for candidate in candidates:
            if candidate.source_kind != "wheel":
                return False

            digest = wheel_digest(candidate, cache_dir)

            archive = load_archive(archive_entry_root(cache_dir, digest), digest)

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
            time.time(),
            key,
            tuple(records),
            graph_items,
        )

        path = _resolution_path(cache_dir, key)

        directory = os.path.dirname(path)

        os.makedirs(directory, exist_ok=True)

        # Deferred: tempfile only when a receipt is written.
        import tempfile

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

    if not valid_sha256(key):
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
        and len(value) == 4
        and isinstance(value[0], float)
        and value[1] == key
        and isinstance(value[2], tuple)
        and isinstance(value[3], tuple)
    ):
        return None

    if time.time() - value[0] > RESOLUTION_CACHE_TTL_SECONDS:
        return None

    candidates: list[WheelCandidate] = []

    try:
        for record in value[2]:
            if not (
                isinstance(record, tuple)
                and len(record) == 13
                and isinstance(record[0], str)
                and isinstance(record[1], str)
                and isinstance(record[2], str)
                and valid_sha256(record[2])
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
                and valid_archive_entries(record[12])
            ):
                return None

            tree = os.path.join(
                archive_entry_root(cache_dir, record[2]),
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

        for graph_record in value[3]:
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

    if len(graph) != len(value[3]):
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

    # Deferred: resolution.models is imported only when a receipt is actually
    # loaded, not by every path that can prepare an archive.
    from cpip.resolution.models import ResolutionResult

    return ResolutionResult(
        candidates=tuple(candidates),
        graph={name: frozenset(children) for name, children in graph.items()},
        metrics=MappingProxyType({"warm_resolution_cache_hit": 1}),
    )
