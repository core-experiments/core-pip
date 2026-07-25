"""Implementation of the ``pip check`` command."""

from __future__ import annotations

import sys

from pip.build.check import (
    PackageDetails,
    check_package_set,
    metadata_errors,
    parse_installed_dependencies,
    unsupported_distributions,
)
from pip.build.metadata import InstalledDistributionStore
from pip.cli.parser import ArgumentParser
from pip.core.packaging import Version, canonicalize_name
from pip.core.target_python import get_supported as get_supported_tags


def create_parser() -> ArgumentParser:
    return ArgumentParser(prog="pip check")


def run_check(args: list[str]) -> int:
    create_parser().parse_args(args)
    distributions = InstalledDistributionStore().iter(skip={"pip"})
    package_set = {
        dist.canonical_name: PackageDetails.from_dependencies(
            Version(dist.raw_version),
            parse_installed_dependencies(dist),
        )
        for dist in distributions
    }
    errors = metadata_errors(distributions)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    unsupported = [
        f"{dist.raw_name} {dist.raw_version} is not supported on this platform"
        for dist in unsupported_distributions(distributions, get_supported_tags())
    ]
    missing, conflicting = check_package_set(package_set)
    if not missing and not conflicting and not unsupported:
        print("No broken requirements found.")
        return 0
    for line in unsupported:
        print(line)
    for name, requirements in sorted(missing.items()):
        distribution = next(
            dist
            for dist in distributions
            if dist.canonical_name == canonicalize_name(name)
        )
        for _, requirement in requirements:
            print(
                f"{name} {distribution.version} requires "
                f"{canonicalize_name(requirement.name)}, which is not installed."
            )
    for name, requirements in sorted(conflicting.items()):
        distribution = next(
            dist
            for dist in distributions
            if dist.canonical_name == canonicalize_name(name)
        )
        for conflict_name, version, requirement in requirements:
            print(
                f"{name} {distribution.version} has requirement {requirement}, "
                f"but you have {conflict_name} {version}."
            )
    return 1
