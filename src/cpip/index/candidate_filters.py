"""Candidate filtering policies shared by the evaluator."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TypeVar

from cpip.core.hashes import Hashes
from cpip.core.wheel import WheelTag
from cpip.index.source_models import CandidateRecord

logger = logging.getLogger("cpip.index.candidate_evaluators")
CandidateT = TypeVar("CandidateT", bound=CandidateRecord)


@lru_cache(maxsize=64)
def supported_tag_ranks(tags: tuple[WheelTag, ...]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for index, tag in enumerate(tags):
        ranks.setdefault(str(tag).lower(), index)
    return ranks


def allowed_hashes(hashes: Hashes | None) -> frozenset[str]:
    return hashes.allowed_digests if hashes is not None else frozenset()


def filter_unallowed_hashes(
    candidates: list[CandidateT],
    *,
    hashes: Hashes | None,
    project_name: str,
) -> list[CandidateT]:
    allowed = allowed_hashes(hashes)
    if hashes is None:
        return list(candidates)
    if not allowed:
        logger.debug(
            "Given no hashes to check %d links for project %r: discarding no candidates",
            len(candidates),
            project_name,
        )
        return list(candidates)
    matches = 0
    no_digest = 0
    discarded: list[str] = []
    result: list[CandidateT] = []
    for candidate in candidates:
        candidate_hashes = candidate.link.hashes or {}
        digest = candidate_hashes.get("sha256")
        if digest is None:
            no_digest += 1
            result.append(candidate)
        elif digest in allowed:
            matches += 1
            result.append(candidate)
        else:
            discarded.append(candidate.link.url)
    if matches == 0:
        logger.debug(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
        return list(candidates)
    if discarded:
        logger.debug(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding %d non-matches:\n  %s",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
            len(discarded),
            "\n  ".join(discarded),
        )
    else:
        logger.debug(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
    return result
