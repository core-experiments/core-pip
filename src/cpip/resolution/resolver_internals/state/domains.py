"""Package domains and learned incompatibility state."""

from __future__ import annotations

from collections.abc import Callable

from cpip.core.packaging import Requirement


class PackageDomain:
    """Requirements and cached masks for one package during a search."""

    __slots__ = (
        "roots",
        "incoming",
        "requirements_internal",
        "constrained_internal",
        "constrained_roots_internal",
        "root_version_mask",
        "incoming_version_masks",
        "active_version_mask",
        "decision_count",
    )

    def __init__(
        self,
        roots: list[Requirement] | None = None,
        incoming: dict[str, tuple[Requirement, ...]] | None = None,
    ) -> None:
        self.roots = [] if roots is None else roots
        self.incoming = {} if incoming is None else incoming
        self.requirements_internal: tuple[Requirement, ...] | None = None
        self.constrained_internal: tuple[Requirement, ...] | None = None
        self.constrained_roots_internal: tuple[Requirement, ...] | None = None
        self.root_version_mask: int | None = None
        self.incoming_version_masks: dict[str, int] = {}
        self.active_version_mask: int | None = None
        self.decision_count: int | None = None

    def requirements(self) -> tuple[Requirement, ...]:
        if self.requirements_internal is None:
            self.requirements_internal = (
                *self.roots,
                *(
                    requirement
                    for requirements in self.incoming.values()
                    for requirement in requirements
                ),
            )
        return self.requirements_internal

    def constrained_requirements(
        self, apply: Callable[[Requirement], Requirement]
    ) -> tuple[Requirement, ...]:
        if self.constrained_internal is None:
            self.constrained_internal = tuple(
                apply(item) for item in self.requirements()
            )
        return self.constrained_internal

    def constrained_roots(
        self, apply: Callable[[Requirement], Requirement]
    ) -> tuple[Requirement, ...]:
        if self.constrained_roots_internal is None:
            self.constrained_roots_internal = tuple(apply(item) for item in self.roots)
        return self.constrained_roots_internal

    def set_incoming(self, source: str, requirements: tuple[Requirement, ...]) -> None:
        self.incoming[source] = requirements
        self.requirements_internal = None
        self.constrained_internal = None
        self.incoming_version_masks.pop(source, None)
        self.active_version_mask = None
        self.decision_count = None

    def remove_incoming(self, source: str) -> None:
        self.incoming.pop(source, None)
        self.requirements_internal = None
        self.constrained_internal = None
        self.incoming_version_masks.pop(source, None)
        self.active_version_mask = None
        self.decision_count = None


Assignment = tuple[int, int, frozenset[str]]
RequirementStateKey = tuple[str, str, tuple[str, ...], str, str, str]
INCREMENTAL_STATE_KEY_THRESHOLD = 16


def requirement_state_key(requirement: Requirement) -> RequirementStateKey:
    return (
        requirement.canonical_name,
        str(requirement.specifier),
        tuple(sorted(requirement.extras)),
        requirement.url or "",
        requirement.marker or "",
        requirement.raw,
    )


class LearnedIncompatibility:
    __slots__ = ("terms", "watches", "decision_levels", "activity", "last_used")

    def __init__(
        self,
        terms: frozenset[Assignment],
        watches: tuple[int, int],
        decision_levels: tuple[tuple[Assignment, int], ...] = (),
    ) -> None:
        self.terms = terms
        self.watches = watches
        self.decision_levels = decision_levels
        self.activity = 0
        self.last_used = 0
