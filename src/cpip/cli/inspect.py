"""Implementation of the cpip inspection and check subcommands."""

from __future__ import annotations

import os
import sys

from cpip.build import metadata as build_metadata
from cpip.build import query
from cpip.cli.parsers.inspect import (
    create_check_parser,
    create_hash_parser,
    create_inspect_parser,
    create_show_parser,
)
from cpip.core import cpip_version, packaging, target_python, urls
from cpip.core import metadata as core_metadata


def run_check(args: list[str]) -> int:
    create_check_parser().parse_args(args)

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


def run_hash(args: list[str]) -> int:
    import hashlib

    options = create_hash_parser().parse_args(args)
    for filename in options.files:
        digest = hashlib.new(options.algorithm)
        with open(filename, "rb") as file:
            while True:
                block = file.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        print(
            f"{os.path.basename(filename)}: --hash={options.algorithm}:{digest.hexdigest()}",
        )
    return 0


def run_show(args: list[str]) -> int:
    options = create_show_parser().parse_args(args)

    if not options.packages:
        print("ERROR: Please provide a package name or names.", file=sys.stderr)
        return 1

    infos = {
        info.distribution.canonical_name: info
        for info in query.iter_installed_package_info(
            options.packages,
            include_files=options.files,
        )
    }

    missing = sorted(
        package
        for package in options.packages
        if packaging.canonicalize_name(package) not in infos
    )

    printed = 0
    for package in options.packages:
        info = infos.get(packaging.canonicalize_name(package))
        if info is None:
            continue

        dist = info.distribution
        if printed:
            print("---")
        printed += 1

        metadata = dist.metadata
        project_urls = metadata.get_all("Project-URL", [])

        print(f"Name: {dist.raw_name}")
        print(f"Version: {dist.raw_version}")
        print(f"Summary: {metadata.get('Summary', '')}")
        print(f"Home-page: {info.homepage}")
        print(f"Author: {metadata.get('Author', '')}")
        print(f"Author-email: {metadata.get('Author-email', '')}")

        metadata_version = dist.metadata_version or ""
        metadata_version_tuple = (
            tuple(map(int, metadata_version.split("."))) if metadata_version else ()
        )

        if metadata_version_tuple >= (2, 4) and metadata.get("License-Expression"):
            print(f"License-Expression: {metadata.get('License-Expression', '')}")
        else:
            print(f"License: {metadata.get('License', '')}")

        print(f"Location: {dist.location}")
        if dist.editable and dist.editable_project_location is not None:
            print(f"Editable project location: {dist.editable_project_location}")

        print(f"Requires: {', '.join(info.requires)}")
        print(f"Required-by: {', '.join(info.required_by)}")

        if options.verbose:
            print(f"Metadata-Version: {dist.metadata_version or ''}")
            print(f"Installer: {dist.installer}")
            print("Classifiers:")
            for classifier in metadata.get_all("Classifier", []):
                print(f"  {classifier}")
            print("Entry-points:")
            for entry_point in info.entry_points:
                print(f"  {entry_point}")
            print("Project-URLs:")
            for project_url in project_urls:
                print(f"  {project_url}")

        if options.files:
            print("Files:")
            files = info.files or []
            if files:
                for filename in files:
                    print(f"  {filename}")
            else:
                print("Cannot locate RECORD or installed-files.txt")
            print()

    if missing:
        print(f"WARNING: Package(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1 if len(missing) == len(options.packages) else 0

    return 0


def run_inspect(args: list[str]) -> int:
    options = create_inspect_parser().parse_args(args)

    distributions = build_metadata.InstalledDistributionStore(
        paths=options.path or None,
    ).iter(
        local_only=options.local,
        user_only=options.user,
        skip=set(core_metadata.stdlib_pkgs),
    )

    installed = []
    for dist in distributions:
        item: dict[str, object] = {
            "metadata": dist.metadata_dict,
            "metadata_location": dist.info_location,
        }

        direct_url = dist.direct_url
        if direct_url is not None:
            item["direct_url"] = direct_url.to_dict_compat()
        elif (location := dist.editable_project_location) is not None:
            item["direct_url"] = {
                "url": urls.path_to_url(location),
                "dir_info": {"editable": True},
            }

        if dist.installer:
            item["installer"] = dist.installer

        if dist.installed_with_dist_info:
            item["requested"] = dist.requested

        installed.append(item)

    import json

    print(
        json.dumps(
            {
                "version": "1",
                "cpip_version": cpip_version.get_cpip_version(),
                "installed": installed,
                "environment": packaging.default_environment(),
            },
        ),
    )

    return 0
