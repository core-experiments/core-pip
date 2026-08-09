"""Implementation of the ``cpip uninstall`` command."""

from __future__ import annotations

import os
import site

from cpip.build.metadata import InstalledDistributionStore
from cpip.cli.parsers.uninstall import create_parser
from cpip.cli.target import target_paths
from cpip.core.packaging import parse_requirement
from cpip.install.requirements import RequirementInstaller


def run_uninstall(args: list[str]) -> int:
    parser = create_parser()

    options = parser.parse_args(args)

    packages = list(options.packages)

    for filename in options.requirement_files:
        with open(filename, encoding="utf-8") as file:
            lines = file.read().splitlines()

        for line in lines:
            requirement = line.partition("#")[0].strip()

            if not requirement:
                continue

            packages.append(parse_requirement(requirement).name)

    if not packages:
        parser.error("You must give at least one package to uninstall")

    removed: list[str] = []

    for package in packages:
        paths = target_paths()

        distribution = InstalledDistributionStore(
            paths=paths,
            user_site=site.getusersitepackages(),
        ).find(package)

        if options.verbose and distribution is not None:
            location = distribution.location

            parent = os.path.dirname(
                os.path.dirname(os.path.dirname(location)),
            )

            if parent != os.path.dirname(parent):
                scripts = "Scripts" if os.name == "nt" else "bin"

                print(f"Uninstalling files from {os.path.join(parent, scripts)}")

        if paths is None:
            removed_now = RequirementInstaller().uninstall(package)

        else:
            removed_now = RequirementInstaller().uninstall(package, paths=paths)

        if removed_now:
            removed.append(package)

    for package in removed:
        print(f"Successfully uninstalled {package}")

    return 0
