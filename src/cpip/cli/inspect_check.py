"""Implementation of the ``cpip check`` subcommand.

Split out of ``cli/inspect.py`` so its cost (the full installed-metadata and
dependency-query stack) is not paid by the other three inspection commands.
"""

from __future__ import annotations


def run_check(args: list[str]) -> int:
    from cpip.cli.parsers.inspect import create_check_parser

    create_check_parser().parse_args(args)

    import sys

    from cpip.build import query
    from cpip.core import cpip_version, light_metadata, packaging

    distributions = light_metadata.LightDistributionStore().iter(
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

    def supported_tags():  # noqa: ANN202
        # Deferred: the tag machinery (core.wheel, sysconfig, platform) only
        # for an environment holding a wheel that is not py3-none-any.
        from cpip.core import target_python

        return target_python.get_supported()

    unsupported = [
        f"{dist.raw_name} {dist.raw_version} is not supported on this platform"
        for dist in query.unsupported_distributions(distributions, supported_tags)
    ]

    missing, conflicting = query.check_package_set(package_set)

    if not missing and not conflicting and not unsupported:
        print("No broken requirements found.")
        return 0

    for line in unsupported:
        print(line)

    by_name = {dist.canonical_name: dist for dist in distributions}

    for name, requirements in sorted(missing.items()):
        distribution = by_name[packaging.canonicalize_name(name)]
        for _, requirement in requirements:
            print(
                f"{name} {distribution.raw_version} requires "
                f"{packaging.canonicalize_name(requirement.name)}, which is not installed.",
            )

    for name, requirements in sorted(conflicting.items()):
        distribution = by_name[packaging.canonicalize_name(name)]
        for conflict_name, version, requirement in requirements:
            print(
                f"{name} {distribution.raw_version} has requirement {requirement}, "
                f"but you have {conflict_name} {version}.",
            )

    return 1
