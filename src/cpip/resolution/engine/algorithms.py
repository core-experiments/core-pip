"""Stateless candidate, version, hash, graph, and URL algorithms."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Callable

from cpip.core.packaging import Requirement, Version
from cpip.core.urls import url_to_path
from cpip.core.wheel import WheelCandidate

if TYPE_CHECKING:
    from cpip.resolution.engine.input.models import RequirementInput

HTTP_URL_SCHEMES = frozenset(("http", "https"))
LOCAL_FILE_NETLOCS = frozenset(("", "localhost"))
PYPI_HOSTS = frozenset(
    (
        "files.pythonhosted.org",
        "test-files.pythonhosted.org",
        "pypi.org",
        "test.pypi.org",
    ),
)
SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))


def file_hashes(path: str) -> dict[str, str]:
    from cpip.core.hashes import file_hashes as compute_file_hashes

    return compute_file_hashes(path)


def best_candidate_internal(
    candidates: Iterable[WheelCandidate],
    requirement: Requirement,
    *,
    allow_prereleases: bool,
) -> WheelCandidate | None:
    for candidate in candidates:
        if requirement.is_satisfied_by(
            candidate.version,
            allow_prereleases=allow_prereleases,
        ):
            return candidate
    return None


def exact_pinned_version(requirement: Requirement) -> Version | None:
    return next(
        (
            specifier.parsed_version
            for specifier in requirement.specifier.specifiers
            if specifier.operator == "==" and not specifier.version.endswith(".*")
        ),
        None,
    )


def specifier_intersection_is_empty(requirements: Iterable[Requirement]) -> bool:
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    excluded: set[Version] = set()

    def tighten_lower(version: Version, inclusive: bool) -> None:
        nonlocal lower
        if lower is None or version > lower[0]:
            lower = (version, inclusive)
        elif version == lower[0]:
            lower = (version, lower[1] and inclusive)

    def tighten_upper(version: Version, inclusive: bool) -> None:
        nonlocal upper
        if upper is None or version < upper[0]:
            upper = (version, inclusive)
        elif version == upper[0]:
            upper = (version, upper[1] and inclusive)

    for requirement in requirements:
        for specifier in requirement.specifier.specifiers:
            if specifier.operator == "==" and not specifier.version.endswith(".*"):
                tighten_lower(specifier.parsed_version, True)
                tighten_upper(specifier.parsed_version, True)
            elif specifier.operator == ">=":
                tighten_lower(specifier.parsed_version, True)
            elif specifier.operator == ">":
                tighten_lower(specifier.parsed_version, False)
            elif specifier.operator == "<=":
                tighten_upper(specifier.parsed_version, True)
            elif specifier.operator == "<":
                tighten_upper(specifier.parsed_version, False)
            elif specifier.operator == "~=":
                tighten_lower(specifier.parsed_version, True)
                tighten_upper(specifier.compatible_upper_bound, False)
            elif specifier.operator == "!=" and not specifier.version.endswith(".*"):
                excluded.add(specifier.parsed_version)

    if lower is None or upper is None:
        return False
    if lower[0] > upper[0]:
        return True
    if lower[0] < upper[0]:
        return False
    return not (lower[1] and upper[1]) or lower[0] in excluded


def topological_weights(
    graph: dict[str, set[str]],
    requirement_keys: set[str],
) -> dict[str, int]:
    reachable: set[str] = set()
    stack = sorted(graph.get("<root>", ()))
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(graph.get(node, ()))

    relevant = reachable & set(requirement_keys)
    memo: dict[str, int] = {}
    node_bits = {node: 1 << index for index, node in enumerate(relevant)}

    for root in relevant:
        if root in memo:
            continue
        pending: list[tuple[str, bool, int]] = [(root, False, 0)]
        while pending:
            node, expanded, path = pending.pop()
            if node in memo:
                continue
            node_bit = node_bits[node]
            if path & node_bit:
                continue
            deps = [dep for dep in graph.get(node, ()) if dep in relevant]
            if not expanded:
                next_path = path | node_bit
                pending.append((node, True, path))
                pending.extend(
                    (dep, False, next_path)
                    for dep in reversed(sorted(deps))
                    if dep not in memo
                )
                continue
            memo[node] = 1 + max(
                (memo.get(dep, 0) if not path & node_bits[dep] else 0 for dep in deps),
                default=0,
            )

    return memo


def allowed_hashes_internal(requirement: RequirementInput) -> dict[str, set[str]]:
    return {
        algorithm: set(digests)
        for algorithm, digests in requirement.hash_options.items()
        if digests
    }


def hash_sets(hashes: Mapping[str, str | list[str]]) -> dict[str, set[str]]:
    return {
        algorithm: {
            digest.lower() for digest in (value if isinstance(value, list) else [value])
        }
        for algorithm, value in hashes.items()
        if value
    }


def actual_hashes_for_candidate(
    candidate: WheelCandidate,
    hash_file: Callable[[str], dict[str, str]] = file_hashes,
) -> dict[str, str]:
    if candidate.source_kind in SOURCE_KINDS and candidate.source_hashes:
        return dict(candidate.source_hashes)
    if candidate.source_url:
        parsed_url = urllib.parse.urlparse(candidate.source_url)
    else:
        parsed_url = None
    if parsed_url is not None and parsed_url.scheme == "file":
        try:
            path_text = url_to_path(candidate.source_url or "")
            return hash_file(path_text)
        except OSError:
            return {}
    try:
        return hash_file(candidate.path)
    except OSError:
        return {}


def hashes_match(
    allowed_hashes: dict[str, set[str]],
    actual_hashes: dict[str, str],
) -> bool:
    for algorithm, digests in allowed_hashes.items():
        actual = actual_hashes.get(algorithm)
        if actual is not None and actual.lower() in {
            digest.lower() for digest in digests
        }:
            return True
    return False


def is_direct_requirement(requirement: Requirement) -> bool:
    raw = requirement.raw.strip()
    return (
        requirement.url is not None
        or raw.startswith((".", "/", "~"))
        or os.sep in raw
        or (os.altsep is not None and os.altsep in raw)
    )


def direct_urls_equivalent(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return first == second
    first_parts = urllib.parse.urlsplit(first)
    second_parts = urllib.parse.urlsplit(second)

    def local_path(parts: urllib.parse.SplitResult, original: str) -> str | None:
        if parts.scheme.lower() == "file":
            return os.path.realpath(url_to_path(original))
        if parts.scheme == "":
            return os.path.realpath(original)
        return None

    first_path = local_path(first_parts, first)
    second_path = local_path(second_parts, second)
    if (first_path is not None or second_path is not None) and (
        first_path is None or second_path is None or first_path != second_path
    ):
        return False

    def normalize_parts(
        parts: urllib.parse.SplitResult,
    ) -> urllib.parse.SplitResult:
        if (
            parts.scheme.lower() == "file"
            and parts.netloc.lower() in LOCAL_FILE_NETLOCS
        ):
            return parts._replace(netloc="")
        return parts

    first_parts = normalize_parts(first_parts)
    second_parts = normalize_parts(second_parts)
    if first_parts[:3] != second_parts[:3]:
        return False

    def normalize_pairs(
        value: str,
        *,
        drop_egg: bool = False,
    ) -> tuple[tuple[str, str], ...]:
        pairs = urllib.parse.parse_qsl(value, keep_blank_values=True)
        if drop_egg:
            pairs = [(key, item) for key, item in pairs if key != "egg"]
        return tuple(sorted(pairs))

    return normalize_pairs(first_parts.query) == normalize_pairs(
        second_parts.query,
    ) and normalize_pairs(first_parts.fragment, drop_egg=True) == normalize_pairs(
        second_parts.fragment,
        drop_egg=True,
    )


def is_pypi_hosted_url(url: str | None) -> bool:
    if not url:
        return False
    if url.partition(":")[0].casefold() not in HTTP_URL_SCHEMES:
        return False
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return host in PYPI_HOSTS
