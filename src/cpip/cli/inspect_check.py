"""Implementation of the ``cpip check`` subcommand.

Split out of ``cli/inspect.py`` so its cost (the full installed-metadata and
dependency-query stack) is not paid by the other three inspection commands.
"""

from __future__ import annotations


def run_check(args: list[str]) -> int:
    from cpip.cli.parsers.inspect import create_check_parser

    create_check_parser().parse_args(args)

    import sys

    from cpip.build import metadata as build_metadata
    from cpip.build import query
    from cpip.core import cpip_version, packaging, target_python

    distributions = build_metadata.InstalledDistributionStore().iter(
        skip=cpip_version.CPIP_DISTRIBUTION_NAMES
    )
    package_set = query.package_set_from_dependencies(
        distributions,
        query.installed_dependencies_by_name(distributions),
    )

    errors = query.metadata_errors(distributions)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    unsupported = [
        f"{dist.raw_name} {dist.raw_version} is not supported on this platform"
        for dist in query.unsupported_distributions(
            distributions, target_python.get_supported()
        )
    ]

    missing, conflicting = query.check_package_set(package_set)

    if not missing and not conflicting and not unsupported:
        print("No broken requirements found.")
        return 0

    for line in unsupported:
        print(line)

    for name, requirements in sorted(missing.items()):
        distribution = next(
            dist
            for dist in distributions
            if dist.canonical_name == packaging.canonicalize_name(name)
        )
        for _, requirement in requirements:
            print(
                f"{name} {distribution.version} requires "
                f"{packaging.canonicalize_name(requirement.name)}, which is not installed.",
            )

    for name, requirements in sorted(conflicting.items()):
        distribution = next(
            dist
            for dist in distributions
            if dist.canonical_name == packaging.canonicalize_name(name)
        )
        for conflict_name, version, requirement in requirements:
            print(
                f"{name} {distribution.version} has requirement {requirement}, "
                f"but you have {conflict_name} {version}.",
            )

    return 1
