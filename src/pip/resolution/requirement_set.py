from __future__ import annotations

from dataclasses import dataclass, field

from pip.core.packaging import canonicalize_name

from pip.resolution.req_install import InstallRequirement


@dataclass
class RequirementSet:
    named_internal: dict[str, InstallRequirement] = field(default_factory=dict)
    unnamed: list[InstallRequirement] = field(default_factory=list)

    @staticmethod
    def name_internal(requirement: InstallRequirement) -> str | None:
        name = requirement.name
        if name is not None:
            return name
        parsed = requirement.req
        return parsed.name if parsed is not None else None

    def add_named_requirement(self, requirement: InstallRequirement) -> None:
        name = self.name_internal(requirement)
        if not name:
            raise ValueError("named requirements must define a parsed requirement")
        self.named_internal[canonicalize_name(name)] = requirement

    def add_unnamed_requirement(self, requirement: InstallRequirement) -> None:
        self.unnamed.append(requirement)

    def has_requirement(self, name: str) -> bool:
        normalized = canonicalize_name(name)
        return (
            normalized in self.named_internal
            and not self.named_internal[normalized].constraint
        )

    def get_requirement(self, name: str) -> InstallRequirement:
        normalized = canonicalize_name(name)
        if normalized in self.named_internal:
            return self.named_internal[normalized]
        raise KeyError(f"No project with the name {name!r}")

    @property
    def requirements(self) -> dict[str, InstallRequirement]:
        return dict(self.named_internal)

    @property
    def unnamed_requirements(self) -> list[InstallRequirement]:
        return list(self.unnamed)

    @property
    def all_requirements(self) -> list[InstallRequirement]:
        return [*self.named_internal.values(), *self.unnamed]

    @property
    def requirements_to_install(self) -> list[InstallRequirement]:
        return [
            requirement
            for requirement in self.all_requirements
            if not requirement.constraint and not requirement.satisfied_by
        ]
