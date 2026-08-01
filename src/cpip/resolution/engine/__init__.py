"""Canonical dependency-resolution engine API."""

from cpip.resolution.engine.api import ResolutionConfig, ResolutionEngine
from cpip.resolution.engine.model import (
    ResolutionCandidate,
    ResolutionResult,
    ResolvedRequirement,
)

__all__ = [
    "ResolutionCandidate",
    "ResolutionConfig",
    "ResolutionEngine",
    "ResolutionResult",
    "ResolvedRequirement",
]
