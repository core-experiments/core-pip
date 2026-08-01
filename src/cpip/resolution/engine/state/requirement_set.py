from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from cpip.core.packaging import canonicalize_name

if TYPE_CHECKING:
    from cpip.resolution.engine.input.models import RequirementInput


RequirementT = TypeVar("RequirementT", bound="RequirementInput")


class RequirementSet(Generic[RequirementT]):
    __slots__ = ("named_internal", "unnamed")

    def __init__(
        self,
        named_internal: dict[str, RequirementT] | None = None,
        unnamed: list[RequirementT] | None = None,
    ) -> None:
        self.named_internal = named_internal if named_internal is not None else {}
        self.unnamed = unnamed if unnamed is not None else []

    @staticmethod
    def name_internal(requirement: RequirementInput) -> str | None:
        name = requirement.name
        if name is not None:
            return name
        parsed = requirement.req
        return parsed.name if parsed is not None else None

    def add_named_requirement(self, requirement: RequirementT) -> None:
        name = self.name_internal(requirement)
        if not name:
            raise ValueError("named requirements must define a parsed requirement")
        self.named_internal[canonicalize_name(name)] = requirement

    def add_unnamed_requirement(self, requirement: RequirementT) -> None:
        self.unnamed.append(requirement)

    def has_requirement(self, name: str) -> bool:
        normalized = canonicalize_name(name)
        return (
            normalized in self.named_internal
            and not self.named_internal[normalized].constraint
        )

    def get_requirement(self, name: str) -> RequirementT:
        normalized = canonicalize_name(name)
        if normalized in self.named_internal:
            return self.named_internal[normalized]
        raise KeyError(f"No project with the name {name!r}")

    @property
    def requirements(self) -> dict[str, RequirementT]:
        return dict(self.named_internal)

    @property
    def unnamed_requirements(self) -> list[RequirementT]:
        return list(self.unnamed)

    @property
    def all_requirements(self) -> list[RequirementT]:
        return [*self.named_internal.values(), *self.unnamed]

    @property
    def requirements_to_install(self) -> list[RequirementT]:
        return [
            requirement
            for requirement in self.all_requirements
            if not requirement.constraint and not requirement.satisfied_by
        ]
