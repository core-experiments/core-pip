"""Validation helpers for installed distribution metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pip.core.packaging import (
    Requirement,
    Version,
    canonicalize_name,
    default_environment,
    marker_applies,
    parse_requirement,
)
from pip.core.wheel import WheelTag, wheel_tag_rank

from .metadata import InstalledMetadataDistribution

PackageSet = dict[str, "PackageDetails"]


@dataclass(frozen=True)
class PackageDetails:
    version: Version
    dependencies: tuple[Requirement, ...]
    requested_extras: frozenset[str] = frozenset()

    @classmethod
    def from_dependencies(
        cls,
        version: Version,
        dependencies: list[Requirement],
        requested_extras: frozenset[str] = frozenset(),
    ) -> "PackageDetails":
        return cls(version, tuple(dependencies), requested_extras)


def marker_allows(requirement: Requirement, requested_extras: frozenset[str]) -> bool:
    if not requirement.marker:
        return True
    if not requested_extras:
        return evaluate_marker(requirement.marker, "")
    return any(evaluate_marker(requirement.marker, extra) for extra in requested_extras)


def evaluate_marker(marker: str, extra: str) -> bool:
    text = marker.strip()
    if text.startswith("extra !="):
        value = text.split("!=", 1)[1].strip().strip("\"'")
        return default_environment(extra)["extra"] != value
    if text.startswith("extra =="):
        value = text.split("==", 1)[1].strip().strip("\"'")
        return default_environment(extra)["extra"] == value
    return True


def check_package_set(
    package_set: PackageSet,
) -> tuple[
    dict[str, list[tuple[str, Requirement]]],
    dict[str, list[tuple[str, str, Requirement]]],
]:
    missing: dict[str, list[tuple[str, Requirement]]] = {}
    conflicting: dict[str, list[tuple[str, str, Requirement]]] = {}
    for name, details in package_set.items():
        for requirement in details.dependencies:
            if not marker_allows(requirement, details.requested_extras):
                continue
            canonical = canonicalize_name(requirement.name)
            dependency = package_set.get(canonical)
            if dependency is None:
                missing.setdefault(name, []).append((canonical, requirement))
                continue
            if not requirement.is_satisfied_by(dependency.version):
                conflicting.setdefault(name, []).append(
                    (canonical, str(dependency.version), requirement)
                )
    return missing, conflicting


def parse_installed_dependencies(
    dist: InstalledMetadataDistribution,
) -> list[Requirement]:
    """Parse the active dependency declarations of an installed distribution."""
    result = []
    for value in dist.iter_raw_dependencies():
        requirement = parse_requirement(value)
        if marker_applies(requirement.marker, extras=()):
            result.append(requirement)
    return result


def metadata_errors(
    distributions: Iterable[InstalledMetadataDistribution],
) -> list[str]:
    """Return human-readable errors for malformed dependency metadata."""
    errors = []
    for dist in distributions:
        for value in dist.iter_raw_dependencies():
            if count_unquoted(value, ";") > 1:
                errors.append(f"Error parsing dependencies of {dist.raw_name}")
                break
            try:
                parse_requirement(value)
            except ValueError as exc:
                errors.append(f"Error parsing dependencies of {dist.raw_name}: {exc}")
                break
    return errors


def unsupported_distributions(
    distributions: Iterable[InstalledMetadataDistribution],
    supported_tags: Iterable[WheelTag],
) -> list[InstalledMetadataDistribution]:
    """Return distributions whose wheel tags are unsupported."""
    supported = tuple(supported_tags)
    result = []
    for dist in distributions:
        try:
            wheel_text = dist.read_text("WHEEL")
        except FileNotFoundError:
            continue
        tags = []
        for line in wheel_text.splitlines():
            if not line.startswith("Tag:"):
                continue
            parts = line.split(":", 1)[1].strip().split("-")
            if len(parts) == 3:
                tags.append(WheelTag(*parts))
        if tags and wheel_tag_rank(tuple(tags), supported) is None:
            result.append(dist)
    return result


def count_unquoted(value: str, target: str) -> int:
    count = 0
    quote: str | None = None
    for char in value:
        if char in {"'", '"'}:
            quote = None if quote == char else char
        elif char == target and quote is None:
            count += 1
    return count
