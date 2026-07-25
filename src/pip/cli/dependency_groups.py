from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pip.core.errors import InstallationError
from pip.core.packaging import canonicalize_name


def parse_dependency_groups(items: list[tuple[str, str]]) -> list[str]:
    requirements: list[str] = []
    for file_name, group_name in items:
        requirements.extend(_resolve_group_file(Path(file_name), group_name))
    return requirements


def _resolve_group_file(path: Path, group_name: str) -> list[str]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise InstallationError(f"{path.name} not found.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InstallationError(f"Error parsing {path.name}") from exc
    except OSError as exc:
        raise InstallationError(f"Error reading {path.name}") from exc

    groups = data.get("dependency-groups")
    if not isinstance(groups, dict):
        raise InstallationError(
            f"[dependency-groups] table was missing from {path.name!r}."
        )

    try:
        return _resolve_group(groups, group_name, stack=[])
    except InstallationError as exc:
        raise InstallationError(
            f"[dependency-groups] resolution failed for {group_name!r} from {path.name!r}: {exc}"
        ) from exc


def _resolve_group(
    groups: dict[str, Any], group_name: str, *, stack: list[str]
) -> list[str]:
    if group_name in stack:
        cycle = ", ".join(
            f"{left} -> {right}" for left, right in zip(stack, stack[1:] + [group_name])
        )
        raise InstallationError(
            f"Cyclic dependency group include while resolving {stack[0]}: {cycle}"
        )

    actual_group_name = group_name if group_name in groups else None
    if actual_group_name is None:
        normalized = canonicalize_name(group_name)
        for key in groups:
            if canonicalize_name(key) == normalized:
                actual_group_name = key
                break
    raw_group = groups.get(actual_group_name)
    if not isinstance(raw_group, list):
        raise InstallationError(
            f"Dependency group {group_name!r} was not defined as a list."
        )

    resolved: list[str] = []
    next_stack = [*stack, actual_group_name or group_name]
    for item in raw_group:
        if isinstance(item, str):
            resolved.append(item)
            continue
        if isinstance(item, dict) and set(item) == {"include-group"}:
            include = item["include-group"]
            if not isinstance(include, str):
                raise InstallationError(
                    f"Dependency group {group_name!r} contains an invalid include."
                )
            resolved.extend(_resolve_group(groups, include, stack=next_stack))
            continue
        raise InstallationError(
            f"Dependency group {group_name!r} contains an invalid item."
        )
    return resolved
