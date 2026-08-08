"""Selection of installed distributions for package listings."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from email.parser import Parser
from typing import TYPE_CHECKING, Any

from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
from cpip.core.packaging import canonicalize_name

from .metadata import InstalledDistributionStore

if TYPE_CHECKING:
    from .metadata import InstalledMetadataDistribution


LatestInfo = Mapping[str, tuple[Any, str]]


def select_installed_distributions(
    *,
    paths: list[str] | None = None,
    local_only: bool = False,
    user_only: bool = False,
    editables_only: bool = False,
    include_editables: bool = True,
    excludes: Iterable[str] = (),
    not_required: bool = False,
    skip: Iterable[str] = (),
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
            skip=set(skip),
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
