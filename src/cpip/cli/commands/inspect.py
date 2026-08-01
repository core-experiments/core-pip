"""Implementation of the ``cpip inspect`` command."""

from __future__ import annotations

import json

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip inspect")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    return parser


def run_inspect(args: list[str]) -> int:
    from cpip.build.metadata import InstalledDistributionStore
    from cpip.core.cpip_version import get_cpip_version
    from cpip.core.metadata import stdlib_pkgs
    from cpip.core.packaging import default_environment
    from cpip.core.urls import path_to_url

    options = create_parser().parse_args(args)
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
