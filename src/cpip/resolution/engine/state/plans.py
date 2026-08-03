"""Resolved-plan and installed-satisfaction state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpip.core.packaging import Requirement
from cpip.core.wheel import WheelCandidate

if TYPE_CHECKING:
    from cpip.core.metadata import InstalledDistribution


class InstallPlan:
    """Resolved candidates plus dependency graph and installed requirements."""

    __slots__ = ("candidates", "conflicts", "graph", "metrics", "satisfied")

    def __init__(
        self,
        candidates: list[WheelCandidate],
        graph: dict[str, set[str]] | None = None,
        conflicts: list[str] | None = None,
        satisfied: list[SatisfiedRequirement] | None = None,
        metrics: dict[str, int | float] | None = None,
    ) -> None:
        self.candidates = candidates
        self.graph = {} if graph is None else graph
        self.conflicts = [] if conflicts is None else conflicts
        self.satisfied = [] if satisfied is None else satisfied
        self.metrics = {} if metrics is None else metrics


class SatisfiedRequirement:
    __slots__ = ("distribution", "requirement")

    def __init__(
        self,
        requirement: Requirement,
        distribution: InstalledDistribution,
    ) -> None:
        self.requirement = requirement
        self.distribution = distribution

    requirement: Requirement
    distribution: InstalledDistribution
