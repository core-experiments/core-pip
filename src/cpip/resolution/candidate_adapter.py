"""Compatibility adapter for indexed local-wheel candidates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpip.resolution.fast_local_wheelhouse import (
    LocalWheelCandidate,
    WheelhouseUnavailable,
    load_candidate,
)

if TYPE_CHECKING:
    from cpip.index.source_models import CandidateRecord


def candidate_from_record(record: CandidateRecord) -> LocalWheelCandidate | None:
    """Adapt an indexed local wheel to the shared lightweight candidate type."""
    if not record.link.is_file or record.link.kind.value != "wheel":
        return None
    try:
        candidate = load_candidate(record.link.file_path)
    except WheelhouseUnavailable:
        return None
    if (
        candidate.canonical_name != record.canonical_name
        or candidate.version != record.version
    ):
        return None
    candidate.source_url = record.link.url
    candidate.source_hashes = dict(record.link.hashes)
    candidate.source_kind = record.link.kind.value
    candidate.yanked_reason = record.link.yanked_reason
    return candidate
