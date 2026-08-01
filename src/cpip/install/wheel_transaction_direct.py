"""Direct-install fast path for fresh wheel batches."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cpip.core.errors import InstallationError
from cpip.core.wheel import WheelCandidate
from cpip.install.target import InstallTarget
from cpip.install.transaction import InstallTransaction
from cpip.install.wheel_archive import (
    DestinationCache,
    ResolvedRoots,
    destination_internal_parts,
    validate_member_parts,
)

if TYPE_CHECKING:
    from cpip.core.direct_url import DirectUrl

DIRECT_CONTENT_BATCH_LIMIT = 4 * 1024 * 1024


def direct_batch_preflight(
    requests: tuple[tuple[str | Path, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
) -> DestinationCache | None:
    """Check whether a batch can write final paths without staging files."""
    destinations: set[str] = set()
    resolved_directories: DestinationCache = {}
    resolved_roots: ResolvedRoots = {}
    member_sets: list[tuple[tuple[str, int, int, int, int, int], ...]] = []
    total_size = 0
    for request, candidate in zip(requests, candidates):
        if request[2] is not None or candidate.wheel_layout is None:
            return None
        _, raw_members, _ = cast(
            "tuple[str, tuple[tuple[str, int, int, int, int, int], ...], bool]",
            candidate.wheel_layout,
        )
        member_sets.append(raw_members)
        total_size += sum(
            raw_member[4]
            for raw_member in raw_members
            if not raw_member[0].endswith("/")
        )
    if total_size <= DIRECT_CONTENT_BATCH_LIMIT:
        return None
    for raw_members in member_sets:
        for raw_member in raw_members:
            name = raw_member[0]
            if name.endswith("/"):
                continue
            try:
                relative_parts = validate_member_parts(name)
            except InstallationError:
                return None
            if (
                (relative_parts[-1] if relative_parts else "")
                in {"INSTALLER", "REQUESTED", "direct_url.json"}
                or (relative_parts[-1] if relative_parts else "") == "entry_points.txt"
                or (len(relative_parts) >= 2 and relative_parts[-2] == "scripts")
            ):
                return None
            destination = destination_internal_parts(
                target,
                relative_parts,
                name,
                resolved_directories=resolved_directories,
                resolved_roots=resolved_roots,
            )
            destination_text = os.fspath(destination)
            if destination_text in destinations or os.path.lexists(destination_text):
                return None
            destinations.add(destination_text)
    return resolved_directories


def install_wheels_directly(
    requests: tuple[tuple[str | Path, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
    pycompile: bool,
    installer: Any,
    destination_cache: DestinationCache,
) -> tuple[WheelCandidate, ...]:
    """Install a preflighted fresh batch directly with transactional rollback."""
    with InstallTransaction() as transaction:
        parallel = 4 <= len(requests) <= 64 and not pycompile

        def install_one(
            index: int,
            request: tuple[str | Path, bool, DirectUrl | None],
            candidate: WheelCandidate,
        ) -> tuple[int, InstallTransaction, WheelCandidate]:
            local_transaction = InstallTransaction()
            try:
                result = installer.install(
                    request[0],
                    candidate=candidate,
                    requested=request[1],
                    direct_url=request[2],
                    existing=None,
                    lookup_existing=False,
                    destination_cache=destination_cache,
                    transaction=local_transaction,
                    direct=True,
                )
            except Exception:
                local_transaction.rollback()
                raise
            return index, local_transaction, result

        futures = []
        staged_results: list[tuple[int, InstallTransaction, WheelCandidate]] = []
        try:
            if parallel:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(requests))) as pool:
                    futures = [
                        pool.submit(install_one, index, request, candidate)
                        for index, (request, candidate) in enumerate(
                            zip(requests, candidates),
                        )
                    ]
                    staged_results = [future.result() for future in futures]
                ordered_results = sorted(staged_results, key=lambda item: item[0])
                for _, local_transaction, _ in ordered_results:
                    transaction.adopt(local_transaction)
                transaction.finish_successfully()
                for _, local_transaction, _ in ordered_results:
                    local_transaction.finalize()
                return tuple(result for _, _, result in ordered_results)

            results = tuple(
                installer.install(
                    path,
                    candidate=candidate,
                    requested=requested,
                    direct_url=direct_url,
                    existing=None,
                    lookup_existing=False,
                    destination_cache=destination_cache,
                    transaction=transaction,
                    direct=True,
                )
                for (path, requested, direct_url), candidate in zip(
                    requests,
                    candidates,
                )
            )
            transaction.finish_successfully()
            return results
        except Exception:
            for future in futures:
                if not future.done() or future.cancelled():
                    continue
                try:
                    _, local_transaction, _ = future.result()
                except Exception:
                    continue
                local_transaction.rollback()
            transaction.rollback()
            raise
