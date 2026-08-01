from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from cpip._vendor import tomli as tomllib
from pathlib import Path
from typing import Any, cast

from cpip.core.errors import InstallationError
from cpip.core.packaging import canonicalize_name


def parse_dependency_groups(items: list[tuple[str, str]]) -> list[str]:
    requirements: list[str] = []
    for file_name, group_name in items:
        requirements.extend(resolve_group_file(Path(file_name), group_name))
    return requirements


def resolve_group_file(path: Path, group_name: str) -> list[str]:
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
            f"[dependency-groups] table was missing from {path.name!r}.",
        )

    try:
        return resolve_group(groups, group_name, stack=[])
    except InstallationError as exc:
        raise InstallationError(
            f"[dependency-groups] resolution failed for {group_name!r} from {path.name!r}: {exc}",
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
                include = cast("dict[str, Any]", item)["include-group"]
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
