"""Installation candidate materialization and ordering."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, TypeVar

from cpip.core.errors import ResolutionError
from cpip.core.hashes import file_hashes
from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import LazyWheelCandidate

SOURCE_TREE_OR_VCS_KINDS = frozenset(("source-tree", "vcs"))


if TYPE_CHECKING:
    from cpip.resolution.input_models import RequirementInput
    from cpip.install.requirement_set import RequirementSet


RequirementT = TypeVar("RequirementT", bound="RequirementInput")


default_file_hashes = file_hashes


_MATERIALIZATION_WORKERS = 32


def actual_hashes_for_candidate(candidate: WheelCandidate) -> dict[str, str]:
    if candidate.source_kind in {"sdist", "source-tree", "vcs"} and candidate.source_hashes:
        return dict(candidate.source_hashes)
    source_url = candidate.source_url
    if source_url is not None and source_url.startswith("file:"):
        from cpip.core.urls import url_to_path

        try:
            return file_hashes(url_to_path(source_url))
        except OSError:
            return {}
    try:
        return file_hashes(candidate.path)
    except OSError:
        return {}


def finalize_source_hashes(candidate: WheelCandidate) -> WheelCandidate:
    if isinstance(candidate, LazyWheelCandidate):
        if candidate.materializer_internal.dry_run and not candidate.record_internal.link.is_file:
            # Keep index-provided hashes, but never download a remote artifact

            # solely to fill an optional dry-run report field.

            return candidate

        if candidate.materializer_internal.dry_run and candidate.source_kind in {
            "sdist",
            "source-tree",
            "vcs",
        }:
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


def _run_candidate_operation(
    candidates: Sequence[WheelCandidate],
    operation: Callable[[WheelCandidate], WheelCandidate],
) -> list[WheelCandidate]:
    """Run an artifact operation with ordered, winner-only wheel concurrency."""

    completed: list[WheelCandidate] = []

    remote_wheels: list[WheelCandidate] = []

    def flush_remote_wheels() -> None:
        if not remote_wheels:
            return

        if len(remote_wheels) == 1:
            completed.append(operation(remote_wheels[0]))

        else:
            # Keep this import off warm paths where every wheel is already

            # materialized from the local cache.

            with ThreadPoolExecutor(
                max_workers=min(_MATERIALIZATION_WORKERS, len(remote_wheels)),
                thread_name_prefix="cpip-wheel",
            ) as pool:
                # Executor.map yields in input order, so errors and result

                # assembly remain deterministic even when downloads finish

                # out of order.

                completed.extend(pool.map(operation, remote_wheels))

        remote_wheels.clear()

    for candidate in candidates:
        if (
            isinstance(candidate, LazyWheelCandidate)
            and candidate.source_kind == "wheel"
            and not candidate.record_internal.link.is_file
        ):
            remote_wheels.append(candidate)

            continue

        flush_remote_wheels()

        completed.append(operation(candidate))

    flush_remote_wheels()

    return completed


def materialize_candidate(candidate: WheelCandidate) -> WheelCandidate:
    if isinstance(candidate, LazyWheelCandidate):
        return candidate.materialize()

    return candidate


def materialize_candidates(
    candidates: Sequence[WheelCandidate],
) -> list[WheelCandidate]:
    """Materialize remote wheel winners concurrently in installation order."""

    return _run_candidate_operation(candidates, materialize_candidate)


def prepare_install_candidates(
    candidates: Sequence[WheelCandidate],
    cache_dir: str | None,
    prepare_archive: Callable[[WheelCandidate, str], object] | None = None,
) -> list[WheelCandidate]:
    """Materialize winners and pipeline completed wheels into archive storage."""

    if cache_dir is None or not candidates or prepare_archive is None:
        return materialize_candidates(candidates)

    # Keep these imports off commands that do not install and cache wheels.

    count = len(candidates)

    concrete: list[WheelCandidate | None] = [None] * count

    prepared: list[WheelCandidate | None] = [None] * count

    errors: list[BaseException | None] = [None] * count

    remote: list[tuple[int, WheelCandidate]] = []

    local: list[tuple[int, WheelCandidate]] = []

    for index, candidate in enumerate(candidates):
        if (
            isinstance(candidate, LazyWheelCandidate)
            and candidate.source_kind == "wheel"
            and not candidate.record_internal.link.is_file
        ):
            remote.append((index, candidate))

        else:
            local.append((index, candidate))

    archive_futures: dict[Future[object], int] = {}

    with ThreadPoolExecutor(
        max_workers=min(4, count),
        thread_name_prefix="cpip-archive",
    ) as archive_pool:

        def submit_archive(index: int, candidate: WheelCandidate) -> None:
            concrete[index] = candidate

            archive_futures[archive_pool.submit(prepare_archive, candidate, cache_dir)] = index

        if remote:
            with ThreadPoolExecutor(
                max_workers=min(_MATERIALIZATION_WORKERS, len(remote)),
                thread_name_prefix="cpip-wheel",
            ) as download_pool:
                download_futures = {
                    download_pool.submit(materialize_candidate, candidate): index
                    for index, candidate in remote
                }

                for future in as_completed(download_futures):
                    index = download_futures[future]

                    try:
                        submit_archive(index, future.result())

                    except Exception as exc:
                        errors[index] = exc

        # Source builds and local candidates intentionally remain on the

        # calling thread. Archive preparation still overlaps between them.

        for index, candidate in local:
            try:
                submit_archive(index, materialize_candidate(candidate))

            except Exception as exc:
                errors[index] = exc

        for future in as_completed(tuple(archive_futures)):
            index = archive_futures[future]

            candidate = concrete[index]

            assert candidate is not None

            try:
                archive = future.result()

            except OSError:
                # Cache access is an optimization. Preserve the concrete ZIP

                # for the existing transactional installer.

                prepared[index] = candidate

            except Exception as exc:
                errors[index] = exc

            else:
                prepared[index] = candidate.copy_with(wheel_layout=archive)

    for error in errors:
        if error is not None:
            raise error

    if any(candidate is None for candidate in prepared):
        raise RuntimeError("candidate preparation did not produce every wheel")

    return [candidate for candidate in prepared if candidate is not None]


def finalize_candidates(
    candidates: Sequence[WheelCandidate],
    finalize: Callable[[WheelCandidate], WheelCandidate] = finalize_source_hashes,
) -> list[WheelCandidate]:
    """Finalize remote wheel winners concurrently while preserving plan order."""

    return _run_candidate_operation(candidates, finalize)


def get_installation_order(
    resolver,
    requirement_set: RequirementSet[RequirementT],
    *,
    graph: dict[str, set[str]] | None = None,
) -> list[RequirementT]:
    active_graph = graph or resolver.last_graph

    if active_graph is None:
        raise ResolutionError("installation order is unavailable before resolution")

    named = requirement_set.requirements

    ordered_names = resolver.installation_order(
        {name for name, req in named.items() if req.req is not None},
        active_graph,
    )

    return [named[name] for name in ordered_names if name in named]


def installation_order(
    selected: Collection[str],
    graph: dict[str, set[str]],
) -> list[str]:
    ordered: list[str] = []

    state: dict[str, int] = {}

    for name in sorted(selected, reverse=True):
        if state.get(name, 0) == 2:
            continue

        pending: list[tuple[str, bool]] = [(name, False)]

        while pending:
            current, expanded = pending.pop()

            if expanded:
                state[current] = 2

                ordered.append(current)

                continue

            if state.get(current, 0) == 2:
                continue

            if state.get(current, 0) == 1:
                continue

            state[current] = 1

            pending.append((current, True))

            dependencies = [
                dep for dep in sorted(graph.get(current, ()), reverse=True) if dep in selected
            ]

            pending.extend((dep, False) for dep in reversed(dependencies))

    return ordered
