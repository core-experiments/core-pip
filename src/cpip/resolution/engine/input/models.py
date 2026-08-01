"""Protocols for requirement values crossing into the resolution engine."""

from __future__ import annotations

from typing import Any, Protocol


class RequirementInput(Protocol):
    """Installer-provided requirement data consumed by resolution.

    The concrete installer requirement also carries build and preparation
    state.  Resolution depends only on this smaller structural contract.
    """

    req: Any
    link: Any
    hash_options: dict[str, list[str]]
    constraint: bool
    satisfied_by: Any
    editable: bool
    user_supplied: bool

    @property
    def name(self) -> str | None: ...

    @property
    def extras(self) -> set[str]: ...

    @property
    def markers(self) -> str | None: ...

    def is_satisfied_by(self, candidate: object) -> bool: ...
