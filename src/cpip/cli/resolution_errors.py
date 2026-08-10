"""Translate resolver reports into the CLI's diagnostic contract.

The resolver explains an incompatibility in its own terms ("because no
versions of X are available ..."). Commands that resolve -- ``install`` and
``download`` -- must instead emit the messages users and tests expect, such as
"No matching distribution found for X==1.0". This module owns that mapping so
both commands report a failure identically.
"""

from __future__ import annotations

import re

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


def resolution_error_message(
    message: str,
    requirements: list[Any],
    release_control: list[tuple[str, str]],
) -> str:
    """Translate resolver incompatibility reports to the CLI diagnostic contract."""
    if message.startswith("No matching distribution found for "):
        return message.splitlines()[0]
    if message.startswith("because no versions of "):
        unavailable = re.match(
            r"because no versions of ([A-Za-z0-9_.-]+) ([^\s]+) are available",
            message,
        )
        root_names = {
            getattr(requirement.req, "name", "").lower()
            for requirement in requirements
            if requirement.req is not None
        }
        if unavailable is not None and unavailable.group(1).lower() not in root_names:
            return (
                f"No matching distribution found for "
                f"{unavailable.group(1)}=={unavailable.group(2)}"
            )
        return message.splitlines()[0]

    root = next(
        (
            requirement.req.raw
            for requirement in requirements
            if requirement.req is not None
        ),
        None,
    )
    if any(name == "only-final" for name, _ in release_control) and root is not None:
        return f"Could not find a final version that satisfies the requirement {root}"

    missing_root = re.search(
        r"because your project depends on ([A-Za-z0-9_.-]+)(?: ([^\s<]+))? <empty>",
        message,
    )
    if missing_root is not None:
        name, version = missing_root.groups()
        return f"No matching distribution found for {name}{version or ''}"

    dependency = re.search(
        r"because (?!your project )[^\n]+ depends on ([A-Za-z0-9_.-]+)(?:(==|!=|<=|>=|~=|<|>)([^\s]+)| ([^\s<]+))?(?: <empty>)?",
        message,
    )
    if dependency is not None:
        name, operator, value, spaced = dependency.groups()
        if operator and value:
            return f"No matching distribution found for {name}{operator}{value}"
        if spaced:
            return f"No matching distribution found for {name} {spaced}"
        return f"Could not find a version that satisfies the requirement {name.replace('-', '_')}"

    if root is None:
        return message
    return f"Could not find a version that satisfies the requirement {root}"
