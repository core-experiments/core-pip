"""Canonical dependency-resolution engine API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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


def __getattr__(name: str) -> Any:
    if name in {"ResolutionConfig", "ResolutionEngine"}:
        from cpip.resolution.engine import api

        value = getattr(api, name)
    elif name in {
        "ResolutionCandidate",
        "ResolutionResult",
        "ResolvedRequirement",
    }:
        from cpip.resolution.engine import model

        value = getattr(model, name)
    else:
        raise AttributeError(name)
    globals()[name] = value
    return value
