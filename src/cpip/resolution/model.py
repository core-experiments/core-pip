"""Canonical values exchanged by the resolver and install pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution
    from cpip.core.packaging import Requirement


@dataclass(frozen=True, slots=True)
class ResolvedRequirement:
    """A requirement satisfied by an already-installed distribution."""

    requirement: Requirement
    distribution: InstalledDistribution


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """The single result shape returned by the canonical engine."""

    candidates: tuple[Any, ...]
    graph: Mapping[str, frozenset[str]]
    conflicts: tuple[str, ...] = ()
    satisfied: tuple[ResolvedRequirement, ...] = ()
    metrics: Mapping[str, int | float] = MappingProxyType({})
