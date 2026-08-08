"""Pure setup transformations used by the install command."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from cpip.cli.requirements import collect_requirements, load_source_config
from cpip.core.appdirs import user_cache_dir
from cpip.core.format_control import FormatControl
from cpip.core.python import CURRENT_PYTHON_VERSION_DIGITS, CURRENT_PYTHON_VERSION_FULL
from cpip.core.wheel import TargetContext


@dataclass(frozen=True)
class InstallRuntimeSetup:
    config: Any
    explicit_index_url: bool
    cache_dir: str | None
    quiet: bool


@dataclass
class InstallExecutionContext:
    options: Any
    bundle: Any
    target: TargetContext
    requirements: list[Any]
    cache_dir: str | None
    quiet: bool
    python_version: str


@dataclass(frozen=True)
class InstallRequirementState:
    requested_order: dict[str, int]
    requested_source_urls: set[str]
    summary_root_source_urls: set[str]
    build_options: dict[str, dict[str, object]]
    source_requirements_by_name: dict[str, Any]
    source_requirements_by_url: dict[str, Any]
    requested_extras_by_name: dict[str, set[str]]


def requirement_state(requirements: list[Any], bundle: Any) -> InstallRequirementState:
    from cpip.core.packaging import canonicalize_name
    from cpip.resolution.input_requirements import install_req_from_line

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

    source_requirements_by_name: dict[str, Any] = {}
    requested_extras_by_name: dict[str, set[str]] = {}
    source_requirements_by_url: dict[str, Any] = {}
    for requirement in requirements:
        if requirement.req is None:
            continue
        name = canonicalize_name(requirement.req.name)
        source_requirements_by_name[name] = requirement
        requested_extras_by_name.setdefault(name, set()).update(requirement.req.extras)
        if requirement.link is not None:
            source_requirements_by_url[requirement.link.url] = requirement
        if requirement.req.url is not None:
            source_requirements_by_url[requirement.req.url] = requirement

    for constraint in bundle.constraints:
        constraint_requirement = install_req_from_line(constraint)
        if constraint_requirement.req is None:
            continue
        name = canonicalize_name(constraint_requirement.req.name)
        source_requirements_by_name[name] = constraint_requirement
        if constraint_requirement.link is not None:
            source_requirements_by_url[constraint_requirement.link.url] = constraint_requirement
        if constraint_requirement.req.url is not None:
            source_requirements_by_url[constraint_requirement.req.url] = constraint_requirement

    return InstallRequirementState(
        requested_order=requested_order,
        requested_source_urls=requested_source_urls,
        summary_root_source_urls=summary_root_source_urls,
        build_options=build_options,
        source_requirements_by_name=source_requirements_by_name,
        source_requirements_by_url=source_requirements_by_url,
        requested_extras_by_name=requested_extras_by_name,
    )


def runtime_setup(args: list[str], options: object, index_url_options: frozenset[str]) -> InstallRuntimeSetup:
    quiet = options.quiet > 0
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)
        os.environ["CPIP_QUIET"] = "1"
    else:
        os.environ.pop("CPIP_QUIET", None)

    cache_dir = (
        None
        if options.no_cache_dir
        else options.cache_dir or os.environ.get("CPIP_CACHE_DIR") or user_cache_dir("cpip")
    )
    return InstallRuntimeSetup(
        config=load_source_config("install"),
        explicit_index_url=any(arg in index_url_options for arg in args),
        cache_dir=cache_dir,
        quiet=quiet,
    )


def config_settings(values: list[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in values:
        key, separator, payload = value.partition("=")
        result[key] = payload if separator else ""
    return result


def group_items(values: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values:
        filename, separator, group = value.partition(":")
        if separator:
            result.append((filename, group))
        else:
            result.append(("pyproject.toml", filename))
    return result


def format_control_from_args(args: list[str]) -> FormatControl:
    control = FormatControl()
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--no-binary", "--only-binary"):
            if index + 1 < len(args):
                control.apply(token[2:], args[index + 1])
            index += 2
            continue
        if token.startswith(("--no-binary=", "--only-binary=")):
            option, _, value = token.partition("=")
            control.apply(option[2:], value)
        index += 1
    return control


def release_control_args(args: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--all-releases", "--only-final"):
            if index + 1 >= len(args):
                raise ValueError(f"{token} requires a value")
            result.append((token[2:], args[index + 1]))
            index += 2
            continue
        if token.startswith(("--all-releases=", "--only-final=")):
            option, _, value = token.partition("=")
            result.append((option[2:], value))
        elif token == "--pre":
            result.append(("pre", ":all:"))
        index += 1
    return result


def requirement_bundle(
    options: object,
    *,
    config: object,
    explicit_index_url: bool,
    grouped_requirements: list[str],
    script_requirements: list[str],
    format_control: FormatControl,
    release_control: list[tuple[str, str]],
    config_settings: dict[str, object],
    cache_dir: str | None,
):
    """Build the normalized requirement bundle consumed by resolution."""
    return collect_requirements(
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
        extra_index_urls=[*config.extra_index_urls, *options.extra_index_url],
        no_index=options.no_index or config.no_index,
        format_control=format_control,
        release_control_args=release_control,
        require_hashes=options.require_hashes,
        cert=options.cert,
        client_cert=options.client_cert,
        no_input=options.no_input,
        keyring_provider=options.keyring_provider,
        proxy=options.proxy,
        cache_dir=cache_dir,
    )


def validate_option_combinations(options: object) -> None:
    """Validate install options that are independent of resolution state."""
    if options.user and options.target:
        raise ValueError("Can not combine '--user' and '--target'")
    if options.user and options.prefix:
        raise ValueError("Can not combine '--user' and '--prefix'")
    if (
        not options.target
        and not options.dry_run
        and (options.platform or options.python_version or options.abi)
    ):
        raise ValueError(
            "Can not use any platform or abi specific options unless installing via '--target'",
        )
    if options.pre and (options.all_releases or options.only_final):
        raise ValueError("--pre cannot be used with --all-releases or --only-final")


def python_version(options: object) -> str:
    if not options.python_version:
        return CURRENT_PYTHON_VERSION_FULL
    value = str(options.python_version)
    if "." in value:
        parts = value.split(".")
        return f"{parts[0]}.{parts[1]}.0" if len(parts) == 2 else value
    if len(value) == 1:
        return f"{value}.0.0"
    if len(value) == 2:
        return f"{value[0]}.{value[1]}.0"
    return value


def target_context(options: object) -> TargetContext:
    return TargetContext(
        platforms=tuple(options.platform),
        implementation=options.implementation,
        python_version=(
            str(options.python_version)
            if options.python_version
            else CURRENT_PYTHON_VERSION_DIGITS
        ),
        abis=tuple(options.abi),
    )


def resolution_error_message(
    message: str,
    requirements: list[Any],
    release_control: list[tuple[str, str]],
) -> str:
    """Translate resolver incompatibility reports to the CLI diagnostic contract."""
    if message.startswith("No matching distribution found for "):
        return message.splitlines()[0]
    if message.startswith("because no versions of "):
        unavailable = re.match(
            r"because no versions of ([A-Za-z0-9_.-]+) ([^\s]+) are available",
            message,
        )
        root_names = {
            getattr(requirement.req, "name", "").lower()
            for requirement in requirements
            if requirement.req is not None
        }
        if unavailable is not None and unavailable.group(1).lower() not in root_names:
            return (
                f"No matching distribution found for "
                f"{unavailable.group(1)}=={unavailable.group(2)}"
            )
        return message.splitlines()[0]

    root = next(
        (
            requirement.req.raw
            for requirement in requirements
            if requirement.req is not None
        ),
        None,
    )
    if any(name == "only-final" for name, _ in release_control) and root is not None:
        return f"Could not find a final version that satisfies the requirement {root}"

    missing_root = re.search(
        r"because your project depends on ([A-Za-z0-9_.-]+)(?: ([^\s<]+))? <empty>",
        message,
    )
    if missing_root is not None:
        name, version = missing_root.groups()
        return f"No matching distribution found for {name}{version or ''}"

    dependency = re.search(
        r"because (?!your project )[^\n]+ depends on ([A-Za-z0-9_.-]+)(?:(==|!=|<=|>=|~=|<|>)([^\s]+)| ([^\s<]+))?(?: <empty>)?",
        message,
    )
    if dependency is not None:
        name, operator, value, spaced = dependency.groups()
        if operator and value:
            return f"No matching distribution found for {name}{operator}{value}"
        if spaced:
            return f"No matching distribution found for {name} {spaced}"
        return f"Could not find a version that satisfies the requirement {name.replace('-', '_')}"

    if root is None:
        return message
    return f"Could not find a version that satisfies the requirement {root}"
