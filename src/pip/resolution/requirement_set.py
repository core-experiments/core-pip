from __future__ import annotations

from dataclasses import dataclass, field

from pip.core.packaging import canonicalize_name

from pip.resolution.req_install import InstallRequirement


@dataclass
class RequirementSet:
    _named: dict[str, InstallRequirement] = field(default_factory=dict)
    _unnamed: list[InstallRequirement] = field(default_factory=list)

    @staticmethod
    def _name(requirement: InstallRequirement) -> str | None:
        name = requirement.name
        if name is not None:
            return name
        parsed = requirement.req
        return parsed.name if parsed is not None else None

    def add_named_requirement(self, requirement: InstallRequirement) -> None:
        name = self._name(requirement)
        if not name:
            raise ValueError("named requirements must define a parsed requirement")
        self._named[canonicalize_name(name)] = requirement

    def add_unnamed_requirement(self, requirement: InstallRequirement) -> None:
        self._unnamed.append(requirement)

    def has_requirement(self, name: str) -> bool:
        normalized = canonicalize_name(name)
        return normalized in self._named and not self._named[normalized].constraint

    def get_requirement(self, name: str) -> InstallRequirement:
        normalized = canonicalize_name(name)
        if normalized in self._named:
            return self._named[normalized]
        raise KeyError(f"No project with the name {name!r}")

    @property
    def requirements(self) -> dict[str, InstallRequirement]:
        return dict(self._named)

    @property
    def unnamed_requirements(self) -> list[InstallRequirement]:
        return list(self._unnamed)

    @property
    def all_requirements(self) -> list[InstallRequirement]:
        return [*self._named.values(), *self._unnamed]

    @property
    def requirements_to_install(self) -> list[InstallRequirement]:
        return [
            requirement
            for requirement in self.all_requirements
            if not requirement.constraint and not requirement.satisfied_by
        ]
