"""Installation candidate materialization and ordering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from cpip.core.wheel import WheelCandidate
from cpip.index.candidate_materialization import LazyWheelCandidate

_MATERIALIZATION_WORKERS = 32


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

            archive_futures[
                archive_pool.submit(prepare_archive, candidate, cache_dir)
            ] = index

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
