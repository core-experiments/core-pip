"""Unified helpers for querying, filtering, and validating installed distributions."""

from __future__ import annotations

import json
import string
from collections.abc import Collection, Iterable, Iterator, Mapping
from email.parser import Parser
from typing import TYPE_CHECKING, Any, NamedTuple

from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
from cpip.core.packaging import (
    Requirement,
    Version,
    canonicalize_name,
    default_environment,
    marker_applies,
    parse_requirement,
)
from cpip.core.wheel import WheelTag, wheel_tag_rank

from .metadata import InstalledDistributionStore, InstalledMetadataDistribution

if TYPE_CHECKING:
    pass

LatestInfo = Mapping[str, tuple[Any, str]]
PackageSet = dict[str, "PackageDetails"]


def normalize_project_url_label(label: str) -> str:
    """Normalize a project URL label according to PEP 753."""
    chars_to_remove = string.punctuation + string.whitespace
    return label.translate(str.maketrans("", "", chars_to_remove)).lower()


class InstalledPackageInfo(NamedTuple):
    distribution: InstalledMetadataDistribution
    requires: list[str]
    required_by: list[str]
    entry_points: list[str]
    files: list[str] | None
    homepage: str


def iter_installed_package_info(
    query: list[str],
    *,
    include_files: bool = False,
) -> Iterator[InstalledPackageInfo]:
    """Collect presentation-neutral information for named distributions."""
    installed = {
        dist.canonical_name: dist for dist in InstalledDistributionStore().iter()
    }
    query_names = [canonicalize_name(name) for name in query]
    for query_name in query_names:
        dist = installed.get(query_name)
        if dist is None:
            continue

        try:
            requires = sorted(
                {requirement.name for requirement in dist.iter_dependencies()},
                key=str.lower,
            )
        except ValueError:
            requires = sorted(dist.iter_raw_dependencies(), key=str.lower)

        required_by: list[str] = []
        for candidate in installed.values():
            try:
                names = {
                    requirement.name for requirement in candidate.iter_dependencies()
                }
            except ValueError:
                required_by = ["#N/A"]
                break
            if dist.canonical_name in {canonicalize_name(name) for name in names}:
                required_by.append(candidate.raw_name)

        try:
            entry_points = dist.read_text("entry_points.txt").splitlines()
        except FileNotFoundError:
            entry_points = []

        files = sorted(dist.iter_declared_entries()) if include_files else None
        project_urls = dist.metadata.get_all("Project-URL", [])
        homepage = dist.metadata.get("Home-page", "")
        if not homepage:
            for project_url in project_urls:
                label, url = project_url.split(",", maxsplit=1)
                if normalize_project_url_label(label) == "homepage":
                    homepage = url.strip()
                    break

        yield InstalledPackageInfo(
            distribution=dist,
            requires=requires,
            required_by=sorted(required_by, key=str.lower),
            entry_points=entry_points,
            files=files,
            homepage=homepage,
        )


def select_installed_distributions(
    *,
    paths: list[str] | None = None,
    local_only: bool = False,
    user_only: bool = False,
    editables_only: bool = False,
    include_editables: bool = True,
    excludes: Iterable[str] = (),
    not_required: bool = False,
    skip: Collection[str] = (),
    user_site: str | None = None,
) -> list[InstalledMetadataDistribution]:
    """Return installed distributions after applying listing filters."""
    excluded = {canonicalize_name(name) for name in excludes}

    if "pip" in excluded:
        excluded.update(canonicalize_name(name) for name in CPIP_DISTRIBUTION_NAMES)

    distributions = list(
        InstalledDistributionStore(paths=paths, user_site=user_site).iter(
            local_only=local_only,
            user_only=user_only,
            editables_only=editables_only,
            include_editables=include_editables,
            skip=skip,
        ),
    )

    if not_required:
        dependency_names = {
            canonicalize_name(requirement.name)
            for dist in distributions
            for requirement in dist.iter_dependencies()
        }

        distributions = [
            dist
            for dist in distributions
            if dist.canonical_name not in dependency_names
        ]

    return [dist for dist in distributions if dist.canonical_name not in excluded]


def format_list_columns(
    distributions: list[InstalledMetadataDistribution],
    *,
    outdated: bool = False,
    verbose: bool = False,
    latest: LatestInfo | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Build rows and headers for the columns list format."""
    header = ["Package", "Version"]

    if outdated:
        header.extend(("Latest", "Type"))

    build_tags = []

    for dist in distributions:
        try:
            wheel_text = dist.read_text("WHEEL")
        except FileNotFoundError:
            build_tags.append(None)
        else:
            build_tags.append(Parser().parsestr(wheel_text).get("Build"))

    if any(build_tags):
        header.append("Build")

    has_editables = any(dist.editable for dist in distributions)

    if has_editables:
        header.append("Editable project location")

    if verbose:
        header.extend(("Location", "Installer"))

    rows = []

    for index, dist in enumerate(distributions):
        row = [dist.raw_name, dist.raw_version]

        if outdated:
            version, filetype = (latest or {})[dist.canonical_name]
            row.extend((str(version), filetype))

        if any(build_tags):
            row.append(build_tags[index] or "")

        if has_editables:
            row.append(dist.editable_project_location or "")

        if verbose:
            row.extend((dist.location, dist.installer))

        rows.append(row)

    return rows, header


def format_list_json(
    distributions: list[InstalledMetadataDistribution],
    *,
    outdated: bool = False,
    verbose: bool = False,
    latest: LatestInfo | None = None,
) -> str:
    """Build JSON for the list format."""
    data = []

    for dist in distributions:
        try:
            version = str(dist.version)
        except ValueError:
            version = dist.raw_version

        info: dict[str, Any] = {"name": dist.raw_name, "version": version}

        if verbose:
            info["location"] = dist.location
            info["installer"] = dist.installer

        if dist.editable_project_location:
            info["editable_project_location"] = dist.editable_project_location

        if outdated:
            latest_version, filetype = (latest or {})[dist.canonical_name]
            info["latest_version"] = str(latest_version)
            info["latest_filetype"] = filetype

        data.append(info)

    return json.dumps(data)


def format_list_freeze(
    distributions: list[InstalledMetadataDistribution],
    *,
    verbose: bool = False,
) -> list[str]:
    """Build lines for the list freeze format."""
    result = []

    for dist in distributions:
        try:
            requirement = f"{dist.raw_name}=={dist.version}"
        except ValueError:
            requirement = f"{dist.raw_name}==={dist.raw_version}"

        if verbose:
            requirement = f"{requirement} ({dist.location})"

        result.append(requirement)

    return result


class PackageDetails:
    __slots__ = ("dependencies", "requested_extras", "version")

    def __init__(
        self,
        version: Version,
        dependencies: tuple[Requirement, ...],
        requested_extras: frozenset[str] = frozenset(),
    ) -> None:
        self.version = version
        self.dependencies = dependencies
        self.requested_extras = requested_extras

    @classmethod
    def from_dependencies(
        cls,
        version: Version,
        dependencies: list[Requirement],
        requested_extras: frozenset[str] = frozenset(),
    ) -> PackageDetails:
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
                    (canonical, str(dependency.version), requirement),
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


def installed_dependencies_by_name(
    distributions: Iterable[InstalledMetadataDistribution],
) -> dict[str, list[Requirement]]:
    """Map each installed distribution's canonical name to its dependencies."""
    return {
        dist.canonical_name: parse_installed_dependencies(dist)
        for dist in distributions
    }


def package_set_from_dependencies(
    distributions: Iterable[InstalledMetadataDistribution],
    dependencies_by_name: dict[str, list[Requirement]],
) -> PackageSet:
    """Build the :func:`check_package_set` input from an installed environment.

    Callers pass the dependency map separately because the install command
    reuses it to index dependents; ``cpip check`` does not.
    """
    return {
        dist.canonical_name: PackageDetails.from_dependencies(
            Version(dist.raw_version),
            dependencies_by_name[dist.canonical_name],
        )
        for dist in distributions
    }


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
