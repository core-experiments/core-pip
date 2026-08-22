from __future__ import annotations

import os

from cpip.core.errors import InstallationError
from cpip.core.names import canonicalize_name

from typing import Any


def group_items(values: list[str]) -> list[tuple[str, str]]:
    """Split ``--group`` values into ``(pyproject file, group name)`` pairs.

    A bare ``NAME`` reads the group from ``pyproject.toml``; ``FILE:NAME``
    names the file explicitly.
    """

    result: list[tuple[str, str]] = []
    for value in values:
        filename, separator, group = value.partition(":")
        if separator:
            result.append((filename, group))
        else:
            result.append(("pyproject.toml", filename))
    return result


def parse_dependency_groups(items: list[tuple[str, str]]) -> list[str]:
    requirements: list[str] = []
    for file_name, group_name in items:
        requirements.extend(resolve_group_file(file_name, group_name))
    return requirements


def toml_module() -> Any:
    """The TOML parser, imported on first use: only a --group install reads
    TOML, and the import is not free."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        from cpip._vendor import tomli as tomllib
    return tomllib


def __getattr__(name: str) -> Any:
    if name == "tomllib":
        return toml_module()
    raise AttributeError(name)


def resolve_group_file(path: str, group_name: str) -> list[str]:
    tomllib = toml_module()

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise InstallationError(f"{os.path.basename(path)} not found.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InstallationError(f"Error parsing {os.path.basename(path)}") from exc
    except OSError as exc:
        raise InstallationError(f"Error reading {os.path.basename(path)}") from exc

    groups = data.get("dependency-groups")
    if not isinstance(groups, dict):
        raise InstallationError(
            f"[dependency-groups] table was missing from {os.path.basename(path)!r}.",
        )

    try:
        return resolve_group(groups, group_name, stack=[])
    except InstallationError as exc:
        raise InstallationError(
            f"[dependency-groups] resolution failed for {group_name!r} from {os.path.basename(path)!r}: {exc}",
        ) from exc


def resolve_group(
    groups: dict[str, Any],
    group_name: str,
    *,
    stack: list[str],
) -> list[str]:
    resolved: list[str] = []
    canonical_groups = {canonicalize_name(name): name for name in groups}
    pending: list[tuple[str, Any, list[str]]] = [("group", group_name, stack)]
    while pending:
        kind, payload, current_stack = pending.pop()
        if kind == "value":
            resolved.append(payload)
            continue
        current_name = payload
        if current_name in current_stack:
            cycle = ", ".join(
                f"{left} -> {right}"
                for left, right in zip(
                    current_stack,
                    current_stack[1:] + [current_name],
                )
            )
            root = current_stack[0] if current_stack else current_name
            raise InstallationError(
                f"Cyclic dependency group include while resolving {root}: {cycle}",
            )

        actual_name = current_name if current_name in groups else None
        if actual_name is None:
            actual_name = canonical_groups.get(canonicalize_name(current_name))
        raw_group = groups.get(actual_name)
        if not isinstance(raw_group, list):
            raise InstallationError(
                f"Dependency group {current_name!r} was not defined as a list.",
            )

        next_stack = [*current_stack, actual_name or current_name]
        for item in reversed(raw_group):
            if isinstance(item, str):
                pending.append(("value", item, next_stack))
                continue
            if isinstance(item, dict) and set(item) == {"include-group"}:
                include = item["include-group"]  # ty:ignore[invalid-argument-type]
                if not isinstance(include, str):
                    raise InstallationError(
                        f"Dependency group {current_name!r} contains an invalid include.",
                    )
                pending.append(("group", include, next_stack))
                continue
            raise InstallationError(
                f"Dependency group {current_name!r} contains an invalid item.",
            )
    return resolved
