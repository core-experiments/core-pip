"""Requirement-file result and parse-error models."""

from __future__ import annotations

from cpip.core.errors import InstallationError


class RequirementsFileParseError(InstallationError):
    """A requirements file could not be parsed."""


class ParsedRequirement:
    __slots__ = (
        "comes_from",
        "constraint",
        "is_editable",
        "line_source",
        "locked_direct",
        "locked_hashes",
        "locked_link",
        "locked_name",
        "options",
        "requirement",
    )

    def __init__(
        self,
        requirement: str,
        comes_from: str,
        is_editable: bool = False,
        constraint: bool = False,
        options: dict[str, object] | None = None,
        line_source: str | None = None,
        locked_link: str | None = None,
        locked_hashes: dict[str, list[str]] | None = None,
        locked_direct: bool = False,
        locked_name: str | None = None,
    ) -> None:
        self.requirement = requirement
        self.comes_from = comes_from
        self.is_editable = is_editable
        self.constraint = constraint
        self.options = options
        self.line_source = line_source
        self.locked_link = locked_link
        self.locked_hashes = locked_hashes
        self.locked_direct = locked_direct
        self.locked_name = locked_name
