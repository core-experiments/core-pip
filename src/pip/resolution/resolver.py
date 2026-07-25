from __future__ import annotations

import logging
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from pip.core.errors import (
    DirectoryUrlHashUnsupported,
    DistributionNotFound,
    HashMismatch,
    HashMissing,
    HashUnpinned,
    InstallationError,
    ResolutionError,
    VcsHashUnsupported,
)
from pip.core.metadata import InstalledDistribution, find_installed
from pip.core.packaging import (
    Requirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from pip.core.wheel import WheelCandidate, parse_wheel_filename, wheel_candidate
from pip.index.candidate_materialization import CandidateStream
from pip.index.links import Link
from pip.index.provider import CandidateProvider
from pip.resolution.req_install import (
    ArchiveInfo,
    DirInfo,
    DownloadInfo,
    InstallRequirement,
    VcsInfo,
    file_hashes,
)
from pip.resolution.requirement_set import RequirementSet

logger = logging.getLogger(__name__)


def _as_requirement_strings(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[str] | None:
    if isinstance(requirements_input, list) and (
        not requirements_input or isinstance(requirements_input[0], str)
    ):
        return cast(list[str], requirements_input)
    return None


def _as_install_requirements(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[InstallRequirement]:
    if isinstance(requirements_input, RequirementSet):
        return list(requirements_input.all_requirements)
    string_requirements = _as_requirement_strings(requirements_input)
    if string_requirements is not None:
        return []
    return cast(list[InstallRequirement], list(requirements_input))


@dataclass
class InstallPlan:
    candidates: list[WheelCandidate]
    graph: dict[str, set[str]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    satisfied: list[SatisfiedRequirement] = field(default_factory=list)


@dataclass(frozen=True)
class SatisfiedRequirement:
    requirement: Requirement
    distribution: InstalledDistribution


class Resolver:
    def __init__(
        self,
        *,
        provider: CandidateProvider | None = None,
        find_links: list[str] | None = None,
        index_urls: list[str] | None = None,
        no_index: bool = False,
        no_deps: bool = False,
        upgrade: bool = False,
        ignore_installed: bool = False,
        constraints: list[str] | None = None,
        allow_prereleases: bool = False,
        require_hashes: bool = False,
        upgrade_strategy: str = "only-if-needed",
        ignore_requires_python: bool = False,
        python_version: str | None = None,
    ) -> None:
        if provider is None and index_urls is None:
            provider = CandidateProvider.from_options(
                find_links=find_links or (),
                no_index=no_index,
            )
        elif provider is None:
            provider = CandidateProvider.from_options(
                find_links=find_links or (),
                index_url=index_urls[0] if index_urls else None,
                extra_index_urls=index_urls[1:] if index_urls else (),
                no_index=no_index,
            )
        self.provider = provider
        self.no_deps = no_deps
        self.upgrade = upgrade
        self.ignore_installed = ignore_installed
        self.constraints = [parse_requirement(item) for item in constraints or ()]
        self._constraints_by_name: dict[str, list[Requirement]] = {}
        for constraint in self.constraints:
            self._constraints_by_name.setdefault(
                constraint.canonical_name, []
            ).append(constraint)
        self.allow_prereleases = allow_prereleases
        if (
            allow_prereleases
            and provider is not None
            and provider.release_control is not None
        ):
            provider.release_control.apply("all_releases", ":all:")
        self.require_hashes = require_hashes
        self.upgrade_strategy = upgrade_strategy
        self.ignore_requires_python = ignore_requires_python
        self.python_version = python_version or ".".join(
            str(part) for part in sys.version_info[:3]
        )
        self._root_requirements: list[Requirement] = []
        self._root_requirement_names: set[str] = set()
        self.conflicts: list[str] = []
        self._candidate_cache: dict[
            tuple[str, str, tuple[str, ...], str | None, str], CandidateStream
        ] = {}
        self._candidate_count_cache: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None, bool], int
        ] = {}
        self._last_graph: dict[str, set[str]] | None = None
        self._incoming_requirements: dict[
            str, dict[str, tuple[Requirement, ...]]
        ] = {}
        self._unavailable_requirements: dict[str, Requirement] = {}
        self._warned_missing_extras: set[tuple[str, str]] = set()
        self._reconsidering: set[
            tuple[
                str,
                tuple[tuple[str, str, tuple[str, ...], str | None, str | None], ...],
            ]
        ] = set()
        self._failed_search_states: set[tuple[object, ...]] = set()
        self._backtrack_count = 0
        self._debug = os.environ.get("PIP_RESOLVER_DEBUG") not in {
            None,
            "",
            "0",
            "false",
            "False",
        }

    def resolve(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> InstallPlan:
        requirements = self._coerce_requirements(requirements_input)
        self._root_requirements = list(requirements)
        self._root_requirement_names = {
            requirement.canonical_name for requirement in requirements
        }
        direct_by_name: dict[str, str] = {}
        for requirement in requirements:
            if not _is_direct_requirement(requirement) or requirement.url is None:
                continue
            previous = direct_by_name.get(requirement.canonical_name)
            if previous is not None and not _direct_urls_equivalent(
                previous, requirement.url
            ):
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package "
                    "versions have conflicting dependencies."
                )
            direct_by_name[requirement.canonical_name] = requirement.url
        if self._debug:
            print("Reporter.starting()", file=sys.stdout)
        source_requirements, source_requirements_by_url = self._source_requirement_map(
            requirements_input
        )
        selected: dict[str, WheelCandidate] = {}
        selected_extras: dict[str, frozenset[str]] = {}
        satisfied: dict[str, SatisfiedRequirement] = {}
        graph: dict[str, set[str]] = {"<root>": set()}
        self._unavailable_requirements.clear()
        self._warned_missing_extras.clear()
        self._reconsidering.clear()
        self._failed_search_states.clear()
        self._candidate_count_cache.clear()
        self._incoming_requirements.clear()
        self._backtrack_count = 0
        if not self._search(
            requirements,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        ):
            if self._unavailable_requirements:
                if self._debug:
                    print(
                        "conflict is caused by unavailable distributions",
                        file=sys.stdout,
                    )
                missing = sorted(
                    self._unavailable_requirements.values(),
                    key=lambda requirement: requirement.canonical_name,
                )
                message = self._no_matching_distribution_message(missing[0])
                if missing[0].canonical_name not in self._root_requirement_names:
                    print(
                        "Additionally, some packages in these conflicts have no "
                        "matching distributions available for your environment:\n"
                        f"    {missing[0].canonical_name}\n"
                    )
                    raise ResolutionError(f"ResolutionImpossible: {message}")
                raise DistributionNotFound(message)
            detail = "; ".join(self.conflicts[-10:]) or "requirements are unsatisfiable"
            if self._debug:
                print(f"conflict is caused by: {detail}", file=sys.stdout)
            raise ResolutionError(
                "package versions have conflicting dependencies: " + detail
            )
        ordered = self._installation_order(selected, graph)
        plan = InstallPlan(
            candidates=[selected[name] for name in ordered],
            graph=graph,
            conflicts=list(self.conflicts),
            satisfied=[
                satisfied[name] for name in sorted(satisfied) if name not in selected
            ],
        )
        self._last_graph = graph
        return plan

    def get_installation_order(
        self,
        requirement_set: RequirementSet,
        *,
        graph: dict[str, set[str]] | None = None,
    ) -> list[InstallRequirement]:
        active_graph = graph or self._last_graph
        if active_graph is None:
            raise ResolutionError("installation order is unavailable before resolution")
        named = requirement_set.requirements
        ordered_names = self._installation_order(
            {
                name: WheelCandidate(
                    name=req.req.name,
                    version=Version("0"),
                    path=Path("."),
                    dependencies=(),
                )
                for name, req in named.items()
                if req.req is not None
            },
            active_graph,
        )
        return [named[name] for name in ordered_names if name in named]

    def get_topological_weights(
        self,
        graph: dict[str, set[str]],
        requirement_keys: set[str],
    ) -> dict[str, int]:
        return _topological_weights(graph, requirement_keys)

    def resolve_requirement_set(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> RequirementSet:
        plan = self.resolve(requirements_input)
        source_requirements, source_requirements_by_url = self._source_requirement_map(
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
                        hashes = file_hashes(
                            Path(candidate.source_url.removeprefix("file://"))
                        )
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

    def _coerce_requirements(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> list[Requirement]:
        string_requirements = _as_requirement_strings(requirements_input)
        if string_requirements is not None:
            return [parse_requirement(req) for req in string_requirements]
        requirements = _as_install_requirements(requirements_input)
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

    def _source_requirement_map(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> tuple[dict[str, InstallRequirement], dict[str, InstallRequirement]]:
        if _as_requirement_strings(requirements_input) is not None:
            return {}, {}
        requirements = _as_install_requirements(requirements_input)
        result: dict[str, InstallRequirement] = {}
        by_url: dict[str, InstallRequirement] = {}
        for requirement in requirements:
            if requirement.req is None:
                continue
            name = requirement.req.canonical_name
            previous = result.get(name)
            if (
                previous is not None
                and previous.hash_options
                and requirement.hash_options
            ):
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

    def _search(
        self,
        pending: list[Requirement],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> bool:
        state = self._search_state_key(
            pending, selected, selected_extras, satisfied, graph
        )
        if state in self._failed_search_states:
            return False
        resolved = self._search_uncached(
            pending,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if not resolved:
            self._failed_search_states.add(state)
        return resolved

    def _search_state_key(
        self,
        pending: list[Requirement],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
    ) -> tuple[object, ...]:
        def requirement_key(
            requirement: Requirement,
        ) -> tuple[str, str, tuple[str, ...], str, str, str]:
            return (
                requirement.canonical_name,
                str(requirement.specifier),
                tuple(sorted(requirement.extras)),
                requirement.url or "",
                requirement.marker or "",
                requirement.raw,
            )

        pending_key = tuple(sorted(requirement_key(item) for item in pending))
        selected_key = tuple(
            sorted(
                (
                    name,
                    str(candidate.version),
                    candidate.source_url or "",
                    os.fspath(candidate.path),
                    tuple(sorted(selected_extras.get(name, ()))),
                )
                for name, candidate in selected.items()
            )
        )
        satisfied_key = tuple(
            sorted(
                (
                    name,
                    item.distribution.version,
                    requirement_key(item.requirement),
                )
                for name, item in satisfied.items()
            )
        )
        graph_key = tuple(
            sorted(
                (name, tuple(sorted(dependencies)))
                for name, dependencies in graph.items()
            )
        )
        return pending_key, selected_key, satisfied_key, graph_key

    def _search_uncached(
        self,
        pending: list[Requirement],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> bool:
        if not pending:
            return self._satisfied_dependencies_are_consistent(selected, satisfied)
        requirement = self._choose_requirement(pending, selected)
        remaining = [req for req in pending if req is not requirement]
        name = requirement.canonical_name
        constrained = self._apply_constraints(requirement)
        graph.setdefault("<root>", set()).add(name)

        if name in satisfied:
            existing = satisfied[name]
            if not constrained.is_satisfied_by(
                existing.distribution.version,
                allow_prereleases=self._allow_prereleases(requirement),
            ):
                return False
            return self._search(
                remaining,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )

        if name in selected:
            selected_candidate = selected[name]
            selected_matches_direct = (
                constrained.url is None
                or _direct_urls_equivalent(
                    selected_candidate.source_url, constrained.url
                )
            )
            if selected_matches_direct and constrained.is_satisfied_by(
                selected_candidate.version,
                allow_prereleases=self._allow_prereleases(requirement),
            ):
                next_remaining = list(remaining)
                merged_extras = selected_extras.get(name, frozenset()) | frozenset(
                    constrained.extras
                )
                if merged_extras != selected_extras.get(name, frozenset()):
                    merged_candidate = self._candidate_with_extras(
                        selected_candidate, constrained, merged_extras
                    )
                    self._remove_candidate_dependencies(name, selected_candidate)
                    selected[name] = merged_candidate
                    self._add_candidate_dependencies(name, merged_candidate)
                    selected_extras[name] = merged_extras
                    graph.setdefault(name, set())
                    if not self.no_deps:
                        extra_pending: list[Requirement] = []
                        for dep in sorted(
                            merged_candidate.dependencies,
                            key=lambda item: item.canonical_name,
                        ):
                            if dep.canonical_name in graph[name]:
                                continue
                            graph[name].add(dep.canonical_name)
                            extra_pending.append(dep)
                        next_remaining = extra_pending + next_remaining
                return self._search(
                    next_remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                )
            previous_candidate = selected.pop(name)
            self._remove_candidate_dependencies(name, previous_candidate)
            previous_extras = selected_extras.pop(name, frozenset())
            reconsider = self._active_requirements_for(
                name,
                constrained,
                remaining,
            )
            reconsider_key = self._reconsideration_key(name, reconsider)
            if reconsider_key not in self._reconsidering:
                self._reconsidering.add(reconsider_key)
                try:
                    if self._search(
                        reconsider,
                        selected,
                        selected_extras,
                        satisfied,
                        graph,
                        source_requirements=source_requirements,
                        source_requirements_by_url=source_requirements_by_url,
                    ):
                        return True
                finally:
                    self._reconsidering.discard(reconsider_key)
            selected[name] = previous_candidate
            self._add_candidate_dependencies(name, previous_candidate)
            if previous_extras:
                selected_extras[name] = previous_extras
            self.conflicts.append(
                f"{constrained.raw or constrained.name} conflicts with selected "
                f"{selected[name].name}=={selected[name].version}"
            )
            return False

        installed = None if self.ignore_installed else find_installed(constrained.name)
        allow_prereleases = self._allow_prereleases(requirement)
        installed_satisfies = installed is not None and constrained.is_satisfied_by(
            installed.version,
            allow_prereleases=True,
        )
        source_requirement = source_requirements.get(name)
        direct_requirement = _is_direct_requirement(requirement) and not (
            source_requirement is not None
            and source_requirement.req is not None
            and source_requirement.req.url is None
        )
        upgrade_allowed = self._upgrade_allowed_for(name)
        if (
            installed is not None
            and installed_satisfies
            and not upgrade_allowed
            and not direct_requirement
        ):
            self._warn_missing_installed_extras(constrained, installed)
            if self._search_with_satisfied(
                constrained,
                installed,
                remaining,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            ):
                return True

        self._preflight_hash_requirement(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        candidates = self._find_candidates(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if candidates and name.startswith("file://"):
            resolved_name = candidates[0].canonical_name
            graph["<root>"].discard(name)
            self._root_requirement_names.discard(name)
            self._root_requirement_names.add(resolved_name)
            normalized = Requirement(
                name=candidates[0].name,
                specifier=constrained.specifier,
                extras=constrained.extras,
                url=constrained.url,
                marker=constrained.marker,
                raw=constrained.raw,
            )
            return self._search(
                [normalized, *remaining],
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
        best_candidate = _best_candidate(
            candidates,
            constrained,
            allow_prereleases=allow_prereleases,
        )
        if best_candidate is None and not allow_prereleases:
            prerelease_candidate = _best_candidate(
                candidates,
                constrained,
                allow_prereleases=True,
            )
            if prerelease_candidate is not None:
                allow_prereleases = True
                best_candidate = prerelease_candidate
        if (
            installed is not None
            and installed_satisfies
            and upgrade_allowed
            and not direct_requirement
        ):
            newer = False
            if best_candidate is not None:
                try:
                    installed_version = Version(installed.version)
                except ValueError:
                    newer = True
                else:
                    newer = best_candidate.version > installed_version
            if not newer:
                self._warn_missing_installed_extras(constrained, installed)
                if self._search_with_satisfied(
                    constrained,
                    installed,
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                ):
                    return True
        if not candidates:
            if requirement.canonical_name in self._root_requirement_names:
                matching_constraints = self._constraints_by_name.get(
                    requirement.canonical_name, ()
                )
                unconstrained_candidates = self.provider.find_candidates(
                    requirement
                )
                if matching_constraints and unconstrained_candidates:
                    for constraint in matching_constraints:
                        print(f"The user requested (constraint) {constraint.raw}")
                    if requirement.url is not None:
                        rejected = unconstrained_candidates[0]
                        raise ResolutionError(
                            f"Cannot install {rejected.name} {rejected.version} "
                            "because it conflicts with a constraint."
                        )
                    raise ResolutionError(
                        "ResolutionImpossible: the requirement conflicts with a "
                        "constraint"
                    )
            if requirement.canonical_name not in self._root_requirement_names:
                self._unavailable_requirements[requirement.canonical_name] = constrained
                self.conflicts.append(
                    f"{requirement.raw or requirement.name} has no matching distribution"
                )
                return False
            raise DistributionNotFound(
                self._no_matching_distribution_message(constrained)
            )

        for candidate in candidates:
            if not constrained.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                continue
            self._validate_candidate_policy(candidate)
            self._validate_candidate_constraints(candidate)
            self._warn_missing_candidate_extras(constrained, candidate)
            self._validate_candidate_hashes(
                constrained,
                candidate,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
            selected[name] = candidate
            self._add_candidate_dependencies(name, candidate)
            selected_extras[name] = frozenset(constrained.extras)
            graph.setdefault(name, set())
            next_pending = list(remaining)
            if not self.no_deps:
                dependency_pending: list[Requirement] = []
                for dep in sorted(
                    candidate.dependencies,
                    key=lambda item: item.canonical_name,
                ):
                    if not marker_applies(dep.marker, extras=constrained.extras):
                        continue
                    graph[name].add(dep.canonical_name)
                    dependency_pending.append(dep)
                next_pending = dependency_pending + next_pending
            satisfied_snapshot = dict(satisfied)
            if self._search(
                next_pending,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            ):
                return True
            selected.pop(name, None)
            self._remove_candidate_dependencies(name, candidate)
            selected_extras.pop(name, None)
            satisfied.clear()
            satisfied.update(satisfied_snapshot)
            self.conflicts.append(
                f"learned incompatibility: {candidate.name}=={candidate.version} "
                f"does not satisfy the active dependency set"
            )
            self._emit_backtracking_message()
        return False

    def _upgrade_allowed_for(self, name: str) -> bool:
        if not self.upgrade:
            return False
        if self.upgrade_strategy == "eager":
            return True
        return name in self._root_requirement_names

    def _validate_candidate_policy(self, candidate: WheelCandidate) -> None:
        self._validate_requires_python(candidate)
        self._validate_external_url_dependencies(candidate)
        if candidate.yanked_reason is not None:
            reason = candidate.yanked_reason or "<none given>"
            print(
                f"WARNING: The candidate selected is a yanked version: {candidate.name}=={candidate.version}",
                file=sys.stderr,
            )
            print(f"Reason for being yanked: {reason}", file=sys.stderr)

    def _validate_requires_python(self, candidate: WheelCandidate) -> None:
        if self.ignore_requires_python:
            return
        if not candidate.requires_python:
            return
        python_version = self.python_version
        try:
            matches = SpecifierSet(candidate.requires_python).contains(python_version)
        except ValueError:
            return
        if matches:
            return
        raise InstallationError(
            f"Package '{candidate.name}' requires a different Python: "
            f"{python_version} not in '{candidate.requires_python}'"
        )

    def _validate_external_url_dependencies(self, candidate: WheelCandidate) -> None:
        if not _is_pypi_hosted_url(candidate.source_url):
            return
        for dependency in candidate.dependencies:
            if dependency.url is None or _is_pypi_hosted_url(dependency.url):
                continue
            raise InstallationError(
                "Packages installed from PyPI cannot depend on packages "
                "which are not also hosted on PyPI.\n"
                f"{candidate.name} depends on {dependency}"
            )

    def _validate_candidate_constraints(self, candidate: WheelCandidate) -> None:
        matching = [
            constraint
            for constraint in self._constraints_by_name.get(
                candidate.canonical_name, ()
            )
            if marker_applies(constraint.marker, extras=())
        ]
        for constraint in matching:
            if not constraint.is_satisfied_by(
                candidate.version, allow_prereleases=True
            ):
                raise ResolutionError(
                    f"Cannot install {candidate.name} {candidate.version} because these "
                    "package versions have conflicting dependencies."
                )

    def _warn_missing_candidate_extras(
        self, requirement: Requirement, candidate: WheelCandidate
    ) -> None:
        if requirement.url is not None and requirement.name.startswith("file://"):
            return
        self._warn_missing_extras(
            candidate.name,
            requirement.extras,
            candidate.provided_extras,
            version=str(candidate.version),
        )

    def _warn_missing_installed_extras(
        self, requirement: Requirement, installed: InstalledDistribution
    ) -> None:
        provided = frozenset(
            canonicalize_name(value.strip())
            for value in installed.raw.metadata.get_all("Provides-Extra", [])
            if value.strip()
        )
        self._warn_missing_extras(
            requirement.name,
            requirement.extras,
            provided,
            version=installed.version,
        )

    def _warn_missing_extras(
        self,
        project_name: str,
        requested: frozenset[str],
        provided: frozenset[str],
        *,
        version: str | None = None,
    ) -> None:
        if not requested:
            return
        normalized_provided = {canonicalize_name(extra) for extra in provided}
        for extra in sorted(requested):
            normalized = canonicalize_name(extra)
            key = (canonicalize_name(project_name), normalized)
            if normalized in normalized_provided or key in self._warned_missing_extras:
                continue
            version_text = f" {version}" if version is not None else ""
            print(
                f"WARNING: {project_name}{version_text} "
                f"{self._does_not_provide_extra_text(extra)}",
                file=sys.stderr,
            )
            self._warned_missing_extras.add(key)

    @staticmethod
    def _does_not_provide_extra_text(extra: str) -> str:
        return f"does not provide the extra '{extra}'"

    def _no_matching_distribution_message(self, requirement: Requirement) -> str:
        summaries = self.provider.available_versions(requirement)
        final_only = (
            self.provider.release_control is not None
            and self.provider.release_control.allows_prereleases(requirement.name)
            is False
        )
        non_yanked_versions = sorted(
            {
                str(summary.version): summary.version
                for summary in summaries
                if not summary.is_yanked
            }.values()
        )
        yanked_versions = sorted(
            {
                str(summary.version): summary.version
                for summary in summaries
                if summary.is_yanked
            }.values()
        )
        if not non_yanked_versions:
            return (
                f"Could not find a version that satisfies the requirement "
                f"{requirement.raw or requirement.name} (from versions: none)\n"
                f"No matching distribution found for {requirement.raw or requirement.name}"
            )
        version_label = "a final version" if final_only else "a version"
        message = (
            f"Could not find {version_label} that satisfies the requirement "
            f"{requirement.raw or requirement.name} (from versions: "
            + ", ".join(str(version) for version in non_yanked_versions)
            + ")"
        )
        if yanked_versions:
            message += "\nIgnored the following yanked versions: " + ", ".join(
                str(version) for version in yanked_versions
            )
        return (
            message
            + f"\nNo matching distribution found for {requirement.raw or requirement.name}"
        )

    def _search_with_satisfied(
        self,
        requirement: Requirement,
        installed: InstalledDistribution,
        remaining: list[Requirement],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> bool:
        previous = satisfied.get(requirement.canonical_name)
        satisfied[requirement.canonical_name] = SatisfiedRequirement(
            requirement=requirement,
            distribution=installed,
        )
        next_remaining = list(remaining)
        if not self.no_deps:
            dependencies = installed.dependencies(requirement.extras)
            graph.setdefault(requirement.canonical_name, set())
            for dependency in sorted(
                dependencies,
                key=lambda item: item.canonical_name,
            ):
                graph[requirement.canonical_name].add(dependency.canonical_name)
                next_remaining.insert(0, dependency)
        if self._search(
            next_remaining,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        ):
            return True
        if previous is None:
            satisfied.pop(requirement.canonical_name, None)
        else:
            satisfied[requirement.canonical_name] = previous
        return False

    def _satisfied_dependencies_are_consistent(
        self,
        selected: dict[str, WheelCandidate],
        satisfied: dict[str, SatisfiedRequirement],
    ) -> bool:
        for item in satisfied.values():
            for dependency in item.distribution.dependencies(item.requirement.extras):
                candidate = selected.get(dependency.canonical_name)
                if candidate is not None:
                    if not dependency.is_satisfied_by(
                        candidate.version,
                        allow_prereleases=self._allow_prereleases(dependency),
                    ):
                        return False
                    continue
                existing = satisfied.get(dependency.canonical_name)
                if existing is not None:
                    if not dependency.is_satisfied_by(
                        existing.distribution.version,
                        allow_prereleases=self._allow_prereleases(dependency),
                    ):
                        return False
                    continue
                installed = find_installed(dependency.name)
                if installed is None or not dependency.is_satisfied_by(
                    installed.version,
                    allow_prereleases=self._allow_prereleases(dependency),
                ):
                    return False
        return True

    def _candidate_with_extras(
        self,
        candidate: WheelCandidate,
        requirement: Requirement,
        extras: frozenset[str],
    ) -> WheelCandidate:
        try:
            enriched = wheel_candidate(candidate.path, set(extras))
        except (OSError, ValueError):
            enriched = None
        if enriched is not None and enriched.version == candidate.version:
            return replace(
                candidate,
                dependencies=enriched.dependencies,
                provided_extras=enriched.provided_extras,
            )
        extra_requirement = Requirement(
            name=candidate.name,
            specifier=SpecifierSet(f"=={candidate.version}"),
            extras=extras,
            url=requirement.url,
            marker=None,
            raw=requirement.raw,
        )
        for extra_candidate in self._find_candidates(extra_requirement):
            if extra_candidate.version == candidate.version:
                return extra_candidate
        return candidate

    def _active_requirements_for(
        self,
        name: str,
        current: Requirement,
        remaining: list[Requirement],
    ) -> list[Requirement]:
        relevant: list[Requirement] = [current]
        deferred: list[Requirement] = []
        for requirement in remaining:
            if requirement.canonical_name == name:
                relevant.append(requirement)
            else:
                deferred.append(requirement)
        for requirement in self._root_requirements:
            if requirement.canonical_name == name:
                relevant.append(requirement)
        for dependencies in self._incoming_requirements.get(name, {}).values():
            relevant.extend(dependencies)
        unique: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None], Requirement
        ] = {}
        for requirement in relevant:
            key = (
                requirement.name,
                str(requirement.specifier),
                tuple(sorted(requirement.extras)),
                requirement.url,
                requirement.marker,
            )
            unique.setdefault(key, requirement)
        return list(unique.values()) + deferred

    def _add_candidate_dependencies(
        self, source: str, candidate: WheelCandidate
    ) -> None:
        dependencies_by_name: dict[str, list[Requirement]] = {}
        for dependency in candidate.dependencies:
            target = dependency.canonical_name
            if target == source:
                continue
            dependencies_by_name.setdefault(target, []).append(dependency)
        for target, dependencies in dependencies_by_name.items():
            self._incoming_requirements.setdefault(target, {})[source] = tuple(
                dependencies
            )

    def _remove_candidate_dependencies(
        self, source: str, candidate: WheelCandidate
    ) -> None:
        for target in {
            dependency.canonical_name
            for dependency in candidate.dependencies
            if dependency.canonical_name != source
        }:
            incoming = self._incoming_requirements.get(target)
            if incoming is None:
                continue
            incoming.pop(source, None)
            if not incoming:
                self._incoming_requirements.pop(target, None)

    def _reconsideration_key(
        self, name: str, requirements: list[Requirement]
    ) -> tuple[str, tuple[tuple[str, str, tuple[str, ...], str, str], ...]]:
        return (
            name,
            tuple(
                sorted(
                    (
                        requirement.name,
                        str(requirement.specifier),
                        tuple(sorted(requirement.extras)),
                        requirement.url or "",
                        requirement.marker or "",
                    )
                    for requirement in requirements
                )
            ),
        )

    def _emit_backtracking_message(self) -> None:
        self._backtrack_count += 1
        if self._backtrack_count in {1, 8}:
            print("This could take a while.", file=sys.stdout)
        if self._backtrack_count == 13:
            print("If you want to abort this run, press Ctrl + C.", file=sys.stdout)

    def _apply_constraints(self, requirement: Requirement) -> Requirement:
        matching = [
            constraint
            for constraint in self._constraints_by_name.get(
                requirement.canonical_name, ()
            )
            if marker_applies(constraint.marker, extras=requirement.extras)
        ]
        if not matching:
            return requirement
        direct_constraints = [constraint for constraint in matching if constraint.url]
        if direct_constraints:
            if (
                len(
                    {
                        constraint.url
                        for constraint in direct_constraints
                        if constraint.url
                    }
                )
                > 1
            ):
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package versions "
                    "have conflicting dependencies."
                )
            selected = direct_constraints[-1]
            if requirement.url is not None and not _direct_urls_equivalent(
                selected.url, requirement.url
            ):
                filename = Path(urllib.parse.urlparse(requirement.url).path).name
                parsed = parse_wheel_filename(filename)
                requested_label = (
                    f"{canonicalize_name(parsed[0])} {parsed[1]}"
                    if parsed is not None
                    else canonicalize_name(requirement.name)
                )
                raise ResolutionError(
                    f"Cannot install {requested_label}"
                    + " because these package versions have conflicting dependencies."
                )
            merged_specifier = ",".join(
                part
                for part in (str(requirement.specifier), str(selected.specifier))
                if part
            )
            return Requirement(
                name=requirement.name,
                specifier=SpecifierSet(merged_specifier),
                extras=requirement.extras,
                url=selected.url,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        spec_parts = [
            str(requirement.specifier),
            *(str(item.specifier) for item in matching),
        ]
        merged = ",".join(part for part in spec_parts if part)
        return Requirement(
            name=requirement.name,
            specifier=SpecifierSet(merged),
            extras=requirement.extras,
            url=requirement.url,
            marker=requirement.marker,
            raw=requirement.raw if not merged else f"{requirement.name}{merged}",
        )

    def _choose_requirement(
        self,
        pending: list[Requirement],
        selected: dict[str, WheelCandidate],
    ) -> Requirement:
        unresolved = [req for req in pending if req.canonical_name not in selected]
        if unresolved:
            direct = [
                req
                for req in unresolved
                if req.url is not None
                or req.raw.startswith((".", "/", "~"))
                or Path(req.raw).exists()
            ]
            if direct:
                return direct[0]
            ranked = [
                (
                    self._candidate_count(self._apply_constraints(req)) or 10**9,
                    index,
                    req,
                )
                for index, req in enumerate(unresolved)
            ]
            return min(ranked, key=lambda item: (item[0], item[1]))[2]
        return pending[0]

    def _candidate_count(self, requirement: Requirement) -> int:
        allow_prereleases = self._allow_prereleases(requirement)
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.marker,
            allow_prereleases,
        )
        cached = self._candidate_count_cache.get(key)
        if cached is not None:
            return cached
        summaries = self.provider.available_versions(requirement)
        count = sum(
            requirement.is_satisfied_by(
                summary.version,
                allow_prereleases=allow_prereleases,
            )
            for summary in summaries
        )
        if not count and not allow_prereleases:
            count = sum(
                requirement.is_satisfied_by(
                    summary.version,
                    allow_prereleases=True,
                )
                for summary in summaries
            )
        self._candidate_count_cache[key] = count
        return count

    def _find_candidates(
        self,
        requirement: Requirement,
        *,
        source_requirements: dict[str, InstallRequirement] | None = None,
        source_requirements_by_url: dict[str, InstallRequirement] | None = None,
    ) -> CandidateStream:
        key = self._candidate_cache_key(requirement)
        if key not in self._candidate_cache:
            logger.debug(
                f"candidate cache miss requirement={requirement.raw or requirement.name}"
            )
            source_req = (
                source_requirements.get(requirement.canonical_name)
                if source_requirements is not None
                else None
            )
            if source_req is None and source_requirements_by_url is not None:
                source_req = source_requirements_by_url.get(requirement.url or "")
            provider_hashes = self.provider.hashes_by_name.get(
                requirement.canonical_name
            )
            if provider_hashes is not None and not provider_hashes._allowed:
                if source_req is not None and "--hash" in str(source_req):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are "
                    "missing from some requirements."
                )
            candidates = self.provider.find_candidates(requirement)
            if (
                source_req is not None
                and provider_hashes is not None
                and not source_req.hash_options
            ):
                if "--hash" in str(source_req):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are "
                    "missing from some requirements."
                )
            allowed = None
            if source_req is not None and source_req.hash_options:
                allowed = {
                    algorithm: {digest.lower() for digest in digests}
                    for algorithm, digests in source_req.hash_options.items()
                }
            if provider_hashes is not None:
                provider_allowed = {
                    algorithm: {digest.lower() for digest in digests}
                    for algorithm, digests in provider_hashes._allowed.items()
                }
                if allowed is None:
                    allowed = provider_allowed
                else:
                    allowed = {
                        algorithm: values & provider_allowed.get(algorithm, set())
                        for algorithm, values in allowed.items()
                    }
                if not any(allowed.values()):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
            if allowed is not None:
                allowed_sha256 = allowed.get("sha256", set())

                def digest(candidate: WheelCandidate) -> str | None:
                    value = (candidate.source_hashes or {}).get("sha256")
                    return value.lower() if value is not None else None

                def keep(candidate: WheelCandidate) -> bool:
                    value = digest(candidate)
                    return value is None or value in allowed_sha256

                def decisive(candidate: WheelCandidate) -> bool:
                    value = digest(candidate)
                    return value is not None and value in allowed_sha256

                if self._debug:
                    materialized = list(candidates)
                    matches = sum(decisive(candidate) for candidate in materialized)
                    no_digest = sum(
                        digest(candidate) is None for candidate in materialized
                    )
                    discarded = [
                        candidate.source_url or str(candidate.path)
                        for candidate in materialized
                        if not keep(candidate)
                    ]
                    discarded_detail = (
                        f":\n  {chr(10).join(discarded)}" if discarded else ""
                    )
                    total_candidates = len(
                        self.provider.evaluate_links(requirement).accepted
                    )
                    discarded_count = max(total_candidates - matches - no_digest, 0)
                    discarded_text = (
                        "no candidates"
                        if not discarded_count
                        else f"{discarded_count} non-matches"
                    )
                    print(
                        "Checked %d links for project %r against %d hashes "
                        "(%d matches, %d no digest): discarding %s%s"
                        % (
                            total_candidates,
                            requirement.name,
                            len(allowed.get("sha256", set())),
                            matches,
                            no_digest,
                            discarded_text,
                            discarded_detail,
                        )
                    )
                    candidates = CandidateStream(iter(materialized))
                candidates = candidates.prefer(keep, decisive=decisive)
            self._candidate_cache[key] = candidates
        else:
            logger.debug(
                f"candidate cache hit requirement={requirement.raw or requirement.name}"
            )
        candidates = self._candidate_cache[key]
        logger.debug(
            "candidate cache ready requirement=%s",
            requirement.raw or requirement.name,
        )
        if self.ignore_requires_python:
            return candidates
        return candidates.prefer(self._candidate_matches_python)

    def _candidate_matches_python(self, candidate: WheelCandidate) -> bool:
        if not candidate.requires_python:
            return True
        try:
            return SpecifierSet(candidate.requires_python).contains(self.python_version)
        except ValueError:
            return True

    def _allow_prereleases(self, requirement: Requirement) -> bool:
        controlled = self.provider.release_control
        if controlled is not None:
            value = controlled.allows_prereleases(requirement.name)
            if value is not None:
                return value
        mentions_prerelease = any(
            spec.operator != "==="
            and not spec.version.endswith(".*")
            and Version(spec.version).is_prerelease
            for spec in requirement.specifier.specifiers
        )
        return (
            self.allow_prereleases
            or _is_direct_requirement(requirement)
            or mentions_prerelease
        )

    @staticmethod
    def _candidate_cache_key(
        requirement: Requirement,
    ) -> tuple[str, str, tuple[str, ...], str | None, str]:
        return (
            requirement.canonical_name,
            str(requirement.specifier),
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.raw,
        )

    def _preflight_hash_requirement(
        self,
        requirement: Requirement,
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> None:
        if not self.require_hashes:
            return
        source_req = source_requirements.get(requirement.canonical_name)
        if source_req is None and requirement.url is not None:
            source_req = source_requirements_by_url.get(requirement.url)
        if source_req is None or source_req.link is None:
            return
        link_url = source_req.link.url
        if link_url.startswith("git+"):
            raise VcsHashUnsupported(
                "Can't verify hashes for these requirements because we don't "
                "have a way to hash version control repositories"
            )
        if link_url.startswith("file://"):
            local_path = Path(link_url.removeprefix("file://"))
            if local_path.is_dir():
                raise DirectoryUrlHashUnsupported(
                    "Can't verify hashes for these file:// requirements because "
                    "they point to directories"
                )

    def _validate_candidate_hashes(
        self,
        requirement: Requirement,
        candidate: WheelCandidate,
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> None:
        source_req = source_requirements.get(requirement.canonical_name)
        if source_req is None and candidate.source_url is not None:
            source_req = source_requirements_by_url.get(candidate.source_url)
        provider_hashes = self.provider.hashes_by_name.get(requirement.canonical_name)
        if not self.require_hashes:
            self._validate_link_hashes(requirement, candidate, source_req)
            return
        if source_req is None:
            if provider_hashes is not None and provider_hashes._allowed:
                allowed = _hash_sets(provider_hashes._allowed)
                actual = _actual_hashes_for_candidate(candidate)
                if _hashes_match(allowed, actual):
                    return
                raise HashMismatch(
                    "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."
                )
            if requirement.url is not None:
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are missing "
                    f"from some requirements. Missing hash for:\n    {requirement.name}=={candidate.version}"
                )
            specifier = str(requirement.specifier)
            if not (
                specifier.startswith("==")
                and "*" not in specifier
                and "," not in specifier
            ):
                raise HashUnpinned(
                    "In --require-hashes mode, all requirements must have their "
                    f"versions pinned with ==. Unpinned requirement:\n    {requirement.name}"
                )
            raise HashMissing(
                "Hashes are required in --require-hashes mode, but they are missing "
                f"from some requirements. Missing hash for:\n    {requirement.name}=={candidate.version}"
            )
        if source_req.link is not None:
            link_url = source_req.link.url
            if link_url.startswith("git+"):
                raise VcsHashUnsupported(
                    "Can't verify hashes for these requirements because we don't "
                    "have a way to hash version control repositories"
                )
            if link_url.startswith("file://"):
                local_path = Path(link_url.removeprefix("file://"))
                if local_path.is_dir():
                    raise DirectoryUrlHashUnsupported(
                        "Can't verify hashes for these file:// requirements because "
                        "they point to directories"
                    )
        allowed_hashes = _allowed_hashes(source_req)
        if not allowed_hashes:
            if provider_hashes is not None and provider_hashes._allowed:
                allowed_hashes = _hash_sets(provider_hashes._allowed)
        if (
            not allowed_hashes
            and source_req.link is not None
            and source_req.link.hashes
        ):
            allowed_hashes = _hash_sets(source_req.link.hashes)
        if (
            not allowed_hashes
            and source_req.user_supplied
            and source_req.link is not None
            and source_req.link.url == candidate.source_url
            and candidate.source_hashes
        ):
            allowed_hashes = _hash_sets(candidate.source_hashes)
        actual_hashes = _actual_hashes_for_candidate(candidate)
        if not allowed_hashes:
            suggestion = ""
            sha256 = actual_hashes.get("sha256")
            if sha256:
                suggestion = f" --hash=sha256:{sha256}"
            raise HashMissing(
                "Hashes are required in --require-hashes mode, but they are missing "
                f"from some requirements. Missing hash for:\n    {source_req}{suggestion}"
            )
        if _hashes_match(allowed_hashes, actual_hashes):
            return
        if candidate.from_cache:
            print(
                "WARNING: The hashes of the source archive found in cache entry "
                "don't match, ignoring cached built wheel and re-downloading source.",
                file=sys.stderr,
            )
        expected_algorithm, expected_digests = next(
            iter(sorted(allowed_hashes.items()))
        )
        expected_digest = min(expected_digests)
        actual_digest = actual_hashes.get(expected_algorithm) or "<missing>"
        label = source_req.link.url if source_req.link is not None else str(source_req)
        raise HashMismatch(
            "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
            f"    {label}:\n"
            f"        Expected {expected_algorithm} {expected_digest}\n"
            f"             Got        {actual_digest}"
        )

    def _validate_link_hashes(
        self,
        requirement: Requirement,
        candidate: WheelCandidate,
        source_req: InstallRequirement | None,
    ) -> None:
        if not candidate.source_hashes:
            return
        if source_req is not None and _allowed_hashes(source_req):
            return
        if not _is_direct_requirement(requirement):
            return
        actual_hashes = _actual_hashes_for_candidate(candidate)
        if not actual_hashes or _hashes_match(
            _hash_sets(candidate.source_hashes), actual_hashes
        ):
            return
        expected_algorithm, expected_digests = next(
            iter(sorted(_hash_sets(candidate.source_hashes).items()))
        )
        expected_digest = min(expected_digests)
        actual_digest = actual_hashes.get(expected_algorithm) or "<missing>"
        label = candidate.source_url or str(candidate.path)
        raise HashMismatch(
            "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
            f"    {label}:\n"
            f"        Expected {expected_algorithm} {expected_digest}\n"
            f"             Got        {actual_digest}"
        )

    def _installation_order(
        self,
        selected: dict[str, WheelCandidate],
        graph: dict[str, set[str]],
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            for dep in sorted(graph.get(name, ()), reverse=True):
                if dep in selected:
                    visit(dep)
            if name in selected:
                ordered.append(name)

        for name in sorted(selected, reverse=True):
            visit(name)
        return ordered


def _best_candidate(
    candidates: Iterable[WheelCandidate],
    requirement: Requirement,
    *,
    allow_prereleases: bool,
) -> WheelCandidate | None:
    for candidate in candidates:
        if requirement.is_satisfied_by(
            candidate.version,
            allow_prereleases=allow_prereleases,
        ):
            return candidate
    return None


def _topological_weights(
    graph: dict[str, set[str]],
    requirement_keys: set[str],
) -> dict[str, int]:
    reachable: set[str] = set()
    stack = sorted(graph.get("<root>", ()))
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(sorted(graph.get(node, ())))

    relevant = reachable & set(requirement_keys)
    memo: dict[str, int] = {}

    def weight(node: str, path: set[str]) -> int:
        if node in memo:
            return memo[node]
        if node in path:
            return 0
        deps = [dep for dep in graph.get(node, ()) if dep in relevant]
        if not deps:
            memo[node] = 1
            return 1
        next_path = set(path)
        next_path.add(node)
        memo[node] = 1 + max(weight(dep, next_path) for dep in deps)
        return memo[node]

    return {node: weight(node, set()) for node in relevant}


def _allowed_hashes(requirement: InstallRequirement) -> dict[str, set[str]]:
    return {
        algorithm: set(digests)
        for algorithm, digests in requirement.hash_options.items()
        if digests
    }


def _hash_sets(hashes: dict[str, str]) -> dict[str, set[str]]:
    return {
        algorithm: {digest.lower()} for algorithm, digest in hashes.items() if digest
    }


def _actual_hashes_for_candidate(candidate: WheelCandidate) -> dict[str, str]:
    if candidate.from_cache and candidate.source_hashes:
        return dict(candidate.source_hashes)
    if candidate.source_url:
        parsed_url = urllib.parse.urlparse(candidate.source_url)
    else:
        parsed_url = None
    if parsed_url is not None and parsed_url.scheme == "file":
        try:
            path = Path(urllib.request.url2pathname(parsed_url.path))
            if path.is_file():
                return file_hashes(path)
        except OSError:
            return {}
    try:
        if candidate.path.is_file():
            return file_hashes(candidate.path)
    except OSError:
        return {}
    return {}


def _hashes_match(
    allowed_hashes: dict[str, set[str]],
    actual_hashes: dict[str, str],
) -> bool:
    for algorithm, digests in allowed_hashes.items():
        actual = actual_hashes.get(algorithm)
        if actual is not None and actual.lower() in {
            digest.lower() for digest in digests
        }:
            return True
    return False


def _is_direct_requirement(requirement: Requirement) -> bool:
    raw = requirement.raw.strip()
    return (
        requirement.url is not None
        or raw.startswith((".", "/", "~"))
        or os.sep in raw
        or (os.altsep is not None and os.altsep in raw)
    )


def _direct_urls_equivalent(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return first == second
    first_parts = urllib.parse.urlsplit(first)
    second_parts = urllib.parse.urlsplit(second)

    def local_path(parts: urllib.parse.SplitResult, original: str) -> Path | None:
        if parts.scheme.lower() == "file":
            return Path(
                urllib.request.url2pathname(urllib.parse.unquote(parts.path))
            ).resolve()
        if parts.scheme == "":
            return Path(original).resolve()
        return None

    first_path = local_path(first_parts, first)
    second_path = local_path(second_parts, second)
    if first_path is not None or second_path is not None:
        if first_path is None or second_path is None or first_path != second_path:
            return False

    def normalize_parts(
        parts: urllib.parse.SplitResult,
    ) -> urllib.parse.SplitResult:
        if parts.scheme.lower() == "file" and parts.netloc.lower() in {
            "",
            "localhost",
        }:
            return parts._replace(netloc="")
        return parts

    first_parts = normalize_parts(first_parts)
    second_parts = normalize_parts(second_parts)
    if first_parts[:3] != second_parts[:3]:
        return False

    def normalize_pairs(
        value: str, *, drop_egg: bool = False
    ) -> tuple[tuple[str, str], ...]:
        pairs = urllib.parse.parse_qsl(value, keep_blank_values=True)
        if drop_egg:
            pairs = [(key, item) for key, item in pairs if key != "egg"]
        return tuple(sorted(pairs))

    return normalize_pairs(first_parts.query) == normalize_pairs(
        second_parts.query
    ) and normalize_pairs(first_parts.fragment, drop_egg=True) == normalize_pairs(
        second_parts.fragment, drop_egg=True
    )


def _is_pypi_hosted_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return host in {
        "files.pythonhosted.org",
        "test-files.pythonhosted.org",
        "pypi.org",
        "test.pypi.org",
    }
