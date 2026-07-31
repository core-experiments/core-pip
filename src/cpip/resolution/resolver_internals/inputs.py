"""Resolver input coercion and requirement-set materialization."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from cpip.core.hashes import file_hashes
from cpip.core.packaging import Requirement, SpecifierSet, parse_requirement
from cpip.core.urls import url_to_path
from cpip.index.links import Link
from cpip.resolution.req_install import InstallRequirement
from cpip.resolution.requirement_set import RequirementSet


def as_requirement_strings(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[str] | None:
    if isinstance(requirements_input, list) and (
        not requirements_input
        or all(isinstance(requirement, str) for requirement in requirements_input)
    ):
        return cast(list[str], requirements_input)
    return None


def as_install_requirements(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[InstallRequirement]:
    from cpip.resolution.requirement_set import RequirementSet

    if isinstance(requirements_input, RequirementSet):
        return list(requirements_input.all_requirements)
    string_requirements = as_requirement_strings(requirements_input)
    if string_requirements is not None:
        return []
    return cast(list, list(requirements_input))


def resolve_requirement_set(
    resolver,
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> RequirementSet:
    from cpip.core.direct_url import ArchiveInfo, DirInfo
    from cpip.resolution.req_install import (
        DownloadInfo,
        InstallRequirement,
        VcsInfo,
    )
    from cpip.resolution.requirement_set import RequirementSet

    plan = resolver.resolve(requirements_input)
    source_requirements, source_requirements_by_url = resolver.source_requirement_map(
        requirements_input
    )
    result = RequirementSet()
    for candidate in plan.candidates:
        source_req = source_requirements.get(
            candidate.canonical_name
        ) or source_requirements_by_url.get(candidate.source_url or "")
        requirement = InstallRequirement(
            req=Requirement(
                name=candidate.name,
                specifier=SpecifierSet(f"=={candidate.version}"),
                extras=frozenset(),
                url=None,
                marker=None,
                raw=f"{candidate.name}=={candidate.version}",
            ),
            link=Link(candidate.path.resolve().as_uri()),
        )
        if candidate.source_url is None:
            requirement.download_info = None
        elif candidate.source_vcs is not None:
            requirement.download_info = DownloadInfo(
                url=candidate.source_url.partition("+")[2],
                vcs_info=VcsInfo(vcs=candidate.source_vcs),
            )
        elif candidate.source_kind == "source-tree":
            requirement.download_info = DownloadInfo(
                url=candidate.source_url,
                dir_info=DirInfo(
                    editable=bool(source_req.editable) if source_req else False
                ),
            )
        else:
            hashes = dict(candidate.source_hashes or {})
            if (
                not hashes
                and not candidate.from_cache
                and candidate.source_url.startswith("file://")
            ):
                try:
                    hashes = file_hashes(url_to_path(candidate.source_url))
                except OSError:
                    hashes = {}
            requirement.download_info = DownloadInfo(
                url=candidate.source_url,
                archive_info=ArchiveInfo(hashes=hashes),
            )
        requirement.editable = (
            bool(source_req.editable) if source_req is not None else False
        )
        requirement.is_wheel_from_cache = candidate.from_cache
        if candidate.from_cache and candidate.source_url is not None:
            requirement.cached_wheel_source_link = Link(candidate.source_url)
        result.add_named_requirement(requirement)
    return result


def coerce_requirements(
    resolver,
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[Requirement]:
    string_requirements = as_requirement_strings(requirements_input)
    if string_requirements is not None:
        return [parse_requirement(req) for req in string_requirements]
    requirements = as_install_requirements(requirements_input)
    result: list[Requirement] = []
    for requirement in requirements:
        if requirement.req is None:
            continue
        result.append(
            Requirement(
                name=requirement.req.name,
                specifier=requirement.req.specifier,
                extras=requirement.req.extras,
                url=(
                    requirement.req.url
                    or (
                        requirement.link.url
                        if requirement.link is not None
                        and (
                            requirement.link.is_existing_dir
                            or requirement.link.is_file
                            or requirement.link.is_vcs
                        )
                        else None
                    )
                ),
                marker=requirement.markers,
                raw=requirement.req.raw,
            )
        )
    return result


def source_requirement_map(
    resolver,
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> tuple[dict[str, InstallRequirement], dict[str, InstallRequirement]]:
    if as_requirement_strings(requirements_input) is not None:
        return {}, {}
    requirements = as_install_requirements(requirements_input)
    result: dict[str, InstallRequirement] = {}
    by_url: dict[str, InstallRequirement] = {}
    for requirement in requirements:
        if requirement.req is None:
            continue
        name = requirement.req.canonical_name
        previous = result.get(name)
        if previous is not None and previous.hash_options and requirement.hash_options:
            merged_hashes: dict[str, list[str]] = {}
            for algorithm in (
                previous.hash_options.keys() & requirement.hash_options.keys()
            ):
                values = [
                    digest
                    for digest in requirement.hash_options[algorithm]
                    if digest in previous.hash_options[algorithm]
                ]
                merged_hashes[algorithm] = values
            requirement.hash_options = merged_hashes
        result[name] = requirement
        if requirement.link is not None:
            by_url[requirement.link.url] = requirement
        elif requirement.req.url is not None:
            by_url[requirement.req.url] = requirement
    return result, by_url
