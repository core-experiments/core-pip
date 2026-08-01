"""Shared local wheelhouse resolver.

The implementation lives in :mod:`fast_local_wheelhouse` because keeping its
parser independent from the full packaging stack is the main performance
property.  This module is the descriptive public-internal entry point used by
commands; the compatibility import keeps callers from knowing that detail.
"""

from cpip.resolution.fast_local_wheelhouse import (
    LocalWheelCandidate,
    LocalWheelPlan,
    WheelArchive,
    WheelhouseUnavailable,
    resolve as resolve_local_wheelhouse,
)


def __getattr__(name: str):
    if name == "candidate_from_record":
        from cpip.resolution.candidate_adapter import candidate_from_record

        return candidate_from_record
    raise AttributeError(name)


_CANDIDATE_FROM_RECORD = "candidate_from_record"

__all__ = [
    "LocalWheelCandidate",
    "LocalWheelPlan",
    "WheelArchive",
    "WheelhouseUnavailable",
    _CANDIDATE_FROM_RECORD,
    "resolve_local_wheelhouse",
]
