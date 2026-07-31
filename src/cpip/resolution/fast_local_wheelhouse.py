"""Facade for the lightweight local pure-wheel resolver.

The implementation is organized under :mod:`cpip.resolution.fast_wheelhouse`;
this module keeps the established command-facing entry points small and
explicit.
"""

from cpip.resolution.fast_wheelhouse.archive import WheelArchive, WheelhouseUnavailable
from cpip.resolution.fast_wheelhouse.engine import (
    build_catalog_indexes,
    parse_requirement,
    quote_path,
    resolve,
)
from cpip.resolution.fast_wheelhouse.metadata import load_candidate, wheel_name
from cpip.resolution.fast_wheelhouse.models import (
    LocalWheelCandidate,
    LocalWheelPlan,
    LocalWheelRequirement,
    LocalWheelSpecifier,
    LocalWheelVersion,
)

__all__ = [
    "LocalWheelCandidate",
    "LocalWheelPlan",
    "LocalWheelRequirement",
    "LocalWheelSpecifier",
    "LocalWheelVersion",
    "WheelArchive",
    "WheelhouseUnavailable",
    "build_catalog_indexes",
    "load_candidate",
    "parse_requirement",
    "quote_path",
    "resolve",
    "wheel_name",
]
