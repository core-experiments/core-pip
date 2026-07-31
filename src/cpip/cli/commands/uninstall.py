"""Implementation of the ``cpip uninstall`` command."""

from __future__ import annotations

import os
import site
from pathlib import Path

from cpip.build.metadata import InstalledDistributionStore
from cpip.cli.context import target_paths
from cpip.cli.parser import ArgumentParser
from cpip.core.packaging import parse_requirement
from cpip.install.requirements import RequirementInstaller


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip uninstall")
    parser.add_argument("packages", nargs="*")
    parser.add_argument(
        "-r", "--requirement", dest="requirement_files", action="append", default=[]
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-y", "--yes", action="store_true")
    return parser


def run_uninstall(args: list[str]) -> int:
    parser = create_parser()
    options = parser.parse_args(args)
    packages = list(options.packages)
    for filename in options.requirement_files:
        for line in Path(filename).read_text(encoding="utf-8").splitlines():
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
            paths=paths, user_site=site.getusersitepackages()
        ).find(package)
        if options.verbose and distribution is not None:
            location = Path(distribution.location)
            if len(location.parents) >= 3:
                scripts = "Scripts" if os.name == "nt" else "bin"
                print(f"Uninstalling files from {location.parents[2] / scripts}")
        if paths is None:
            removed_now = RequirementInstaller().uninstall(package)
        else:
            removed_now = RequirementInstaller().uninstall(package, paths=paths)
        if removed_now:
            removed.append(package)
    for package in removed:
        print(f"Successfully uninstalled {package}")
    return 0
