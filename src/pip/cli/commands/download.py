"""Implementation of the ``pip download`` command."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from pip.build.build import build_wheel_from_source
from pip.cli.parser import ArgumentParser
from pip.cli.requirements import (
    _bundle_install_requirements,
    _collect_requirements,
    _load_source_config,
)
from pip.core.format_control import FormatControl
from pip.index.artifacts import ArtifactLocator
from pip.index.provider import CandidateProvider
from pip.install.editable import prepare_editable_source
from pip.resolution.resolver import Resolver


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="pip download")
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--group", dest="groups", action="append", default=[])
    parser.add_argument(
        "-r", "--requirement", dest="requirement_files", action="append", default=[]
    )
    parser.add_argument(
        "-c", "--constraint", dest="constraint_files", action="append", default=[]
    )
    parser.add_argument("-f", "--find-links", action="append", default=[])
    parser.add_argument("-i", "--index-url")
    parser.add_argument("--extra-index-url", action="append", default=[])
    parser.add_argument(
        "--trusted-host", dest="trusted_hosts", action="append", default=[]
    )
    parser.add_argument("--proxy")
    parser.add_argument("--cert")
    parser.add_argument("--client-cert")
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--no-cache-dir", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("-d", "--dest", required=True)
    return parser


def run_download(args: list[str]) -> int:
    options = create_parser().parse_args(args)
    config = _load_source_config("download")
    bundle = _collect_requirements(
        requirements=options.requirements,
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        find_links=options.find_links or config.find_links,
        index_url=options.index_url or config.index_url,
        extra_index_urls=options.extra_index_url or config.extra_index_urls,
        no_index=options.no_index or config.no_index,
        format_control=FormatControl(),
        cert=options.cert,
        client_cert=options.client_cert,
        proxy=options.proxy,
    )
    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
    )
    plan = Resolver(
        provider=provider,
        constraints=bundle.constraints,
        ignore_installed=True,
    ).resolve(_bundle_install_requirements(bundle))
    destination = Path(options.dest)
    destination.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for editable in bundle.editables:
        source_path, _, _ = prepare_editable_source(editable, prepare_metadata=False)
        wheel = build_wheel_from_source(
            source_path,
            wheel_dir=destination,
            build_isolation=not options.no_build_isolation,
        )
        names.append(wheel.stem.split("-", 1)[0])
    for candidate in plan.candidates:
        source = candidate.path
        if candidate.source_kind == "sdist" and candidate.source_url is not None:
            source = ArtifactLocator(bundle.session).ensure_local(candidate.source_url)
            # Build isolation commonly consumes setuptools from a local wheel
            # directory. Preserve the materialized wheel for that bootstrap
            # dependency when the index did not offer a compatible wheel URL.
            if candidate.canonical_name == "setuptools":
                source = candidate.path
        shutil.copy2(source, destination / source.name)
        names.append(candidate.name)
    if names:
        message = f"Successfully downloaded {' '.join(sorted(names))}"
        if (
            sys.stdout.isatty()
            and not options.no_color
            and "NO_COLOR" not in os.environ
        ):
            message = f"\x1b[32m{message}\x1b[0m"
        print(message)
    return 0
