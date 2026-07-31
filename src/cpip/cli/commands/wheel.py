"""Implementation of the ``cpip wheel`` command."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cpip.build.build import build_wheel_from_source
from cpip.cli.dependency_groups import parse_dependency_groups
from cpip.cli.parser import ArgumentParser
from cpip.cli.requirements import collect_requirements, load_source_config
from cpip.core.errors import CommandError
from cpip.core.format_control import FormatControl
from cpip.core.wheel import wheel_candidate
from cpip.index.provider import CandidateProvider
from cpip.resolution.req_install import install_req_from_line
from cpip.resolution.resolver import Resolver


def create_parser() -> ArgumentParser:
    """Build wheels for requirements without installing them."""

    parser = ArgumentParser(prog="cpip wheel")
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--group", dest="groups", action="append", default=[])
    parser.add_argument(
        "-r", "--requirement", dest="requirement_files", action="append", default=[]
    )
    parser.add_argument(
        "-c", "--constraint", dest="constraint_files", action="append", default=[]
    )
    parser.add_argument(
        "--build-constraint", dest="build_constraint_files", action="append", default=[]
    )
    parser.add_argument(
        "-e", "--editable", dest="editables", action="append", default=[]
    )
    parser.add_argument("-f", "--find-links", action="append", default=[])
    parser.add_argument("-i", "--index-url")
    parser.add_argument("--extra-index-url", action="append", default=[])
    parser.add_argument(
        "--trusted-host", dest="trusted_hosts", action="append", default=[]
    )
    parser.add_argument("--proxy")
    parser.add_argument(
        "--use-feature", dest="use_features", action="append", default=[]
    )
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument(
        "--config-settings",
        "--config-setting",
        dest="config_settings",
        action="append",
        default=[],
    )
    parser.add_argument("-w", "--wheel-dir", default=".")
    return parser


def run_wheel(args: list[str]) -> int:
    options = create_parser().parse_args([arg for arg in args if arg])

    config = load_source_config("wheel")
    config_settings: dict[str, object] = {}
    for value in options.config_settings:
        key, separator, payload = value.partition("=")
        config_settings[key] = payload if separator else ""
    group_items = [("pyproject.toml", group) for group in options.groups]
    grouped_requirements = parse_dependency_groups(group_items)
    bundle = collect_requirements(
        requirements=[*options.requirements, *grouped_requirements],
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        editables=options.editables,
        requirement_config_settings={
            requirement: dict(config_settings) for requirement in options.requirements
        },
        editable_config_settings={
            editable: dict(config_settings) for editable in options.editables
        },
        find_links=options.find_links or config.find_links,
        index_url=options.index_url or config.index_url,
        extra_index_urls=options.extra_index_url or config.extra_index_urls,
        no_index=options.no_index or config.no_index,
        format_control=FormatControl(),
        proxy=options.proxy,
    )
    if options.proxy:
        os.environ["CPIP_PROXY"] = options.proxy
        os.environ["HTTP_PROXY"] = options.proxy
        os.environ["HTTPS_PROXY"] = options.proxy
        os.environ["http_proxy"] = options.proxy
        os.environ["https_proxy"] = options.proxy
    raw_requirements = [*bundle.requirements, *bundle.editables]
    if not raw_requirements and not options.requirement_files and not options.groups:
        raise CommandError(
            'You must give at least one requirement to wheel (see "cpip help wheel")'
        )
    requirements = []
    for requirement in raw_requirements:
        item = install_req_from_line(requirement)
        if requirement in bundle.requirement_config_settings:
            item.config_settings = bundle.requirement_config_settings[requirement]
        elif requirement in bundle.editable_config_settings:
            item.config_settings = bundle.editable_config_settings[requirement]
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
    plan = Resolver(
        provider=provider,
        no_deps=options.no_deps,
        constraints=bundle.constraints,
    ).resolve(requirements)
    wheel_dir = Path(options.wheel_dir)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    built_names: list[str] = []
    for candidate in plan.candidates:
        source = candidate.path
        if source.suffix != ".whl":
            source = build_wheel_from_source(
                source,
                wheel_dir=wheel_dir,
                config_settings=build_options.get(candidate.source_url or ""),
                build_constraints=options.build_constraint_files,
                build_isolation=not options.no_build_isolation,
            )
        else:
            destination = wheel_dir / source.name
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            source = destination
        built_names.append(wheel_candidate(source).name)
    if built_names:
        print(f"Successfully built {' '.join(sorted(set(built_names)))}")
    return 0
