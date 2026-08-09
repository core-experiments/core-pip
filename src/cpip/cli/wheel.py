"""Implementation of the ``cpip wheel`` command."""

from __future__ import annotations

import os
import shutil

from cpip.build.build import build_wheel_from_source
from cpip.cli.config import load_source_config, resolve_sources
from cpip.cli.dependency_groups import group_items, parse_dependency_groups
from cpip.cli.parsers.wheel import create_parser
from cpip.cli.requirements import (
    apply_proxy_environment,
    bundle_install_requirements,
    collect_requirements,
    config_settings,
)
from cpip.core.errors import CommandError
from cpip.core.format_control import FormatControl
from cpip.core.wheel import wheel_candidate
from cpip.index.provider import CandidateProvider
from cpip.resolution.api import ResolutionEngine
from cpip.resolution.input_requirements import install_req_from_line


def run_wheel(args: list[str]) -> int:
    options = create_parser().parse_args([arg for arg in args if arg])

    sources = resolve_sources(options, load_source_config("wheel"))

    parsed_config_settings = config_settings(options.config_settings)

    grouped_requirements = parse_dependency_groups(group_items(options.groups))

    bundle = collect_requirements(
        requirements=[*options.requirements, *grouped_requirements],
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        editables=options.editables,
        requirement_config_settings={
            requirement: dict(parsed_config_settings)
            for requirement in options.requirements
        },
        editable_config_settings={
            editable: dict(parsed_config_settings) for editable in options.editables
        },
        find_links=sources.find_links,
        index_url=sources.index_url,
        extra_index_urls=sources.extra_index_urls,
        no_index=sources.no_index,
        format_control=FormatControl(),
        proxy=options.proxy,
    )

    apply_proxy_environment(options.proxy)

    raw_requirements = [*bundle.requirements, *bundle.editables]

    if not raw_requirements and not options.requirement_files and not options.groups:
        raise CommandError(
            'You must give at least one requirement to wheel (see "cpip help wheel")',
        )

    requirements = bundle_install_requirements(bundle)
    for editable in bundle.editables:
        item = install_req_from_line(editable)
        item.editable = True
        item.config_settings = bundle.editable_config_settings.get(editable, {})
        requirements.append(item)

    build_options: dict[str, dict[str, object]] = {}

    for requirement in requirements:
        if not requirement.config_settings or requirement.req is None:
            continue

        settings = dict(requirement.config_settings)

        build_options[requirement.req.raw] = settings

        if requirement.req.url is not None:
            build_options[requirement.req.url] = settings

        if requirement.link is not None:
            build_options[requirement.link.url] = settings

    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        build_options=build_options,
        build_constraints=options.build_constraint_files,
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
        build_isolation=not options.no_build_isolation,
    )

    plan = ResolutionEngine(
        provider=provider,
        no_deps=options.no_deps,
        constraints=bundle.constraints,
    ).resolve(requirements)

    wheel_dir = os.fspath(options.wheel_dir)

    os.makedirs(wheel_dir, exist_ok=True)

    built_names: list[str] = []

    for candidate in plan.candidates:
        source = candidate.path

        if os.path.splitext(os.fspath(source))[1] != ".whl":
            source = build_wheel_from_source(
                source,
                wheel_dir=wheel_dir,
                config_settings=build_options.get(candidate.source_url or ""),
                build_constraints=options.build_constraint_files,
                build_isolation=not options.no_build_isolation,
            )

        else:
            destination = os.path.join(wheel_dir, os.path.basename(source))

            if os.path.realpath(source) != os.path.realpath(
                destination,
            ):
                shutil.copy2(source, destination)

            source = destination

        built_names.append(wheel_candidate(source).name)

    if built_names:
        print(f"Successfully built {' '.join(sorted(set(built_names)))}")

    return 0
