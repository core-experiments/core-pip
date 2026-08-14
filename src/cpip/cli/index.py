"""Implementation of the ``cpip index`` command."""

from __future__ import annotations

import json

from cpip.cli.config import load_source_config, resolve_sources
from cpip.cli.parsers.index import create_parser
from cpip.core.format_control import FormatControl
from cpip.core.packaging import parse_requirement
from cpip.index.provider import CandidateProvider


def run_index(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    sources = resolve_sources(options, load_source_config("index"))

    provider = CandidateProvider.from_options(
        index_url=sources.index_url,
        extra_index_urls=sources.extra_index_urls,
        no_index=sources.no_index,
        format_control=FormatControl(),
        trusted_hosts=options.trusted_hosts,
    )

    requirement = parse_requirement(options.package)

    versions = provider.available_versions(requirement)

    if not options.pre:
        versions = tuple(
            version for version in versions if not version.version.is_prerelease
        )

    available = [str(version.version) for version in reversed(versions)]

    latest = available[0] if available else None

    if options.json:
        print(
            json.dumps(
                {"name": requirement.name, "versions": available, "latest": latest},
            ),
        )

        return 0

    print(f"{requirement.name} ({latest or 'none'})")

    print(f"Available versions: {', '.join(available) or 'none'}")

    return 0
