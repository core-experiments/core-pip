"""Implementation of the ``cpip show`` command."""

from __future__ import annotations

import sys

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip show")
    parser.add_argument("-f", "--files", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("packages", nargs="*")
    return parser


def run_show(args: list[str]) -> int:
    from cpip.build.show import iter_installed_package_info
    from cpip.core.packaging import canonicalize_name

    options = create_parser().parse_args(args)
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
