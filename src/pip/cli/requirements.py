"""Shared requirement collection and conversion."""

from __future__ import annotations

import argparse
import os
import sys
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from pip._vendor import tomli as tomllib
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pip.cli.config import ConfigurationStore
from pip.core.errors import ConfigurationError, InstallationError
from pip.core.format_control import FormatControl
from pip.core.packaging import (
    SpecifierSet,
    Version,
    canonicalize_name,
    parse_requirement,
)
from pip.core.release_control import ReleaseControl
from pip.core.wheel import TargetContext
from pip.index.links import Link

NO_INDEX_VALUES = frozenset(("1", "true", "yes", "on"))
RELEASE_OPTIONS = frozenset(("pre", "all-releases"))

if TYPE_CHECKING:
    from pip.resolution.req_install import InstallRequirement


@dataclass(frozen=True)
class RequirementsBundle:
    requirements: list[str]
    constraints: list[str]
    editables: list[str]
    requirement_config_settings: dict[str, dict[str, object]]
    requirement_hashes: dict[str, dict[str, list[str]]]
    constraint_hashes: dict[str, dict[str, list[str]]]
    editable_config_settings: dict[str, dict[str, object]]
    find_links: list[str]
    index_url: str | None
    extra_index_urls: list[str]
    no_index: bool
    format_control: FormatControl
    locked_links: dict[str, str] = field(default_factory=dict)
    locked_direct_names: frozenset[str] = frozenset()
    release_control: ReleaseControl = field(default_factory=ReleaseControl)
    require_hashes: bool = False
    session: Any = None


@dataclass(frozen=True)
class SourceConfig:
    find_links: list[str]
    index_url: str | None
    extra_index_urls: list[str]
    no_index: bool


def load_source_config(command: str | None = None) -> SourceConfig:
    from pip.index.config import DEFAULT_INDEX_URL

    store = ConfigurationStore()
    try:
        store.load()
    except ConfigurationError:
        return SourceConfig([], DEFAULT_INDEX_URL, [], False)

    def configured(option: str) -> str | None:
        if command is not None:
            value = store.get_optional(f"{command}.{option}")
            if value is not None:
                return value
        return store.get_optional(f"global.{option}")

    raw_find_links = configured("find-links")
    find_links = (
        []
        if raw_find_links is None
        else [line.strip() for line in raw_find_links.splitlines() if line.strip()]
    )
    index_url = configured("index-url") or DEFAULT_INDEX_URL
    raw_extra_index_urls = configured("extra-index-url")
    extra_index_urls = (
        []
        if raw_extra_index_urls is None
        else [
            line.strip() for line in raw_extra_index_urls.splitlines() if line.strip()
        ]
    )
    no_index_value = configured("no-index")
    no_index = (
        no_index_value is not None
        and no_index_value.strip().lower() in NO_INDEX_VALUES
    )
    if (value := os.environ.get("PIP_FIND_LINKS")) is not None:
        find_links = value.split()
    if (value := os.environ.get("PIP_INDEX_URL")) is not None:
        index_url = value
    if (value := os.environ.get("PIP_EXTRA_INDEX_URL")) is not None:
        extra_index_urls = value.split()
    if (value := os.environ.get("PIP_NO_INDEX")) is not None:
        no_index = value.strip().lower() in NO_INDEX_VALUES
    return SourceConfig(find_links, index_url, extra_index_urls, no_index)


def requirements_from_script(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallationError(f"Could not read script {path}: {exc}") from exc

    blocks: list[str] = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() != "# /// script":
            index += 1
            continue
        index += 1
        block: list[str] = []
        while index < len(lines) and lines[index].strip() != "# ///":
            line = lines[index]
            block.append(line.removeprefix("# ").removeprefix("#"))
            index += 1
        if index == len(lines):
            raise InstallationError("Failed to parse TOML in script metadata")
        blocks.append("\n".join(block))
        index += 1
    if len(blocks) > 1:
        raise InstallationError("Multiple 'script' blocks found")
    if not blocks:
        raise InstallationError(f"No 'script' block found in {path}")
    try:
        data = tomllib.loads(blocks[0])
    except tomllib.TOMLDecodeError as exc:
        raise InstallationError(
            f"Failed to parse TOML in script metadata: {exc}"
        ) from exc
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise InstallationError(
            "Script metadata 'dependencies' must be a list of strings"
        )
    requires_python = data.get("requires-python")
    if requires_python is not None:
        if not isinstance(requires_python, str):
            raise InstallationError(
                "Script metadata 'requires-python' must be a string"
            )
        current = Version(".".join(str(part) for part in sys.version_info[:3]))
        incompatible = (
            requires_python.startswith("!=")
            and requires_python.endswith(".*")
            and str(current).startswith(requires_python[2:-1])
        )
        if incompatible or not SpecifierSet(requires_python).contains(current):
            raise InstallationError(
                f"Script requires a different Python version: {requires_python}"
            )
    return dependencies


def collect_requirements(
    *,
    requirements: list[str],
    requirement_files: list[str] | None = None,
    constraint_files: list[str] | None = None,
    editables: list[str] | None = None,
    requirement_config_settings: dict[str, dict[str, object]] | None = None,
    editable_config_settings: dict[str, dict[str, object]] | None = None,
    find_links: list[str] | None = None,
    index_url: str | None = None,
    extra_index_urls: list[str] | None = None,
    no_index: bool = False,
    format_control: FormatControl | None = None,
    release_control_args: list[tuple[str, str]] | None = None,
    require_hashes: bool = False,
    cert: str | None = None,
    client_cert: str | None = None,
    no_input: bool = False,
    keyring_provider: str = "auto",
    proxy: str | None = None,
) -> RequirementsBundle:
    from pip.index.provider import CandidateProvider
    from pip.network.http import NetworkSession
    from pip.resolution.req_file import parse_requirements

    if index_url is None:
        from pip.index.config import DEFAULT_INDEX_URL

        index_url = DEFAULT_INDEX_URL

    collected_requirements = list(requirements)
    collected_constraints: list[str] = []
    collected_editables = list(editables or [])
    requirement_settings = dict(requirement_config_settings or {})
    requirement_hashes: dict[str, dict[str, list[str]]] = {}
    constraint_hashes: dict[str, dict[str, list[str]]] = {}
    locked_links: dict[str, str] = {}
    locked_direct_names: set[str] = set()
    editable_settings = dict(editable_config_settings or {})

    def store_hashes(
        target: dict[str, dict[str, list[str]]],
        key: str,
        hashes: dict[str, list[str]],
    ) -> None:
        previous = target.get(key)
        if previous is None:
            target[key] = dict(hashes)
        else:
            target[key] = {
                algorithm: [
                    digest
                    for digest in previous.get(algorithm, [])
                    if digest in hashes.get(algorithm, [])
                ]
                for algorithm in previous.keys() & hashes.keys()
            }

    bundle_find_links = list(find_links or [])
    bundle_index_url = index_url
    bundle_extra_index_urls = list(extra_index_urls or [])
    bundle_no_index = no_index
    bundle_format_control = format_control or FormatControl()

    option_state = argparse.Namespace(require_hashes=require_hashes)
    session = NetworkSession(
        index_urls=[url for url in (bundle_index_url, *bundle_extra_index_urls) if url]
    )
    session.auth.prompting = not no_input
    session.auth.keyring_provider = keyring_provider
    if cert:
        session.verify = cert
    if client_cert:
        session.cert = client_cert
    if proxy is not None:
        session.proxies = {"http": proxy, "https": proxy} if proxy else {}
    provider = CandidateProvider.from_options(
        find_links=bundle_find_links,
        index_url=bundle_index_url,
        extra_index_urls=bundle_extra_index_urls,
        no_index=bundle_no_index,
        format_control=bundle_format_control,
        session=session,
    )
    if provider.release_control is not None:
        for kind, value in release_control_args or []:
            provider.release_control.apply(
                "all_releases" if kind in RELEASE_OPTIONS else "only_final",
                value,
            )

    for filename in requirement_files or []:
        for item in parse_requirements(
            filename,
            session,
            provider=provider,
            options=option_state,
        ):
            if item.locked_link is not None and item.locked_name is not None:
                locked_links[item.locked_name] = item.locked_link
                if item.locked_hashes:
                    requirement_hashes[item.requirement] = dict(item.locked_hashes)
                if item.locked_direct:
                    locked_direct_names.add(item.locked_name)
            if item.is_editable:
                collected_editables.append(item.requirement)
                if item.options and "config_settings" in item.options:
                    editable_settings[item.requirement] = dict(
                        cast(dict[str, object], item.options["config_settings"])
                    )
            elif item.constraint:
                validate_constraint_requirement(
                    item.requirement,
                    editable=item.is_editable,
                )
                collected_constraints.append(item.requirement)
                if item.options and "hashes" in item.options:
                    store_hashes(
                        constraint_hashes,
                        item.requirement,
                        cast(dict[str, list[str]], item.options["hashes"]),
                    )
            else:
                collected_requirements.append(item.requirement)
                if item.options and "config_settings" in item.options:
                    requirement_settings[item.requirement] = dict(
                        cast(dict[str, object], item.options["config_settings"])
                    )
                if item.options and "hashes" in item.options:
                    store_hashes(
                        requirement_hashes,
                        item.requirement,
                        cast(dict[str, list[str]], item.options["hashes"]),
                    )

    for filename in constraint_files or []:
        for item in parse_requirements(
            filename,
            session,
            provider=provider,
            options=option_state,
            constraint=True,
        ):
            validate_constraint_requirement(
                item.requirement,
                editable=item.is_editable,
            )
            collected_constraints.append(item.requirement)
            if item.options and "hashes" in item.options:
                store_hashes(
                    constraint_hashes,
                    item.requirement,
                    cast(dict[str, list[str]], item.options["hashes"]),
                )

    provider.locked_links = {name: Link(url) for name, url in locked_links.items()}

    return RequirementsBundle(
        requirements=collected_requirements,
        constraints=collected_constraints,
        editables=collected_editables,
        requirement_config_settings=requirement_settings,
        requirement_hashes=requirement_hashes,
        constraint_hashes=constraint_hashes,
        editable_config_settings=editable_settings,
        find_links=list(provider.find_links),
        index_url=provider.index_urls[0] if provider.index_urls else None,
        extra_index_urls=provider.index_urls[1:]
        if len(provider.index_urls) > 1
        else [],
        no_index=provider.no_index,
        format_control=provider.format_control or FormatControl(),
        locked_links=locked_links,
        locked_direct_names=frozenset(locked_direct_names),
        release_control=provider.release_control or ReleaseControl(),
        require_hashes=(
            bool(getattr(option_state, "require_hashes", False))
            or bool(requirement_hashes)
            or bool(constraint_hashes)
        ),
        session=session,
    )


def validate_constraint_requirement(
    requirement: str, *, editable: bool = False
) -> None:
    from pip.resolution.req_install import install_req_from_line

    text = requirement.strip()
    if editable:
        raise InstallationError("Editable requirements are not allowed as constraints")
    item = install_req_from_line(requirement, constraint=True)
    if item.req is None:
        raise InstallationError("Unnamed requirements are not allowed as constraints")
    if (
        item.req.url is not None
        and "@" not in requirement
        and "#egg=" not in requirement
    ):
        raise InstallationError("Unnamed requirements are not allowed as constraints")
    if item.req.extras:
        raise InstallationError("Constraints cannot have extras")
    if (
        "@" not in text
        and not any(char.isspace() for char in text)
        and (
            text.startswith((".", "/", "~"))
            or text.endswith(
                (".zip", ".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tgz")
            )
        )
    ):
        raise InstallationError("Unnamed requirements are not allowed as constraints")


def bundle_install_requirements(
    bundle: RequirementsBundle,
    *,
    target: TargetContext | None = None,
) -> list[InstallRequirement]:
    from pip.build.build_backend import prepare_project_metadata
    from pip.resolution.req_install import install_req_from_line
    from pip.core.wheel import parse_wheel_file, supported_wheel_tags, wheel_tag_rank

    requirements: list[InstallRequirement] = []
    direct_sources: dict[str, tuple[str, str]] = {}
    for requirement in bundle.requirements:
        item = install_req_from_line(requirement)
        raw_path = requirement.split("[", 1)[0]
        if item.req is not None and Path(raw_path).is_dir():
            source_path = Path(raw_path).resolve()
            try:
                metadata = prepare_project_metadata(source_path, build_isolation=False)
                source_name = canonicalize_name(metadata.name)
                source_version = str(metadata.version)
            except Exception:
                source_name = item.req.canonical_name
                source_version = "unknown"
            previous = direct_sources.get(source_name)
            if previous is not None and Path(previous[0]).resolve() != source_path:
                print(f"The user requested {source_name} {previous[1]}")
                print(f"The user requested {source_name} {source_version}")
                raise InstallationError(
                    f"Cannot install {source_name} because these package versions "
                    "have conflicting dependencies."
                )
            direct_sources[source_name] = (str(source_path), source_version)
        direct_constraints = (
            [
                constraint
                for constraint in (
                    parse_requirement(raw_constraint)
                    for raw_constraint in bundle.constraints
                )
                if constraint.canonical_name == item.req.canonical_name
                and constraint.url is not None
            ]
            if item.req is not None
            else []
        )
        if direct_constraints:
            constrained = direct_constraints[-1]
            if item.req is not None and (
                item.req.url is None or item.req.url == constrained.url
            ):
                merged_specifier = ",".join(
                    part
                    for part in (str(item.req.specifier), str(constrained.specifier))
                    if part
                )
                constrained = replace(
                    constrained,
                    specifier=SpecifierSet(merged_specifier),
                    extras=constrained.extras | item.req.extras,
                )
                constrained = replace(
                    constrained,
                    raw=item.req.raw,
                )
                item.req = constrained
                item.link = Link(item.req.url or "")
            wheel = parse_wheel_file(
                Path(urllib.parse.urlparse(constrained.url or "").path)
            )
            if (
                wheel is not None
                and item.req is not None
                and not item.req.specifier.contains(wheel.version)
            ):
                raise InstallationError(
                    f"Cannot install {item.req.name} because these package versions "
                    "have conflicting dependencies. "
                    f"The URL constraint selects incompatible version {wheel.version}."
                )
        if item.link is not None and item.link.is_file and item.link.is_wheel:
            wheel = parse_wheel_file(item.link.file_path)
            if (
                wheel is not None
                and wheel_tag_rank(wheel.tags, supported_wheel_tags(target)) is None
            ):
                if direct_constraints:
                    assert item.req is not None
                    raise InstallationError(
                        f"Cannot install {item.req.name} because these package "
                        "versions have conflicting dependencies."
                    )
                raise InstallationError(
                    f"{item.link.filename} is not a supported wheel on this platform"
                )
        if not item.match_markers():
            if item.req is not None and item.markers:
                print(
                    f"Ignoring {item.req.name}: markers '{item.markers}' don't match "
                    "your environment"
                )
            continue
        if requirement in bundle.requirement_config_settings:
            item.config_settings = bundle.requirement_config_settings[requirement]
        if requirement in bundle.requirement_hashes:
            item.hash_options = {
                name: list(digests)
                for name, digests in bundle.requirement_hashes[requirement].items()
            }
        if item.req is not None and not item.hash_options:
            for raw, hashes in bundle.constraint_hashes.items():
                if parse_requirement(raw).canonical_name == item.req.canonical_name:
                    if any(hashes.values()):
                        item.hash_options = {
                            name: list(digests) for name, digests in hashes.items()
                        }
                    break
        if item.req is not None and item.req.canonical_name in bundle.locked_links:
            item.link = Link(bundle.locked_links[item.req.canonical_name])
            item.local_file_path = item.link.file_path if item.link.is_file else None
        if item.req is not None and item.local_file_path is not None:
            source_path = Path(item.local_file_path)
            previous = direct_sources.get(item.req.canonical_name)
            if (
                previous is not None
                and Path(previous[0]).resolve() != source_path.resolve()
            ):
                raise InstallationError(
                    f"Cannot install {item.req.name} because these package versions "
                    "have conflicting dependencies."
                )
            direct_sources[item.req.canonical_name] = (str(source_path), "")
        requirements.append(item)
    return requirements
