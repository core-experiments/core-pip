"""Implementation of the ``cpip inspect`` subcommand.

``check``, ``hash``, and ``show`` used to live in this file too, but their
import needs diverge sharply -- ``hash`` touches only ``hashlib``, while this
command and ``check`` need most of the metadata stack.  Each now has its own
module (``cli/inspect_check.py``, ``cli/inspect_hash.py``,
``cli/inspect_show.py``) and its own ``CommandSpec`` entry in
``cli/registry.py``, so each pays only for what it reaches.
"""

from __future__ import annotations


def run_inspect(args: list[str]) -> int:
    import json

    from cpip.build import metadata as build_metadata
    from cpip.cli.parsers.inspect import create_inspect_parser
    from cpip.core import cpip_version, packaging, urls
    from cpip.core import metadata as core_metadata

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
