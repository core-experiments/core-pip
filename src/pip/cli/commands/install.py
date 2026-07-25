from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pip.build.build import build_editable_from_source
from pip.build.check import (
    PackageDetails,
    check_package_set,
    parse_installed_dependencies,
)
from pip.build.metadata import InstalledDistributionStore
from pip.cli.dependency_groups import parse_dependency_groups
from pip.cli.context import target_prefix as _target_prefix
from pip.cli.parser import ArgumentParser as _ArgumentParser
from pip.cli.requirements import (
    _bundle_install_requirements,
    _collect_requirements,
    _load_source_config,
    _requirements_from_script,
)
from pip.core.appdirs import user_cache_dir
from pip.core.errors import (
    CommandError,
    DistributionNotFound,
    InstallationError,
)
from pip.core.format_control import FormatControl
from pip.core.metadata import (
    find_installed,
    user_lib_path,
)
from pip.core.packaging import (
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from pip.core.wheel import TargetContext
from pip.index.links import Link
from pip.install.report import ReportItem, write_install_report

if TYPE_CHECKING:
    from pip.resolution.req_install import InstallRequirement


def run_install(args: list[str]) -> int:
    from pip.core.wheel import wheel_candidate
    from pip.index.provider import CandidateProvider
    from pip.install.editable import prepare_editable_source
    from pip.install.target import InstallTarget
    from pip.install.wheel_transaction import (
        WheelInstaller,
        install_wheels_transactionally,
    )
    from pip.resolution.req_install import install_req_from_line
    from pip.resolution.resolver import Resolver

    normalized_args: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-i", "--index-url"} and index + 1 < len(args):
            normalized_args.append(f"{token}={args[index + 1]}")
            index += 2
            continue
        normalized_args.append(token)
        index += 1
    parser = create_parser()
    options = parser.parse_args(normalized_args)
    if len(options.requirements_from_scripts) > 1:
        raise CommandError("--requirements-from-script can only be given once")
    if options.no_input:
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
    if any(Path(value).name == "requirements.txt" for value in options.requirements):
        print(
            "Hint: It looks like you are trying to install a requirements file. "
            "Use the -r option to install the file, or provide a package literally "
            'named "requirements.txt".',
        )
    for filename in options.build_constraint_files:
        if not Path(filename).is_file():
            raise InstallationError(
                f"Could not open requirements file: {filename}: "
                "No such file or directory"
            )
    for feature in options.use_features:
        if feature == "build-constraint":
            print(
                "WARNING: --use-feature=build-constraint is always enabled; "
                "the option is a no-op.",
                file=sys.stderr,
            )
    quiet = options.quiet > 0
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)
        os.environ["PIP_QUIET"] = "1"
    else:
        os.environ.pop("PIP_QUIET", None)
    config = _load_source_config("install")
    explicit_index_url = any(arg in {"-i", "--index-url"} for arg in args)
    config_settings: dict[str, object] = {}
    for value in options.config_settings:
        key, sep, payload = value.partition("=")
        config_settings[key] = payload if sep else ""
    group_items: list[tuple[str, str]] = []
    for group in options.groups:
        file_name, sep, group_name = group.partition(":")
        if sep:
            path = Path(file_name)
            if path.name != "pyproject.toml":
                parser.error("group paths use 'pyproject.toml' filenames")
            group_items.append((os.fspath(path), group_name))
            continue
        group_items.append(("pyproject.toml", file_name))
    grouped_requirements = parse_dependency_groups(group_items)
    script_requirements: list[str] = []
    if options.requirements_from_scripts:
        script_requirements = _requirements_from_script(
            Path(options.requirements_from_scripts[0])
        )
    format_control = FormatControl()
    index = 0
    while index < len(normalized_args):
        token = normalized_args[index]
        if token in {"--no-binary", "--only-binary"}:
            if index + 1 < len(normalized_args):
                format_control.apply(token[2:], normalized_args[index + 1])
            index += 2
            continue
        if token.startswith(("--no-binary=", "--only-binary=")):
            option, _, value = token.partition("=")
            format_control.apply(option[2:], value)
        index += 1
    if options.user and options.target:
        raise CommandError("Can not combine '--user' and '--target'")
    if options.user and options.prefix:
        raise CommandError("Can not combine '--user' and '--prefix'")
    if options.user:
        import site

        from pip.platform.virtualenv import running_under_virtualenv

        if not site.ENABLE_USER_SITE:
            if running_under_virtualenv():
                raise InstallationError(
                    "Can not perform a '--user' install. User site-packages are "
                    "not visible in this virtualenv."
                )
            raise InstallationError(
                "Can not perform a '--user' install. User site-packages are "
                "disabled for this Python."
            )
        if running_under_virtualenv():
            for raw_requirement in options.requirements:
                item = install_req_from_line(raw_requirement)
                if item.req is None:
                    continue
                installed = InstalledDistributionStore().find(item.req.name)
                if (
                    installed is not None
                    and not options.ignore_installed
                    and not Path(installed.location).is_relative_to(user_lib_path())
                    and installed.in_site_packages
                ):
                    raise InstallationError(
                        "Will not install to the user site because it will lack "
                        f"sys.path precedence to {installed.raw_name} in "
                        f"{installed.location}"
                    )
    if (
        not options.target
        and not options.dry_run
        and (options.platform or options.python_version or options.abi)
    ):
        raise CommandError(
            "Can not use any platform or abi specific options unless installing via "
            "'--target'"
        )
    if options.pre and (options.all_releases or options.only_final):
        raise CommandError("--pre cannot be used with --all-releases or --only-final")
    if (
        options.index_url
        and "://" not in options.index_url
        and not options.index_url.startswith(("http:", "https:", "file:"))
    ):
        print(
            f'WARNING: The index url "{options.index_url}" seems invalid, please provide a scheme.',
            file=sys.stderr,
        )
    release_control_args: list[tuple[str, str]] = []
    index = 0
    while index < len(normalized_args):
        token = normalized_args[index]
        if token in {"--all-releases", "--only-final"}:
            if index + 1 >= len(normalized_args):
                parser.error(f"{token} requires a value")
            release_control_args.append((token[2:], normalized_args[index + 1]))
            index += 2
            continue
        if token.startswith(("--all-releases=", "--only-final=")):
            option, _, value = token.partition("=")
            release_control_args.append((option[2:], value))
        elif token == "--pre":
            release_control_args.append(("pre", ":all:"))
        index += 1
    bundle = _collect_requirements(
        requirements=[
            *options.requirements,
            *grouped_requirements,
            *script_requirements,
        ],
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        editables=options.editables,
        requirement_config_settings={
            requirement: dict(config_settings) for requirement in options.requirements
        },
        editable_config_settings={
            editable: dict(config_settings) for editable in options.editables
        },
        find_links=[*config.find_links, *options.find_links],
        index_url=options.index_url if explicit_index_url else config.index_url,
        extra_index_urls=[
            *config.extra_index_urls,
            *options.extra_index_url,
        ],
        no_index=options.no_index or config.no_index,
        format_control=format_control,
        release_control_args=release_control_args,
        require_hashes=options.require_hashes,
        cert=options.cert,
        client_cert=options.client_cert,
        no_input=options.no_input,
        keyring_provider=options.keyring_provider,
        proxy=options.proxy,
    )
    if bundle.find_links:
        os.environ["PIP_FIND_LINKS"] = " ".join(bundle.find_links)
    if bundle.no_index:
        os.environ["PIP_NO_INDEX"] = "1"
    if (
        not bundle.requirements
        and not bundle.editables
        and not options.groups
        and not options.requirement_files
    ):
        raise CommandError(
            'You must give at least one requirement to install (see "pip help install")'
        )
    installed: list[str] = []
    installed_canonical_names: list[str] = []
    summary_root_names: set[str] = set()
    newly_installed_names: set[str] = set()
    reported_satisfied: set[str] = set()
    report_items: list[ReportItem] = []
    reinstall = options.force_reinstall or options.ignore_installed
    requested_roots: set[str] = set()
    requested_names: dict[str, str] = {}
    for requirement in bundle.requirements:
        item = install_req_from_line(requirement)
        name = item.req.name if item.req is not None else requirement
        canonical_name = canonicalize_name(name)
        requested_roots.add(canonical_name)
        requested_names.setdefault(canonical_name, name)
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    if options.python_version:
        value = str(options.python_version)
        if "." in value:
            parts = value.split(".")
            python_version = f"{parts[0]}.{parts[1]}.0" if len(parts) == 2 else value
        elif len(value) == 1:
            python_version = f"{value}.0.0"
        elif len(value) == 2:
            python_version = f"{value[0]}.{value[1]}.0"
    target = TargetContext(
        platforms=tuple(options.platform),
        implementation=options.implementation,
        python_version=(
            str(options.python_version)
            if options.python_version
            else f"{sys.version_info.major}{sys.version_info.minor}"
        ),
        abis=tuple(options.abi),
    )
    requirements = (
        _bundle_install_requirements(bundle, target=target)
        if bundle.requirements
        else []
    )
    requested_order = {
        requirement.req.canonical_name: index
        for index, requirement in enumerate(requirements)
        if requirement.req is not None
    }
    requested_source_urls = {
        url
        for requirement in requirements
        for url in (
            requirement.link.url if requirement.link is not None else None,
            requirement.req.url if requirement.req is not None else None,
        )
        if url is not None
    }
    summary_root_source_urls = {
        requirement.link.url
        for requirement in requirements
        if requirement.link is not None and requirement.link.is_existing_dir
    }
    build_options: dict[str, dict[str, object]] = {}
    for requirement in requirements:
        if not requirement.config_settings or requirement.req is None:
            continue
        build_options[requirement.req.raw] = dict(requirement.config_settings)
        if requirement.req.url is not None:
            build_options[requirement.req.url] = dict(requirement.config_settings)
        if requirement.link is not None:
            build_options[requirement.link.url] = dict(requirement.config_settings)
    source_requirements_by_name: dict[str, InstallRequirement] = {}
    requested_extras_by_name: dict[str, set[str]] = {}
    source_requirements_by_url: dict[str, InstallRequirement] = {}
    for requirement in requirements:
        if requirement.req is None:
            continue
        source_requirements_by_name[canonicalize_name(requirement.req.name)] = (
            requirement
        )
        requested_extras_by_name.setdefault(
            canonicalize_name(requirement.req.name), set()
        ).update(requirement.req.extras)
        if requirement.link is not None:
            source_requirements_by_url[requirement.link.url] = requirement
        if requirement.req.url is not None:
            source_requirements_by_url[requirement.req.url] = requirement
    for constraint in bundle.constraints:
        constraint_requirement = install_req_from_line(constraint)
        if constraint_requirement.req is None:
            continue
        canonical_name = canonicalize_name(constraint_requirement.req.name)
        source_requirements_by_name[canonical_name] = constraint_requirement
        if constraint_requirement.link is not None:
            source_requirements_by_url[constraint_requirement.link.url] = (
                constraint_requirement
            )
        if constraint_requirement.req.url is not None:
            source_requirements_by_url[constraint_requirement.req.url] = (
                constraint_requirement
            )
    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        build_options=build_options,
        build_constraints=options.build_constraint_files,
        wheel_cache_dir=(
            None
            if options.no_cache_dir
            else options.cache_dir
            or os.environ.get("PIP_CACHE_DIR")
            or user_cache_dir("pip")
        ),
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
        build_isolation=not options.no_build_isolation,
        locked_links={name: Link(url) for name, url in bundle.locked_links.items()},
        target=target,
        uploaded_prior_to=(
            datetime.datetime.fromisoformat(
                options.uploaded_prior_to.replace("Z", "+00:00")
            )
            if options.uploaded_prior_to
            else None
        ),
    )
    provider.release_control = bundle.release_control
    from pip.core.hashes import Hashes

    def intersect_hashes(left: Hashes, right: Hashes) -> Hashes:
        return Hashes(
            {
                algorithm: [
                    digest
                    for digest in left._allowed.get(algorithm, [])
                    if digest in right._allowed.get(algorithm, [])
                ]
                for algorithm in left._allowed.keys() & right._allowed.keys()
            }
        )

    provider.hashes_by_name = {}
    for item in requirements:
        if item.req is None or not item.hash_options:
            continue
        hashes = item.hashes()
        previous = provider.hashes_by_name.get(item.req.canonical_name)
        provider.hashes_by_name[item.req.canonical_name] = (
            hashes if previous is None else intersect_hashes(previous, hashes)
        )
    for raw, hashes in bundle.constraint_hashes.items():
        name = parse_requirement(raw).canonical_name
        current = provider.hashes_by_name.get(name)
        if current is None:
            from pip.core.hashes import Hashes

            provider.hashes_by_name[name] = Hashes(hashes)
        else:
            provider.hashes_by_name[name] = intersect_hashes(current, Hashes(hashes))

    for raw, hashes in bundle.requirement_hashes.items():
        name = parse_requirement(raw).canonical_name
        current = provider.hashes_by_name.get(name)
        provider.hashes_by_name[name] = (
            Hashes(hashes)
            if current is None
            else intersect_hashes(current, Hashes(hashes))
        )
    if options.verbose and bundle.no_index:
        print("Ignoring indexes:")
    if options.verbose and bundle.index_url:
        for requirement in requirements:
            if requirement.req is None:
                continue
            print(
                "Getting page "
                f"{bundle.index_url.rstrip('/')}/{requirement.req.canonical_name}"
            )
    if options.verbose and bundle.find_links:
        for find_link in bundle.find_links:
            if find_link.startswith(("http://", "https://")):
                print(f"Fetching project page and analyzing links: {find_link}")

    def install_candidate(
        candidate: Any,
        *,
        requested: bool,
        direct_url: Any = None,
    ) -> None:
        target = InstallTarget.from_options(
            candidate.canonical_name,
            target=options.target,
            user=options.user,
            root=options.root,
            prefix=options.prefix or _target_prefix(),
        )
        WheelInstaller(
            target,
            pycompile=not options.no_compile,
            force=(
                reinstall or (direct_url is not None and direct_url.is_local_editable())
            ),
            preserve_existing=options.ignore_installed,
        ).install(
            candidate.path,
            requested=requested,
            direct_url=direct_url,
        )

    preinstalled_editables: set[str] = set()
    preinstalled_editable_reports: dict[str, tuple[Any, Any]] = {}
    if bundle.editables:
        from pip.install.editable import prepare_editable_source

        for editable in bundle.editables:
            source_path, direct_url, metadata = prepare_editable_source(
                editable, build_isolation=not options.no_build_isolation
            )
            if (
                metadata is None
                or metadata.dependencies
                or metadata.optional_dependencies
            ):
                continue
            built = build_editable_from_source(
                source_path,
                config_settings=bundle.editable_config_settings.get(editable),
                build_constraints=options.build_constraint_files,
                build_isolation=not options.no_build_isolation,
            )
            candidate = wheel_candidate(built)
            for raw_constraint in bundle.constraints:
                constraint = parse_requirement(raw_constraint)
                if (
                    constraint.canonical_name == candidate.canonical_name
                    and constraint.url is None
                    and not constraint.is_satisfied_by(
                        candidate.version, allow_prereleases=options.pre
                    )
                ):
                    raise InstallationError(
                        f"Cannot install {candidate.name} {candidate.version} because "
                        f"these package versions have conflicting dependencies."
                    )
            if not options.dry_run:
                install_candidate(candidate, requested=True, direct_url=direct_url)
            installed.append(f"{candidate.name}-{candidate.version}")
            installed_canonical_names.append(candidate.canonical_name)
            newly_installed_names.add(candidate.canonical_name)
            preinstalled_editables.add(editable)
            preinstalled_editable_reports[editable] = (candidate, direct_url)

    if bundle.find_links and not quiet:
        print(f"Looking in links: {', '.join(bundle.find_links)}")
    if bundle.requirements:
        try:
            plan = Resolver(
                provider=provider,
                no_deps=options.no_deps,
                upgrade=options.upgrade,
                upgrade_strategy=options.upgrade_strategy,
                ignore_installed=reinstall,
                constraints=bundle.constraints,
                allow_prereleases=options.pre,
                require_hashes=bundle.require_hashes,
                ignore_requires_python=options.ignore_requires_python,
                python_version=python_version,
            ).resolve(requirements)
        except DistributionNotFound as exc:
            if options.verbose:
                message = str(exc)
                detail = next(
                    (
                        line
                        for line in message.splitlines()
                        if line.startswith("No matching distribution found for ")
                    ),
                    message,
                )
                print(f"DistributionNotFound: {detail}")
            raise
        unique_candidates: dict[str, Any] = {}
        for candidate in plan.candidates:
            unique_candidates.setdefault(candidate.canonical_name, candidate)
        plan.candidates = list(unique_candidates.values())
        for item in plan.satisfied:
            requested = item.requirement.raw or item.requirement.name
            if not quiet:
                if options.upgrade:
                    print(
                        f"Requirement already satisfied: {requested} in "
                        f"{item.distribution.location}"
                    )
                else:
                    print(f"Requirement already satisfied: {requested}")
            reported_satisfied.add(requested)
        if plan.candidates and not quiet:
            print(
                "Installing collected packages: "
                + ", ".join(
                    requested_names.get(candidate.canonical_name, candidate.name)
                    for candidate in plan.candidates
                )
            )
        if plan.candidates:
            batch_target = InstallTarget.from_options(
                plan.candidates[0].canonical_name,
                target=options.target,
                user=options.user,
                root=options.root,
                prefix=options.prefix or _target_prefix(),
            )
            candidate_direct_urls: dict[str, Any] = {}
            for candidate in plan.candidates:
                source_requirement = source_requirements_by_name.get(
                    candidate.canonical_name
                ) or source_requirements_by_url.get(candidate.source_url or "")
                direct_url = None
                if (
                    source_requirement is not None
                    and source_requirement.link is not None
                    and source_requirement.req is not None
                    and source_requirement.req.url is not None
                ):
                    from pip.resolution.direct_url_helpers import direct_url_from_link

                    direct_url = direct_url_from_link(source_requirement.link)
                candidate_direct_urls[os.fspath(candidate.path)] = direct_url
            if not options.dry_run:
                try:
                    install_wheels_transactionally(
                        [
                            (
                                candidate.path,
                                candidate.canonical_name in requested_roots,
                                candidate_direct_urls[os.fspath(candidate.path)],
                            )
                            for candidate in plan.candidates
                        ],
                        target=batch_target,
                        pycompile=not options.no_compile,
                        force=reinstall,
                        preserve_existing=options.ignore_installed,
                    )
                except InstallationError as exc:
                    prefix = "Cannot install "
                    message = str(exc)
                    if message.startswith(prefix):
                        conflict_name = message[len(prefix) :].split(":", 1)[0]
                        for candidate in plan.candidates:
                            if candidate.canonical_name == conflict_name:
                                print(
                                    f"The user requested {candidate.canonical_name} "
                                    f"{candidate.version}"
                                )
                    raise
        ordered_candidates = (
            sorted(
                plan.candidates,
                key=lambda candidate: (
                    (
                        0,
                        requested_order[candidate.canonical_name],
                    )
                    if candidate.canonical_name in requested_order
                    else (1, plan.candidates.index(candidate))
                ),
            )
            if options.user
            else plan.candidates
        )
        for candidate in ordered_candidates:
            display_name = requested_names.get(candidate.canonical_name, candidate.name)
            installed.append(f"{display_name}-{candidate.version}")
            installed_canonical_names.append(candidate.canonical_name)
            newly_installed_names.add(candidate.canonical_name)
        plan_order = {
            id(candidate): index for index, candidate in enumerate(plan.candidates)
        }
        report_candidates = sorted(
            plan.candidates,
            key=lambda candidate: (
                (
                    0,
                    requested_order[candidate.canonical_name],
                )
                if candidate.canonical_name in requested_order
                else (1, plan_order[id(candidate)])
            ),
        )
        for candidate in report_candidates:
            if candidate.source_url in requested_source_urls:
                requested_roots.add(candidate.canonical_name)
                requested_names.setdefault(candidate.canonical_name, candidate.name)
            if candidate.source_url in summary_root_source_urls:
                summary_root_names.add(candidate.canonical_name)
            if not quiet:
                provenance = None
                for parent in plan.candidates:
                    if candidate.canonical_name not in plan.graph.get(
                        parent.canonical_name, set()
                    ):
                        continue
                    parent_name = requested_names.get(
                        parent.canonical_name, parent.name
                    )
                    parent_extras = sorted(
                        requested_extras_by_name.get(parent.canonical_name, ())
                    )
                    provenance = (
                        f"{parent_name}[{','.join(parent_extras)}]"
                        if parent_extras
                        else parent_name
                    )
                    if parent_extras:
                        break
                suffix = f" (from {provenance})" if provenance else ""
                print(f"Processing {candidate.path}{suffix}")
            source_requirement = source_requirements_by_name.get(
                candidate.canonical_name
            ) or source_requirements_by_url.get(candidate.source_url or "")
            requested_extras = tuple(
                sorted(requested_extras_by_name.get(candidate.canonical_name, ()))
            )
            if source_requirement is not None and source_requirement.req is not None:
                requested_extras = tuple(
                    sorted(set(requested_extras) | set(source_requirement.req.extras))
                )
            report_items.append(
                ReportItem(
                    candidate_name=candidate.name,
                    candidate_version=str(candidate.version),
                    requested=candidate.canonical_name in requested_roots,
                    source_url=candidate.source_url,
                    source_hashes=candidate.source_hashes,
                    yanked=candidate.yanked_reason is not None,
                    is_direct=(
                        candidate.canonical_name in bundle.locked_direct_names
                        or (
                            source_requirement is not None
                            and source_requirement.req is not None
                            and source_requirement.req.url is not None
                        )
                    ),
                    requested_extras=requested_extras,
                    requires_dist=tuple(
                        str(dependency) for dependency in candidate.dependencies
                    ),
                )
            )
    for editable in bundle.editables:
        if editable in preinstalled_editables:
            candidate, direct_url = preinstalled_editable_reports[editable]
            report_items.append(
                ReportItem(
                    candidate_name=candidate.name,
                    candidate_version=str(candidate.version),
                    requested=True,
                    source_url=direct_url.url if direct_url is not None else None,
                    source_hashes=None,
                    yanked=False,
                    is_direct=direct_url is not None,
                    editable=True,
                )
            )
            continue
        source_path, direct_url, metadata = prepare_editable_source(editable)
        built = build_editable_from_source(
            source_path,
            config_settings=bundle.editable_config_settings.get(editable),
            build_constraints=options.build_constraint_files,
            build_isolation=not options.no_build_isolation,
        )
        built_candidate = wheel_candidate(built)
        editable_requirement = install_req_from_line(editable)
        for raw_constraint in bundle.constraints:
            constraint = parse_requirement(raw_constraint)
            if constraint.canonical_name != built_candidate.canonical_name:
                continue
            if constraint.url is None and not constraint.is_satisfied_by(
                built_candidate.version,
                allow_prereleases=options.pre,
            ):
                raise InstallationError(
                    f"Cannot install {built_candidate.name} "
                    f"{built_candidate.version} because it does not satisfy "
                    f"the constraint {raw_constraint}"
                )
        editable_dependencies = [
            dependency
            for dependency in built_candidate.dependencies
            if marker_applies(
                parse_requirement(str(dependency)).marker,
                extras=(
                    editable_requirement.req.extras
                    if editable_requirement.req is not None
                    else ()
                ),
            )
        ]
        if metadata is not None and editable_requirement.req is not None:
            editable_dependencies = [
                dependency
                for dependency in metadata.dependencies
                if marker_applies(
                    parse_requirement(str(dependency)).marker,
                    extras=editable_requirement.req.extras,
                )
            ]
            for extra in editable_requirement.req.extras:
                editable_dependencies.extend(
                    metadata.optional_dependencies.get(extra, ())
                )
        if not options.no_deps and editable_dependencies:
            dependency_plan = Resolver(
                provider=provider,
                no_deps=False,
                upgrade=options.upgrade and options.upgrade_strategy == "eager",
                upgrade_strategy=options.upgrade_strategy,
                ignore_installed=reinstall,
                constraints=bundle.constraints,
                allow_prereleases=options.pre,
                require_hashes=bundle.require_hashes,
                ignore_requires_python=options.ignore_requires_python,
                python_version=python_version,
            ).resolve(
                [
                    install_req_from_line(str(requirement))
                    for requirement in editable_dependencies
                ]
            )
            for candidate in dependency_plan.candidates:
                report_items.append(
                    ReportItem(
                        candidate_name=candidate.name,
                        candidate_version=str(candidate.version),
                        requested=False,
                        source_url=candidate.source_url,
                        source_hashes=candidate.source_hashes,
                        yanked=candidate.yanked_reason is not None,
                    )
                )
                if not options.dry_run:
                    install_candidate(candidate, requested=False)
                installed.append(f"{candidate.name}-{candidate.version}")
                installed_canonical_names.append(candidate.canonical_name)
        if options.dry_run:
            candidate = wheel_candidate(built)
        else:
            candidate = wheel_candidate(built)
            install_candidate(candidate, requested=True, direct_url=direct_url)
        installed.append(f"{candidate.name}-{candidate.version}")
        installed_canonical_names.append(candidate.canonical_name)
        newly_installed_names.add(candidate.canonical_name)
        report_items.append(
            ReportItem(
                candidate_name=candidate.name,
                candidate_version=str(candidate.version),
                requested=True,
                source_url=direct_url.url if direct_url is not None else None,
                source_hashes=None,
                yanked=False,
                is_direct=direct_url is not None,
                requested_extras=(
                    tuple(sorted(editable_requirement.req.extras))
                    if editable_requirement.req is not None
                    else ()
                ),
                requires_dist=tuple(
                    str(dependency) for dependency in editable_dependencies
                ),
                editable=True,
            )
        )
    if not installed and bundle.requirements:
        for requirement in bundle.requirements:
            item = install_req_from_line(requirement)
            requirement_name = item.req.name if item.req is not None else requirement
            installed_dist = find_installed(requirement_name)
            if (
                installed_dist is not None
                and requirement not in reported_satisfied
                and not quiet
            ):
                print(
                    f"Requirement already satisfied: {requirement}",
                    file=sys.stdout,
                )
        return 0
    if options.report:
        write_install_report(Path(options.report), report_items)
    if (
        installed
        and not options.dry_run
        and not options.no_deps
        and not options.no_warn_conflicts
    ):
        _warn_about_install_conflicts(newly_installed_names)
    if installed and options.dry_run and not quiet:
        print(f"Would install {' '.join(installed)}")
    elif installed and not quiet:
        locked_order = {name: index for index, name in enumerate(bundle.locked_links)}
        installed = [
            value
            for _, value in sorted(
                zip(installed_canonical_names, installed, strict=True),
                key=lambda item: (
                    0 if item[0] in locked_order else item[0] not in summary_root_names,
                    locked_order.get(item[0], len(locked_order)),
                ),
            )
        ]
        print(f"Successfully installed {' '.join(installed)}")
    return 0


def _warn_about_install_conflicts(changed_names: set[str]) -> None:
    """Warn about broken requirements affected by the current install."""
    distributions = InstalledDistributionStore().iter(skip={"pip"})
    package_set = {
        dist.canonical_name: PackageDetails.from_dependencies(
            Version(dist.raw_version),
            parse_installed_dependencies(dist),
        )
        for dist in distributions
    }
    affected = set(changed_names)
    changed = True
    while changed:
        changed = False
        for dist in distributions:
            if dist.canonical_name in affected:
                continue
            if any(
                canonicalize_name(requirement.name) in affected
                for requirement in parse_installed_dependencies(dist)
            ):
                affected.add(dist.canonical_name)
                changed = True
    missing, conflicting = check_package_set(package_set)
    for name, requirements in sorted(missing.items()):
        if name not in affected:
            continue
        distribution = next(
            dist for dist in distributions if dist.canonical_name == name
        )
        for _, requirement in requirements:
            print(
                f"{distribution.canonical_name} {distribution.version} requires "
                f"{requirement.name}, which is not installed.",
                file=sys.stderr,
            )
    for name, requirements in sorted(conflicting.items()):
        if name not in affected:
            continue
        distribution = next(
            dist for dist in distributions if dist.canonical_name == name
        )
        for dependency_name, version, requirement in requirements:
            print(
                f"{distribution.canonical_name} {distribution.version} requires "
                f"{requirement}, but you have {dependency_name} {version} which is incompatible.",
                file=sys.stderr,
            )


def create_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="pip install", allow_abbrev=False)
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--group", dest="groups", action="append", default=[])
    parser.add_argument(
        "--requirements-from-script",
        dest="requirements_from_scripts",
        action="append",
        default=[],
    )
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
    parser.add_argument("--cert")
    parser.add_argument("--client-cert")
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument(
        "--keyring-provider",
        choices=("auto", "disabled", "import", "subprocess"),
        default="auto",
    )
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--use-pep517", action="store_true")
    parser.add_argument("--use-deprecated", action="append", default=[])
    parser.add_argument(
        "--use-feature", dest="use_features", action="append", default=[]
    )
    parser.add_argument("--disable-pip-version-check", action="store_true")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("-U", "--upgrade", action="store_true")
    parser.add_argument(
        "--upgrade-strategy",
        choices=("only-if-needed", "eager"),
        default="only-if-needed",
    )
    parser.add_argument("-I", "--ignore-installed", action="store_true")
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--no-user", action="store_true")
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--prefix")
    parser.add_argument("-t", "--target")
    parser.add_argument("--cache-dir")
    parser.add_argument("--no-cache-dir", action="store_true")
    parser.add_argument("--no-binary", action="append", default=[])
    parser.add_argument("--only-binary", action="append", default=[])
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--implementation")
    parser.add_argument("--python-version")
    parser.add_argument("--abi", action="append", default=[])
    parser.add_argument("--pre", action="store_true")
    parser.add_argument("--all-releases", action="append", default=[])
    parser.add_argument("--only-final", action="append", default=[])
    parser.add_argument("--ignore-requires-python", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--uploaded-prior-to")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="count", default=0)
    parser.add_argument("--no-warn-script-location", action="store_true")
    parser.add_argument("--no-warn-conflicts", action="store_true")
    parser.add_argument(
        "--config-settings",
        "--config-setting",
        dest="config_settings",
        action="append",
        default=[],
    )
    parser.add_argument("--require-hashes", action="store_true")
    return parser
