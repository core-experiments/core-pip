"""Search requests exchanged by the resolver engine."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

from cpip.core.wheel import WheelCandidate
from cpip.resolution.resolver_internals.state.agenda import PendingAgenda
from cpip.resolution.resolver_internals.state.plans import SatisfiedRequirement

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement
    from cpip.resolution.resolver_internals.state.domains import (
        LearnedIncompatibility,
    )


class SearchFailure:
    """Internal false result carrying a conflict-directed jump target."""

    __slots__ = ("conflict", "target_level")

    def __init__(self, conflict: LearnedIncompatibility, target_level: int) -> None:
        self.conflict = conflict
        self.target_level = target_level

    def __bool__(self) -> bool:
        return False


class SearchRequest:
    """A resumable resolver search frame exchanged during backtracking."""

    __slots__ = (
        "pending",
        "selected",
        "selected_extras",
        "satisfied",
        "graph",
        "source_requirements",
        "source_requirements_by_url",
        "checkpoint",
    )

    def __init__(
        self,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
        checkpoint: int = 0,
    ) -> None:
        self.pending = pending
        self.selected = selected
        self.selected_extras = selected_extras
        self.satisfied = satisfied
        self.graph = graph
        self.source_requirements = source_requirements
        self.source_requirements_by_url = source_requirements_by_url
        self.checkpoint = checkpoint


SearchFrame = Generator[SearchRequest, bool | SearchFailure, bool | SearchFailure]
