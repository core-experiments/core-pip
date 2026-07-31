"""Resolver result assembly and installation ordering."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cpip.core.errors import ResolutionError
from cpip.core.hashes import file_hashes as compute_file_hashes
from cpip.core.packaging import Version
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import LazyWheelCandidate
from cpip.resolution.algorithms import (
    actual_hashes_for_candidate as actual_hashes_for_candidate_internal,
)

SOURCE_TREE_OR_VCS_KINDS = frozenset(("source-tree", "vcs"))

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement
    from cpip.resolution.requirement_set import RequirementSet


def file_hashes(path: Path) -> dict[str, str]:
    return compute_file_hashes(path)


def actual_hashes_for_candidate(candidate: WheelCandidate) -> dict[str, str]:
    return actual_hashes_for_candidate_internal(candidate, file_hashes)


def finalize_source_hashes(candidate: WheelCandidate) -> WheelCandidate:
    if isinstance(candidate, LazyWheelCandidate):
        if candidate.materializer_internal.dry_run and not candidate.record_internal.link.is_file:
            # Keep index-provided hashes, but never download a remote artifact
            # solely to fill an optional dry-run report field.
            return candidate
        if (
            candidate.materializer_internal.dry_run
            and candidate.source_kind in {"sdist", "source-tree", "vcs"}
        ):
            return candidate
        candidate = candidate.materialize()
    if (
        candidate.source_hashes
        or candidate.source_kind in SOURCE_TREE_OR_VCS_KINDS
        or (candidate.from_cache)
    ):
        return candidate
    hashes = actual_hashes_for_candidate(candidate)
    return candidate.copy_with(source_hashes=hashes or None)


def get_installation_order(
    resolver,
    requirement_set: RequirementSet,
    *,
    graph: dict[str, set[str]] | None = None,
) -> list[InstallRequirement]:
    active_graph = graph or resolver.last_graph
    if active_graph is None:
        raise ResolutionError("installation order is unavailable before resolution")
    named = requirement_set.requirements
    ordered_names = resolver.installation_order(
        {
            name: WheelCandidate(
                name=req.req.name,
                version=Version("0"),
                path=Path("."),
                dependencies=(),
            )
            for name, req in named.items()
            if req.req is not None
        },
        active_graph,
    )
    return [named[name] for name in ordered_names if name in named]


def installation_order(
    selected: dict[str, WheelCandidate],
    graph: dict[str, set[str]],
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in sorted(graph.get(name, ()), reverse=True):
            if dep in selected:
                visit(dep)
        if name in selected:
            ordered.append(name)

    for name in sorted(selected, reverse=True):
        visit(name)
    return ordered
