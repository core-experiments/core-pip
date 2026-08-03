from __future__ import annotations

import sys

from cpip.build.check import (
    PackageDetails,
    check_package_set,
    parse_installed_dependencies,
)
from cpip.build.metadata import InstalledDistributionStore
from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
from cpip.core.packaging import Version, canonicalize_name


def warn_about_install_conflicts(changed_names: set[str]) -> None:
    """Warn about broken requirements affected by the current install."""

    distributions = InstalledDistributionStore().iter(skip=CPIP_DISTRIBUTION_NAMES)

    distributions_by_name = {dist.canonical_name: dist for dist in distributions}

    dependencies_by_name = {}

    dependents_by_name: dict[str, set[str]] = {}

    for dist in distributions:
        dependencies = parse_installed_dependencies(dist)

        dependencies_by_name[dist.canonical_name] = dependencies

        for requirement in dependencies:
            dependents_by_name.setdefault(
                canonicalize_name(requirement.name),
                set(),
            ).add(dist.canonical_name)

    package_set = {
        dist.canonical_name: PackageDetails.from_dependencies(
            Version(dist.raw_version),
            dependencies_by_name[dist.canonical_name],
        )
        for dist in distributions
    }

    affected = set(changed_names)

    pending = list(changed_names)

    while pending:
        dependency = pending.pop()

        for dependent in dependents_by_name.get(dependency, ()):
            if dependent not in affected:
                affected.add(dependent)

                pending.append(dependent)

    missing, conflicting = check_package_set(package_set)

    for name, requirements in sorted(missing.items()):
        if name not in affected:
            continue

        distribution = distributions_by_name[name]

        for _, requirement in requirements:
            print(
                f"{distribution.canonical_name} {distribution.version} requires "
                f"{requirement.name}, which is not installed.",
                file=sys.stderr,
            )

    for name, requirements in sorted(conflicting.items()):
        if name not in affected:
            continue

        distribution = distributions_by_name[name]

        for dependency_name, version, requirement in requirements:
            print(
                f"{distribution.canonical_name} {distribution.version} requires "
                f"{requirement}, but you have {dependency_name} {version} which is incompatible.",
                file=sys.stderr,
            )
