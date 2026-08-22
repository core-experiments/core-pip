"""Unified helpers for querying, filtering, and validating installed distributions."""

from __future__ import annotations

import json
import string
from collections import namedtuple
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping

from cpip.core.cpip_version import CPIP_DISTRIBUTION_NAMES
from cpip.core.light_metadata import LightDistributionStore, parse_metadata_text
from cpip.core.packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.versions import Version

TYPE_CHECKING = False

if TYPE_CHECKING:
    # typing is imported only by the type checker: the structural type and
    # the alias below are annotations, and typing is ~1 ms of import on
    # every show/check/list/inspect run.
    from typing import Any, NamedTuple, Protocol

    from cpip.core.wheel import WheelTag

    LatestInfo = Mapping[str, tuple[Any, str]]

    class DistributionLike(Protocol):
        """The subset of InstalledMetadataDistribution/LightDistribution this module uses.

        A structural type rather than a shared base class: check/show/inspect/
        freeze read installed metadata through the lightweight, importlib.metadata
        -free LightDistribution, while list's slower path still uses the richer
        InstalledMetadataDistribution. The functions below don't care which one
        they get, only that it has this shape.
        """

        @property
        def canonical_name(self) -> str: ...

        @property
        def raw_name(self) -> str: ...

        @property
        def raw_version(self) -> str: ...

        @property
        def location(self) -> str: ...

        @property
        def metadata(self) -> Any: ...

        @property
        def version(self) -> Version: ...

        @property
        def metadata_version(self) -> str | None: ...

        @property
        def editable(self) -> bool: ...

        @property
        def editable_project_location(self) -> str | None: ...

        @property
        def installer(self) -> str: ...

        def iter_dependencies(
            self, extras: tuple[str, ...] = ()
        ) -> list[Requirement]: ...

        def iter_raw_dependencies(self) -> list[str]: ...

        def read_text(self, path: str) -> str: ...

        def iter_declared_entries(self) -> list[str]: ...


PackageSet = dict[str, "PackageDetails"]


def normalize_project_url_label(label: str) -> str:
    """Normalize a project URL label according to PEP 753."""
    chars_to_remove = string.punctuation + string.whitespace
    return label.translate(str.maketrans("", "", chars_to_remove)).lower()


if TYPE_CHECKING:

    class InstalledPackageInfo(NamedTuple):
        distribution: DistributionLike
        requires: list[str]
        required_by: list[str]
        entry_points: list[str]
        files: list[str] | None
        homepage: str

else:
    InstalledPackageInfo = namedtuple(
        "InstalledPackageInfo",
        [
            "distribution",
            "requires",
            "required_by",
            "entry_points",
            "files",
            "homepage",
        ],
    )


def _dependent_index(
    candidates: Iterable[DistributionLike],
) -> tuple[dict[str, list[str]], bool]:
    """Map each canonical name to the raw names of the distributions needing it.

    Built once per call rather than per queried package: ``cpip show a b c``
    otherwise re-parses every installed distribution's dependencies once per
    argument.  The flag reports unreadable dependency metadata, which is a
    property of the environment and so applies to every query alike.
    """
    dependents: dict[str, list[str]] = {}
    for candidate in candidates:
        try:
            names = {requirement.name for requirement in candidate.iter_dependencies()}
        except ValueError:
            return {}, True
        for name in {canonicalize_name(name) for name in names}:
            existing = dependents.get(name)
            if existing is None:
                dependents[name] = [candidate.raw_name]
            else:
                existing.append(candidate.raw_name)
    return dependents, False


def iter_installed_package_info(
    query: list[str],
    *,
    include_files: bool = False,
) -> Iterator[InstalledPackageInfo]:
    """Collect presentation-neutral information for named distributions."""
    installed = {dist.canonical_name: dist for dist in LightDistributionStore().iter()}
    query_names = [canonicalize_name(name) for name in query]
    dependents, dependents_unavailable = (
        _dependent_index(installed.values())
        if any(name in installed for name in query_names)
        else ({}, False)
    )
    for query_name in query_names:
        dist = installed.get(query_name)
        if dist is None:
            continue

        try:
            requires = sorted(
                {requirement.name for requirement in dist.iter_dependencies()},
                key=str.lower,
            )
        except ValueError:
            requires = sorted(dist.iter_raw_dependencies(), key=str.lower)

        required_by = (
            ["#N/A"] if dependents_unavailable else dependents.get(query_name, [])
        )

        try:
            entry_points = dist.read_text("entry_points.txt").splitlines()
        except FileNotFoundError:
            entry_points = []

        files = sorted(dist.iter_declared_entries()) if include_files else None
        project_urls = dist.metadata.get_all("Project-URL", [])
        homepage = dist.metadata.get("Home-page") or ""
        if not homepage:
            for project_url in project_urls:
                # A third-party wheel can ship a Project-URL with no comma;
                # that is malformed, not a reason for ``cpip show`` to fail.
                label, separator, url = project_url.partition(",")
                if not separator:
                    continue
                if normalize_project_url_label(label) == "homepage":
                    homepage = url.strip()
                    break

        yield InstalledPackageInfo(
            distribution=dist,
            requires=requires,
            required_by=sorted(required_by, key=str.lower),
            entry_points=entry_points,
            files=files,
            homepage=homepage,
        )


def select_installed_distributions(
    *,
    paths: list[str] | None = None,
    local_only: bool = False,
    user_only: bool = False,
    editables_only: bool = False,
    include_editables: bool = True,
    excludes: Iterable[str] = (),
    not_required: bool = False,
    skip: Collection[str] = (),
    user_site: str | None = None,
) -> list[DistributionLike]:
    """Return installed distributions after applying listing filters."""
    from .metadata import InstalledDistributionStore

    excluded = {canonicalize_name(name) for name in excludes}

    if "pip" in excluded:
        excluded.update(canonicalize_name(name) for name in CPIP_DISTRIBUTION_NAMES)

    distributions = list(
        InstalledDistributionStore(paths=paths, user_site=user_site).iter(
            local_only=local_only,
            user_only=user_only,
            editables_only=editables_only,
            include_editables=include_editables,
            skip=skip,
        ),
    )

    if not_required:
        dependency_names = {
            canonicalize_name(requirement.name)
            for dist in distributions
            for requirement in dist.iter_dependencies()
        }

        distributions = [
            dist
            for dist in distributions
            if dist.canonical_name not in dependency_names
        ]

    return [dist for dist in distributions if dist.canonical_name not in excluded]


def format_list_columns(
    distributions: list[DistributionLike],
    *,
    outdated: bool = False,
    verbose: bool = False,
    latest: LatestInfo | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Build rows and headers for the columns list format."""

    header = ["Package", "Version"]

    if outdated:
        header.extend(("Latest", "Type"))

    build_tags = []

    for dist in distributions:
        try:
            wheel_text = dist.read_text("WHEEL")
        except FileNotFoundError:
            build_tags.append(None)
        else:
            # The same first-value read email.parser would give, without
            # importing the email package for one header per WHEEL file.
            build_tags.append(parse_metadata_text(wheel_text).get("Build"))

    if any(build_tags):
        header.append("Build")

    has_editables = any(dist.editable for dist in distributions)

    if has_editables:
        header.append("Editable project location")

    if verbose:
        header.extend(("Location", "Installer"))

    rows = []

    for index, dist in enumerate(distributions):
        row = [dist.raw_name, dist.raw_version]

        if outdated:
            version, filetype = (latest or {})[dist.canonical_name]
            row.extend((str(version), filetype))

        if any(build_tags):
            row.append(build_tags[index] or "")

        if has_editables:
            row.append(dist.editable_project_location or "")

        if verbose:
            row.extend((dist.location, dist.installer))

        rows.append(row)

    return rows, header


def format_list_json(
    distributions: list[DistributionLike],
    *,
    outdated: bool = False,
    verbose: bool = False,
    latest: LatestInfo | None = None,
) -> str:
    """Build JSON for the list format."""
    data = []

    for dist in distributions:
        try:
            version = str(dist.version)
        except ValueError:
            version = dist.raw_version

        info: dict[str, Any] = {"name": dist.raw_name, "version": version}

        if verbose:
            info["location"] = dist.location
            info["installer"] = dist.installer

        if dist.editable_project_location:
            info["editable_project_location"] = dist.editable_project_location

        if outdated:
            latest_version, filetype = (latest or {})[dist.canonical_name]
            info["latest_version"] = str(latest_version)
            info["latest_filetype"] = filetype

        data.append(info)

    return json.dumps(data)


def format_list_freeze(
    distributions: list[DistributionLike],
    *,
    verbose: bool = False,
) -> list[str]:
    """Build lines for the list freeze format."""
    result = []

    for dist in distributions:
        try:
            requirement = f"{dist.raw_name}=={dist.version}"
        except ValueError:
            requirement = f"{dist.raw_name}==={dist.raw_version}"

        if verbose:
            requirement = f"{requirement} ({dist.location})"

        result.append(requirement)

    return result


class PackageDetails:
    """One installed distribution as :func:`check_package_set` sees it.

    ``version`` is ``None`` when the installed metadata carries a version that
    is not PEP 440.  Such a distribution is still installed, so it must stay in
    the set -- dropping it would report every dependent as missing it -- but
    nothing can be compared against it.
    """

    __slots__ = ("dependencies", "requested_extras", "version")

    def __init__(
        self,
        version: Version | None,
        dependencies: tuple[Requirement, ...],
        requested_extras: frozenset[str] = frozenset(),
    ) -> None:
        self.version = version
        self.dependencies = dependencies
        self.requested_extras = requested_extras

    @classmethod
    def from_dependencies(
        cls,
        version: Version | None,
        dependencies: list[Requirement],
        requested_extras: frozenset[str] = frozenset(),
    ) -> PackageDetails:
        return cls(version, tuple(dependencies), requested_extras)


def marker_allows(requirement: Requirement, requested_extras: frozenset[str]) -> bool:
    return marker_applies(requirement.marker, extras=requested_extras)


def check_package_set(
    package_set: PackageSet,
) -> tuple[
    dict[str, list[tuple[str, Requirement]]],
    dict[str, list[tuple[str, str, Requirement]]],
]:
    missing: dict[str, list[tuple[str, Requirement]]] = {}
    conflicting: dict[str, list[tuple[str, str, Requirement]]] = {}
    for name, details in package_set.items():
        for requirement in details.dependencies:
            if not marker_allows(requirement, details.requested_extras):
                continue
            canonical = canonicalize_name(requirement.name)
            dependency = package_set.get(canonical)
            if dependency is None:
                missing_entry = missing.get(name)
                if missing_entry is None:
                    missing[name] = [(canonical, requirement)]
                else:
                    missing_entry.append((canonical, requirement))
                continue
            if dependency.version is None:
                # Installed, but with a version no specifier can be compared
                # against. Reporting a conflict would be a guess.
                continue
            if not requirement.is_satisfied_by(dependency.version):
                conflicting_entry = conflicting.get(name)
                conflict = (canonical, str(dependency.version), requirement)
                if conflicting_entry is None:
                    conflicting[name] = [conflict]
                else:
                    conflicting_entry.append(conflict)
    return missing, conflicting


def parse_installed_dependencies(
    dist: DistributionLike,
) -> list[Requirement]:
    """Parse the active dependency declarations of an installed distribution."""
    result = []
    for value in dist.iter_raw_dependencies():
        requirement = parse_requirement(value)
        if marker_applies(requirement.marker, extras=()):
            result.append(requirement)
    return result


def installed_dependencies_by_name(
    distributions: Iterable[DistributionLike],
) -> dict[str, list[Requirement]]:
    """Map each installed distribution's canonical name to its dependencies."""
    return {
        dist.canonical_name: parse_installed_dependencies(dist)
        for dist in distributions
    }


def package_set_from_dependencies(
    distributions: Iterable[DistributionLike],
    dependencies_by_name: dict[str, list[Requirement]],
) -> PackageSet:
    """Build the :func:`check_package_set` input from an installed environment.

    Callers pass the dependency map separately because the install command
    reuses it to index dependents; ``cpip check`` does not.
    """
    package_set: PackageSet = {}

    for dist in distributions:
        try:
            version = Version(dist.raw_version)
        except ValueError:
            # A legacy or vendor-patched distribution can carry a non-PEP 440
            # version. It is still installed, so keep it in the set.
            version = None

        package_set[dist.canonical_name] = PackageDetails.from_dependencies(
            version,
            dependencies_by_name[dist.canonical_name],
        )

    return package_set


def metadata_errors(
    distributions: Iterable[DistributionLike],
) -> list[str]:
    """Return human-readable errors for malformed dependency metadata."""
    errors = []
    for dist in distributions:
        for value in dist.iter_raw_dependencies():
            if count_unquoted(value, ";") > 1:
                errors.append(f"Error parsing dependencies of {dist.raw_name}")
                break
            try:
                parse_requirement(value)
            except ValueError as exc:
                errors.append(f"Error parsing dependencies of {dist.raw_name}: {exc}")
                break
    return errors


def unsupported_distributions(
    distributions: Iterable[DistributionLike],
    supported_tags: Callable[[], Iterable[WheelTag]],
) -> list[DistributionLike]:
    """Return distributions whose wheel tags are unsupported.

    ``supported_tags`` is called at most once, and only for a distribution
    whose WHEEL carries a tag other than ``py3-none-any``: that tag is in
    every Python 3 interpreter's supported set, so a wheel listing it ranks
    without computing the set (and without importing the tag machinery).
    """
    supported: tuple[WheelTag, ...] | None = None
    result = []
    for dist in distributions:
        try:
            wheel_text = dist.read_text("WHEEL")
        except FileNotFoundError:
            continue
        tags = []
        for line in wheel_text.splitlines():
            if not line.startswith("Tag:"):
                continue
            parts = line.split(":", 1)[1].strip().split("-")
            if len(parts) == 3:
                tags.append(tuple(parts))
        if not tags or ("py3", "none", "any") in tags:
            continue
        from cpip.core.wheel import WheelTag, wheel_tag_rank

        if supported is None:
            supported = tuple(supported_tags())
        wheel_tags = tuple(WheelTag(*parts) for parts in tags)
        if wheel_tag_rank(wheel_tags, supported) is None:
            result.append(dist)
    return result


def count_unquoted(value: str, target: str) -> int:
    count = 0
    quote: str | None = None
    for char in value:
        if char in {"'", '"'}:
            quote = None if quote == char else char
        elif char == target and quote is None:
            count += 1
    return count
