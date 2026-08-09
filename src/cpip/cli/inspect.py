"""Implementation of the cpip inspection and check subcommands."""

from __future__ import annotations

import hashlib
import json
import os
import sys

from cpip.build.query import (
    check_package_set,
    installed_dependencies_by_name,
    metadata_errors,
    package_set_from_dependencies,
    unsupported_distributions,
    iter_installed_package_info,
)
from cpip.build.metadata import InstalledDistributionStore
from cpip.cli.common import ArgumentParser
from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES, get_cpip_version
from cpip.core.metadata import stdlib_pkgs
from cpip.core.packaging import canonicalize_name, default_environment
from cpip.core.target_python import get_supported as get_supported_tags
from cpip.core.urls import path_to_url


# ==============================================================================
# cpip check
# ==============================================================================

def create_check_parser() -> ArgumentParser:
    return ArgumentParser(prog="cpip check")


def run_check(args: list[str]) -> int:
    create_check_parser().parse_args(args)

    distributions = InstalledDistributionStore().iter(skip=CPIP_DISTRIBUTION_NAMES)
    package_set = package_set_from_dependencies(
        distributions,
        installed_dependencies_by_name(distributions),
    )

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
                f"{canonicalize_name(requirement.name)}, which is not installed.",
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
                f"but you have {conflict_name} {version}.",
            )

    return 1


# ==============================================================================
# cpip hash
# ==============================================================================

def create_hash_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip hash")
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "-a",
        "--algorithm",
        default="sha256",
        choices=sorted(hashlib.algorithms_available),
    )
    return parser


def run_hash(args: list[str]) -> int:
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


# ==============================================================================
# cpip show
# ==============================================================================

def create_show_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip show")
    parser.add_argument("-f", "--files", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("packages", nargs="*")
    return parser


def run_show(args: list[str]) -> int:
    options = create_show_parser().parse_args(args)

    if not options.packages:
        print("ERROR: Please provide a package name or names.", file=sys.stderr)
        return 1

    infos = {
        info.distribution.canonical_name: info
        for info in iter_installed_package_info(
            options.packages,
            include_files=options.files,
        )
    }

    missing = sorted(
        package
        for package in options.packages
        if canonicalize_name(package) not in infos
    )

    printed = 0
    for package in options.packages:
        info = infos.get(canonicalize_name(package))
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


# ==============================================================================
# cpip inspect
# ==============================================================================

def create_inspect_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip inspect")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    return parser


def run_inspect(args: list[str]) -> int:
    options = create_inspect_parser().parse_args(args)

    distributions = InstalledDistributionStore(
        paths=options.path or None,
    ).iter(
        local_only=options.local,
        user_only=options.user,
        skip=set(stdlib_pkgs),
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
                "url": path_to_url(location),
                "dir_info": {"editable": True},
            }

        if dist.installer:
            item["installer"] = dist.installer

        if dist.installed_with_dist_info:
            item["requested"] = dist.requested

        installed.append(item)

    print(
        json.dumps(
            {
                "version": "1",
                "cpip_version": get_cpip_version(),
                "installed": installed,
                "environment": default_environment(),
            },
        ),
    )

    return 0
