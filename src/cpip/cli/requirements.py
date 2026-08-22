"""Shared requirement collection and conversion."""

from __future__ import annotations

import argparse
import os
import sys
import threading


from cpip.cli.dependency_groups import toml_module
from cpip.core.errors import InstallationError
from cpip.core.format_control import FormatControl
from cpip.core.packaging import SpecifierSet, canonicalize_name, parse_requirement
from cpip.core.versions import Version
from cpip.core.release_control import ReleaseControl
from cpip.core.wheel import parse_wheel_file, supported_wheel_tags, wheel_tag_rank
from cpip.index.config import DEFAULT_INDEX_URL
from cpip.index.links import Link
from cpip.index.source_locations import resolve_source_location
from cpip.core.appdirs import http_cache_path
from cpip.resolution.input_requirements import install_req_from_line

RELEASE_OPTIONS = frozenset(("pre", "all-releases"))


TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from cpip.core.wheel import TargetContext
    from cpip.resolution.req_install import InstallRequirement


class RequirementsBundle:
    __slots__ = tuple(
        [
            "requirements",
            "constraints",
            "editables",
            "requirement_config_settings",
            "requirement_hashes",
            "constraint_hashes",
            "editable_config_settings",
            "find_links",
            "index_url",
            "extra_index_urls",
            "no_index",
            "format_control",
            "locked_links",
            "locked_direct_names",
            "release_control",
            "require_hashes",
            "session",
        ],
    )

    def __init__(
        self,
        requirements: list[str],
        constraints: list[str],
        editables: list[str],
        requirement_config_settings: dict[str, dict[str, object]],
        requirement_hashes: dict[str, dict[str, list[str]]],
        constraint_hashes: dict[str, dict[str, list[str]]],
        editable_config_settings: dict[str, dict[str, object]],
        find_links: list[str],
        index_url: str | None,
        extra_index_urls: list[str],
        no_index: bool,
        format_control: FormatControl,
        locked_links: dict[str, str] | None = None,
        locked_direct_names: frozenset[str] = frozenset(),
        release_control: ReleaseControl | None = None,
        require_hashes: bool = False,
        session: Any = None,
    ) -> None:
        self.requirements = requirements

        self.constraints = constraints

        self.editables = editables

        self.requirement_config_settings = requirement_config_settings

        self.requirement_hashes = requirement_hashes

        self.constraint_hashes = constraint_hashes

        self.editable_config_settings = editable_config_settings

        self.find_links = find_links

        self.index_url = index_url

        self.extra_index_urls = extra_index_urls

        self.no_index = no_index

        self.format_control = format_control

        self.locked_links = locked_links if locked_links is not None else {}

        self.locked_direct_names = locked_direct_names

        self.release_control = (
            release_control if release_control is not None else ReleaseControl()
        )

        self.require_hashes = require_hashes

        self.session = session


class DeferredNetworkSession:
    """Delay transport policy and cache setup until a session attribute is used."""

    __slots__ = (
        "cache_dir",
        "cert",
        "client_cert",
        "index_urls",
        "keyring_provider",
        "lock",
        "no_input",
        "proxy",
        "session",
    )

    def __init__(
        self,
        *,
        index_urls: list[str],
        cache_dir: str | None,
        cert: str | None,
        client_cert: str | None,
        no_input: bool,
        keyring_provider: str,
        proxy: str | None,
    ) -> None:
        self.index_urls = index_urls

        self.cache_dir = cache_dir

        self.cert = cert

        self.client_cert = client_cert

        self.no_input = no_input

        self.keyring_provider = keyring_provider

        self.proxy = proxy

        self.session: Any = None

        self.lock = threading.Lock()

    def materialize(self) -> Any:
        if self.session is not None:
            return self.session

        with self.lock:
            if self.session is not None:
                return self.session

            # Deferred: network.http (and the auth, cache and platform
            # modules under it) only when a session is actually built --
            # never on a warm or local resolve that makes no request.
            from cpip.network.http import NetworkSession

            session = NetworkSession(
                index_urls=self.index_urls,
                cache=(http_cache_path(self.cache_dir) if self.cache_dir else None),
            )

            session.auth.prompting = not self.no_input

            session.auth.keyring_provider = self.keyring_provider

            if self.cert:
                session.verify = self.cert

            if self.client_cert:
                session.cert = self.client_cert

            if self.proxy is not None:
                session.proxies = (
                    {"http": self.proxy, "https": self.proxy} if self.proxy else {}
                )

            self.session = session

            return session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.materialize(), name)

    @property
    def auth(self) -> Any:
        return self.materialize().auth

    @property
    def trusted_hosts(self) -> Any:
        return self.materialize().trusted_hosts

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self.materialize().get(*args, **kwargs)


class RequirementSourceState:
    """Requirement-file source options without constructing an index provider."""

    __slots__ = (
        "find_links",
        "format_control",
        "index_urls",
        "locked_links",
        "no_index",
        "release_control",
    )

    def __init__(
        self,
        *,
        find_links: list[str],
        index_urls: list[str],
        no_index: bool,
        format_control: FormatControl,
    ) -> None:
        self.find_links = find_links

        self.index_urls = index_urls

        self.no_index = no_index

        self.format_control = format_control

        self.release_control = ReleaseControl()

        self.locked_links: dict[str, Any] = {}


PROXY_ENVIRONMENT_NAMES = (
    "CPIP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


def apply_proxy_environment(proxy: str | None) -> None:
    """Point urllib-based helpers and subprocesses at the resolver's proxy.

    Also ensures ``--proxy`` overrides inherited HTTP(S)_PROXY values on every
    platform.
    """

    if not proxy:
        return

    for name in PROXY_ENVIRONMENT_NAMES:
        os.environ[name] = proxy


def config_settings(values: list[str]) -> dict[str, object]:
    """Parse ``--config-settings KEY=VALUE`` pairs; a bare ``KEY`` means ``""``."""

    result: dict[str, object] = {}
    for value in values:
        key, separator, payload = value.partition("=")
        result[key] = payload if separator else ""
    return result


def build_options_from_requirements(
    requirements: list[InstallRequirement],
) -> dict[str, dict[str, object]]:
    """Map each requirement's raw text, URL, and link URL to its config settings."""

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
    return build_options


def requirements_from_script(path: str) -> list[str]:
    tomllib = toml_module()

    try:
        with open(path, encoding="utf-8") as file:
            source = file.read()

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
            f"Failed to parse TOML in script metadata: {exc}",
        ) from exc

    dependencies = data.get("dependencies", [])

    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise InstallationError(
            "Script metadata 'dependencies' must be a list of strings",
        )

    requires_python = data.get("requires-python")

    if requires_python is not None:
        if not isinstance(requires_python, str):
            raise InstallationError(
                "Script metadata 'requires-python' must be a string",
            )

        current = Version(".".join(str(part) for part in sys.version_info[:3]))

        incompatible = (
            requires_python.startswith("!=")
            and requires_python.endswith(".*")
            and str(current).startswith(requires_python[2:-1])
        )

        if incompatible or not SpecifierSet(requires_python).contains(current):
            raise InstallationError(
                f"Script requires a different Python version: {requires_python}",
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
    cache_dir: str | None = None,
) -> RequirementsBundle:
    if index_url is None:
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

    local_only = (
        bundle_no_index
        and not requirement_files
        and not constraint_files
        and bool(bundle_find_links)
        and all(
            resolve_source_location(value)[1] is not None for value in bundle_find_links
        )
    )

    if local_only:
        session = None

    else:
        session = DeferredNetworkSession(
            index_urls=[
                url for url in (bundle_index_url, *bundle_extra_index_urls) if url
            ],
            cache_dir=cache_dir,
            cert=cert,
            client_cert=client_cert,
            no_input=no_input,
            keyring_provider=keyring_provider,
            proxy=proxy,
        )

    provider = RequirementSourceState(
        find_links=bundle_find_links,
        index_urls=(
            [url for url in (bundle_index_url, *bundle_extra_index_urls) if url]
            if not bundle_no_index
            else []
        ),
        no_index=bundle_no_index,
        format_control=bundle_format_control,
    )

    for kind, value in release_control_args or []:
        provider.release_control.apply(
            "all_releases" if kind in RELEASE_OPTIONS else "only_final",
            value,
        )

    if requirement_files or constraint_files:
        assert session is not None

    for filename in requirement_files or []:
        assert session is not None

        # Deferred: the requirements-file parser only when a file is given.
        from cpip.resolution.files import parse_requirements

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
                        item.options["config_settings"],
                    )  # ty:ignore[no-matching-overload]

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
                        item.options["hashes"],  # ty:ignore[invalid-argument-type]
                    )

            else:
                collected_requirements.append(item.requirement)

                if item.options and "config_settings" in item.options:
                    requirement_settings[item.requirement] = dict(
                        item.options["config_settings"],
                    )  # ty:ignore[no-matching-overload]

                if item.options and "hashes" in item.options:
                    store_hashes(
                        requirement_hashes,
                        item.requirement,
                        item.options["hashes"],  # ty:ignore[invalid-argument-type]
                    )

    for filename in constraint_files or []:
        assert session is not None

        # Deferred: the requirements-file parser only when a file is given.
        from cpip.resolution.files import parse_requirements

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
                    item.options["hashes"],  # ty:ignore[invalid-argument-type]
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
    requirement: str,
    *,
    editable: bool = False,
) -> None:
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
                (".zip", ".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tgz"),
            )
        )
    ):
        raise InstallationError("Unnamed requirements are not allowed as constraints")


def bundle_install_requirements(
    bundle: RequirementsBundle,
    *,
    target: TargetContext | None = None,
) -> list[InstallRequirement]:
    requirements: list[InstallRequirement] = []

    direct_sources: dict[str, tuple[str, str]] = {}

    for requirement in bundle.requirements:
        item = install_req_from_line(requirement)

        raw_path = requirement.split("[", 1)[0]

        if item.req is not None and os.path.isdir(raw_path):
            source_path = os.path.realpath(raw_path)

            try:
                # Deferred: only a source requirement builds metadata.
                from cpip.build.build_backend import prepare_project_metadata

                metadata = prepare_project_metadata(
                    source_path,
                    build_isolation=False,
                )

                source_name = canonicalize_name(metadata.name)

                source_version = str(metadata.version)

            except Exception:
                source_name = item.req.canonical_name

                source_version = "unknown"

            previous = direct_sources.get(source_name)

            if previous is not None and os.path.realpath(previous[0]) != source_path:
                print(f"The user requested {source_name} {previous[1]}")

                print(f"The user requested {source_name} {source_version}")

                raise InstallationError(
                    f"Cannot install {source_name} because these package versions "
                    "have conflicting dependencies.",
                )

            direct_sources[source_name] = (source_path, source_version)

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
            import urllib.parse

            constrained = direct_constraints[-1]

            if item.req is not None and (
                item.req.url is None or item.req.url == constrained.url
            ):
                merged_specifier = ",".join(
                    part
                    for part in (str(item.req.specifier), str(constrained.specifier))
                    if part
                )

                constrained = constrained.copy_with(
                    specifier=SpecifierSet(merged_specifier),
                    extras=constrained.extras | item.req.extras,
                )

                constrained = constrained.copy_with(
                    raw=item.req.raw,
                )

                item.req = constrained

                item.link = Link(item.req.url or "")

            wheel = parse_wheel_file(
                urllib.parse.urlparse(constrained.url or "").path,
            )

            if (
                wheel is not None
                and item.req is not None
                and not item.req.specifier.contains(wheel.version)
            ):
                raise InstallationError(
                    f"Cannot install {item.req.name} because these package versions "
                    "have conflicting dependencies. "
                    f"The URL constraint selects incompatible version {wheel.version}.",
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
                        "versions have conflicting dependencies.",
                    )

                raise InstallationError(
                    f"{item.link.filename} is not a supported wheel on this platform",
                )

        if not item.match_markers():
            if item.req is not None and item.markers:
                print(
                    f"Ignoring {item.req.name}: markers '{item.markers}' don't match "
                    "your environment",
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
            source_path = os.path.realpath(item.local_file_path)

            from cpip.build.build_backend import prepare_project_metadata

            try:
                source_version = str(
                    prepare_project_metadata(
                        source_path,
                        build_isolation=False,
                    ).version,
                )
            except Exception:
                source_version = ""

            previous = direct_sources.get(item.req.canonical_name)

            if previous is not None and os.path.realpath(previous[0]) != source_path:
                raise InstallationError(
                    f"Cannot install {item.req.name} because these package versions "
                    "have conflicting dependencies.",
                )

            direct_sources[item.req.canonical_name] = (source_path, source_version)

        requirements.append(item)

    return requirements
