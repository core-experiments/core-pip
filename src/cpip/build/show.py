"""Installed-package inspection data used by presentation layers."""

from __future__ import annotations

import string
from collections.abc import Iterator
from typing import NamedTuple

from cpip.core.packaging import canonicalize_name

from .metadata import InstalledDistributionStore, InstalledMetadataDistribution


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
