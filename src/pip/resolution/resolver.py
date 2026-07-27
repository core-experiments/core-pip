from __future__ import annotations

import logging
import os
import sys
import urllib.parse
import urllib.request
from bisect import bisect_left, insort
from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, cast

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
from pip.core.urls import url_to_path
from pip.core.metadata import InstalledDistribution, iter_installed_distributions
from pip.core.packaging import (
    Requirement,
    SpecifierSet,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from pip.core.wheel import WheelCandidate, wheel_candidate
from pip.index.candidate_materialization import CandidateStream, LazyWheelCandidate
from pip.index.links import Link
from pip.index.provider import CandidateProvider
from pip.index.source_locations import looks_like_path_requirement
from pip.resolution.req_install import (
    ArchiveInfo,
    DirInfo,
    DownloadInfo,
    InstallRequirement,
    VcsInfo,
    file_hashes,
)
from pip.resolution.constraints import ConstraintStore
from pip.resolution.requirement_set import RequirementSet

logger = logging.getLogger(__name__)

HTTP_URL_SCHEMES = frozenset(("http", "https"))
LOCAL_FILE_NETLOCS = frozenset(("", "localhost"))
FALSE_VALUES = frozenset((None, "", "0", "false", "False"))
PYPI_HOSTS = frozenset(
    (
        "files.pythonhosted.org",
        "test-files.pythonhosted.org",
        "pypi.org",
        "test.pypi.org",
    )
)
SOURCE_KINDS = frozenset(("source-tree", "sdist", "vcs"))
SOURCE_TREE_OR_VCS_KINDS = frozenset(("source-tree", "vcs"))


def as_requirement_strings(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[str] | None:
    if isinstance(requirements_input, list) and (
        not requirements_input or isinstance(requirements_input[0], str)
    ):
        return cast(list[str], requirements_input)
    return None


def as_install_requirements(
    requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
) -> list[InstallRequirement]:
    if isinstance(requirements_input, RequirementSet):
        return list(requirements_input.all_requirements)
    string_requirements = as_requirement_strings(requirements_input)
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


@dataclass(slots=True)
class PackageDomain:
    roots: list[Requirement] = field(default_factory=list)
    incoming: dict[str, tuple[Requirement, ...]] = field(default_factory=dict)
    requirements_internal: tuple[Requirement, ...] | None = None
    constrained_internal: tuple[Requirement, ...] | None = None
    constrained_roots_internal: tuple[Requirement, ...] | None = None
    root_version_mask: int | None = None
    incoming_version_masks: dict[str, int] = field(default_factory=dict)
    decision_count: int | None = None

    def requirements(self) -> tuple[Requirement, ...]:
        if self.requirements_internal is None:
            self.requirements_internal = (
                *self.roots,
                *(
                    requirement
                    for requirements in self.incoming.values()
                    for requirement in requirements
                ),
            )
        return self.requirements_internal

    def constrained_requirements(
        self, apply: Callable[[Requirement], Requirement]
    ) -> tuple[Requirement, ...]:
        if self.constrained_internal is None:
            self.constrained_internal = tuple(
                apply(item) for item in self.requirements()
            )
        return self.constrained_internal

    def constrained_roots(
        self, apply: Callable[[Requirement], Requirement]
    ) -> tuple[Requirement, ...]:
        if self.constrained_roots_internal is None:
            self.constrained_roots_internal = tuple(apply(item) for item in self.roots)
        return self.constrained_roots_internal

    def set_incoming(self, source: str, requirements: tuple[Requirement, ...]) -> None:
        self.incoming[source] = requirements
        self.requirements_internal = None
        self.constrained_internal = None
        self.incoming_version_masks.pop(source, None)
        self.decision_count = None

    def remove_incoming(self, source: str) -> None:
        self.incoming.pop(source, None)
        self.requirements_internal = None
        self.constrained_internal = None
        self.incoming_version_masks.pop(source, None)
        self.decision_count = None


Assignment = tuple[int, int, frozenset[str]]
RequirementStateKey = tuple[str, str, tuple[str, ...], str, str, str]
INCREMENTAL_STATE_KEY_THRESHOLD = 16


def requirement_state_key(requirement: Requirement) -> RequirementStateKey:
    return (
        requirement.canonical_name,
        str(requirement.specifier),
        tuple(sorted(requirement.extras)),
        requirement.url or "",
        requirement.marker or "",
        requirement.raw,
    )


@dataclass(frozen=True, slots=True)
class LearnedIncompatibility:
    terms: frozenset[Assignment]
    watches: tuple[int, int]


@dataclass(slots=True)
class AgendaEntry:
    requirement: Requirement
    previous: int | None
    next: int | None
    order: int
    active: bool = True


@dataclass(slots=True)
class AgendaMutation:
    kind: str
    entry: int
    previous: int | None
    next: int | None
    old_head: int | None
    old_tail: int | None
    old_front_order: int


class PendingAgenda:
    __slots__ = (
        "by_name",
        "entries_internal",
        "front_order",
        "head_internal",
        "key_cache",
        "length_internal",
        "state_keys",
        "tail_internal",
        "undo",
    )

    def __init__(self, requirements: Iterable[Requirement] = ()) -> None:
        self.entries_internal: list[AgendaEntry] = []
        self.head_internal: int | None = None
        self.tail_internal: int | None = None
        self.front_order = -1
        self.length_internal = 0
        self.undo: list[AgendaMutation] = []
        self.by_name: dict[str, set[int]] = {}
        self.key_cache: dict[int, RequirementStateKey] = {}
        self.state_keys: list[RequirementStateKey] = []
        self.append_initial(requirements)

    def key_internal(self, requirement: Requirement) -> RequirementStateKey:
        key = self.key_cache.get(id(requirement))
        if key is None:
            key = requirement_state_key(requirement)
            self.key_cache[id(requirement)] = key
        return key

    def state_key(self) -> tuple[RequirementStateKey, ...]:
        return tuple(self.state_keys)

    def remove_state_key(self, requirement: Requirement) -> None:
        key = self.key_internal(requirement)
        index = bisect_left(self.state_keys, key)
        assert self.state_keys[index] == key
        self.state_keys.pop(index)

    def add_state_key(self, requirement: Requirement) -> None:
        insort(self.state_keys, self.key_internal(requirement))

    def append_initial(self, requirements: Iterable[Requirement]) -> None:
        for order, requirement in enumerate(requirements):
            entry_id = len(self.entries_internal)
            self.entries_internal.append(
                AgendaEntry(requirement, self.tail_internal, None, order)
            )
            self.add_state_key(requirement)
            self.by_name.setdefault(requirement.canonical_name, set()).add(entry_id)
            if self.tail_internal is None:
                self.head_internal = entry_id
            else:
                self.entries_internal[self.tail_internal].next = entry_id
            self.tail_internal = entry_id
            self.length_internal += 1

    def __bool__(self) -> bool:
        return bool(self.length_internal)

    def __len__(self) -> int:
        return self.length_internal

    def __iter__(self) -> Iterator[Requirement]:
        entry_id = self.head_internal
        while entry_id is not None:
            entry = self.entries_internal[entry_id]
            yield entry.requirement
            entry_id = entry.next

    def iter_entries(self) -> Iterator[tuple[int, Requirement]]:
        entry_id = self.head_internal
        while entry_id is not None:
            entry = self.entries_internal[entry_id]
            yield entry_id, entry.requirement
            entry_id = entry.next

    def first(self) -> tuple[int, Requirement]:
        assert self.head_internal is not None
        return self.head_internal, self.entries_internal[self.head_internal].requirement

    def checkpoint(self) -> int:
        return len(self.undo)

    def remove(self, entry_id: int) -> None:
        entry = self.entries_internal[entry_id]
        assert entry.active
        self.undo.append(
            AgendaMutation(
                "remove",
                entry_id,
                entry.previous,
                entry.next,
                self.head_internal,
                self.tail_internal,
                self.front_order,
            )
        )
        if entry.previous is None:
            self.head_internal = entry.next
        else:
            self.entries_internal[entry.previous].next = entry.next
        if entry.next is None:
            self.tail_internal = entry.previous
        else:
            self.entries_internal[entry.next].previous = entry.previous
        entry.active = False
        self.remove_state_key(entry.requirement)
        name = entry.requirement.canonical_name
        self.by_name[name].remove(entry_id)
        if not self.by_name[name]:
            self.by_name.pop(name)
        self.length_internal -= 1

    def prepend(self, requirements: Iterable[Requirement]) -> None:
        items = tuple(requirements)
        if not items:
            return
        old_head = self.head_internal
        old_tail = self.tail_internal
        old_front_order = self.front_order
        first_id = len(self.entries_internal)
        previous: int | None = None
        first_order = self.front_order - len(items) + 1
        for offset, requirement in enumerate(items):
            entry_id = len(self.entries_internal)
            self.entries_internal.append(
                AgendaEntry(requirement, previous, None, first_order + offset)
            )
            self.add_state_key(requirement)
            self.by_name.setdefault(requirement.canonical_name, set()).add(entry_id)
            if previous is not None:
                self.entries_internal[previous].next = entry_id
            previous = entry_id
        last_id = len(self.entries_internal) - 1
        self.entries_internal[last_id].next = old_head
        if old_head is not None:
            self.entries_internal[old_head].previous = last_id
        else:
            self.tail_internal = last_id
        self.head_internal = first_id
        self.front_order = first_order - 1
        self.length_internal += len(items)
        self.undo.append(
            AgendaMutation(
                "prepend", first_id, None, None, old_head, old_tail, old_front_order
            )
        )

    def rollback(self, checkpoint: int) -> None:
        while len(self.undo) > checkpoint:
            mutation = self.undo.pop()
            if mutation.kind == "remove":
                entry = self.entries_internal[mutation.entry]
                entry.previous = mutation.previous
                entry.next = mutation.next
                entry.active = True
                self.add_state_key(entry.requirement)
                self.by_name.setdefault(entry.requirement.canonical_name, set()).add(
                    mutation.entry
                )
                self.head_internal = mutation.old_head
                self.tail_internal = mutation.old_tail
                if mutation.previous is not None:
                    self.entries_internal[mutation.previous].next = mutation.entry
                if mutation.next is not None:
                    self.entries_internal[mutation.next].previous = mutation.entry
                self.length_internal += 1
                continue
            removed = len(self.entries_internal) - mutation.entry
            for entry_id in range(mutation.entry, len(self.entries_internal)):
                requirement = self.entries_internal[entry_id].requirement
                self.remove_state_key(requirement)
                name = requirement.canonical_name
                self.by_name[name].remove(entry_id)
                if not self.by_name[name]:
                    self.by_name.pop(name)
            del self.entries_internal[mutation.entry :]
            self.head_internal = mutation.old_head
            self.tail_internal = mutation.old_tail
            self.front_order = mutation.old_front_order
            if mutation.old_head is not None:
                self.entries_internal[mutation.old_head].previous = None
            if mutation.old_tail is not None:
                self.entries_internal[mutation.old_tail].next = None
            self.length_internal -= removed


@dataclass(slots=True)
class SearchRequest:
    pending: PendingAgenda
    selected: dict[str, WheelCandidate]
    selected_extras: dict[str, frozenset[str]]
    satisfied: dict[str, SatisfiedRequirement]
    graph: dict[str, set[str]]
    source_requirements: dict[str, InstallRequirement]
    source_requirements_by_url: dict[str, InstallRequirement]
    checkpoint: int = 0


SearchFrame = Generator[SearchRequest, bool, bool]


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
        self.constraint_store = ConstraintStore(
            (parse_requirement(item) for item in constraints or ()),
            direct_urls_equivalent=direct_urls_equivalent,
        )
        self.constraints = list(self.constraint_store.constraints)
        self.constraints_by_name = self.constraint_store.constraints_by_name
        self.allow_prereleases = allow_prereleases
        if (
            allow_prereleases
            and provider is not None
            and provider.release_control is not None
        ):
            provider.release_control.apply("all_releases", ":all:")
        self.require_hashes = require_hashes
        self.provider.compute_source_hashes = require_hashes
        self.upgrade_strategy = upgrade_strategy
        self.ignore_requires_python = ignore_requires_python
        self.python_version = python_version or ".".join(
            str(part) for part in sys.version_info[:3]
        )
        self.root_requirements: list[Requirement] = []
        self.root_requirement_names: set[str] = set()
        self.conflicts: list[str] = []
        self.candidate_cache: dict[tuple[object, ...], CandidateStream] = {}
        self.candidate_count_cache: dict[
            tuple[str, str, tuple[str, ...], str | None, str | None, bool], int
        ] = {}
        self.decision_count_cache: dict[int, int] = {}
        self.domain_viability_cache: dict[tuple[str, tuple[str, ...]], bool] = {}
        self.version_tables: dict[str, tuple[Version, ...]] = {}
        self.version_masks: dict[tuple[str, str, bool], int] = {}
        self.active_version_masks: dict[
            tuple[str, tuple[tuple[str, bool], ...]], int
        ] = {}
        self.allowed_versions_cache: dict[tuple[str, int], frozenset[Version]] = {}
        self.allow_prereleases_cache: dict[tuple[str, str, str | None, str], bool] = {}
        self.last_graph: dict[str, set[str]] | None = None
        self.incoming_requirements: dict[str, dict[str, tuple[Requirement, ...]]] = {}
        self.domains_internal: dict[str, PackageDomain] = {}
        self.unavailable_requirements: dict[str, Requirement] = {}
        self.warned_missing_extras: set[tuple[str, str]] = set()
        self.reconsidering: set[
            tuple[
                str,
                tuple[tuple[str, str, tuple[str, ...], str | None, str | None], ...],
            ]
        ] = set()
        self.failed_search_states: set[tuple[object, ...]] = set()
        self.candidate_state_keys: dict[int, tuple[str, str, str, str]] = {}
        self.requirement_state_keys: dict[int, RequirementStateKey] = {}
        self.candidate_dependency_groups: dict[
            tuple[int, frozenset[str]],
            tuple[tuple[str, tuple[Requirement, ...]], ...],
        ] = {}
        self.backtrack_count = 0
        self.root_incompatibilities: set[tuple[int, frozenset[str]]] = set()
        self.root_unsatisfiable_domains: set[tuple[object, ...]] = set()
        self.seen_candidate_conflicts: set[tuple[int, frozenset[str]]] = set()
        self.root_incompatibility_hits = 0
        self.last_conflict_was_root = False
        self.package_ids: dict[str, int] = {}
        self.package_names_internal: list[str] = []
        self.candidate_ids: dict[tuple[int, Version, str], int] = {}
        self.conflict_activity: list[int] = []
        self.learned_incompatibilities: list[LearnedIncompatibility] = []
        self.learned_incompatibility_terms: set[frozenset[Assignment]] = set()
        self.incompatibility_watches: dict[int, set[int]] = {}
        self.installed_by_name_internal: dict[str, InstalledDistribution] | None = None
        self.debug_internal = os.environ.get("PIP_RESOLVER_DEBUG") not in FALSE_VALUES

    def resolve(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> InstallPlan:
        requirements = self.coerce_requirements(requirements_input)
        self.root_requirements = list(requirements)
        self.root_requirement_names = {
            requirement.canonical_name for requirement in requirements
        }
        direct_by_name: dict[str, str] = {}
        for requirement in requirements:
            if not is_direct_requirement(requirement) or requirement.url is None:
                continue
            previous = direct_by_name.get(requirement.canonical_name)
            if previous is not None and not direct_urls_equivalent(
                previous, requirement.url
            ):
                raise ResolutionError(
                    f"Cannot install {requirement.name} because these package "
                    "versions have conflicting dependencies."
                )
            direct_by_name[requirement.canonical_name] = requirement.url
        if self.debug_internal:
            print("Reporter.starting()", file=sys.stdout)
        source_requirements, source_requirements_by_url = self.source_requirement_map(
            requirements_input
        )
        selected: dict[str, WheelCandidate] = {}
        selected_extras: dict[str, frozenset[str]] = {}
        satisfied: dict[str, SatisfiedRequirement] = {}
        graph: dict[str, set[str]] = {"<root>": set()}
        self.unavailable_requirements.clear()
        self.warned_missing_extras.clear()
        self.reconsidering.clear()
        self.failed_search_states.clear()
        self.candidate_state_keys.clear()
        self.requirement_state_keys.clear()
        self.candidate_dependency_groups.clear()
        self.candidate_count_cache.clear()
        self.decision_count_cache.clear()
        self.domain_viability_cache.clear()
        self.version_tables.clear()
        self.version_masks.clear()
        self.active_version_masks.clear()
        self.allowed_versions_cache.clear()
        self.incoming_requirements.clear()
        self.domains_internal.clear()
        self.root_incompatibilities.clear()
        self.root_unsatisfiable_domains.clear()
        self.seen_candidate_conflicts.clear()
        self.root_incompatibility_hits = 0
        self.last_conflict_was_root = False
        self.package_ids.clear()
        self.package_names_internal.clear()
        self.candidate_ids.clear()
        self.conflict_activity.clear()
        self.learned_incompatibilities.clear()
        self.learned_incompatibility_terms.clear()
        self.incompatibility_watches.clear()
        for requirement in requirements:
            domain = self.domains_internal.setdefault(
                requirement.canonical_name, PackageDomain()
            )
            domain.roots.append(requirement)
            domain.requirements_internal = None
            domain.constrained_internal = None
            domain.constrained_roots_internal = None
        self.backtrack_count = 0
        if not self.search_internal(
            requirements,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        ):
            if self.unavailable_requirements:
                if self.debug_internal:
                    print(
                        "conflict is caused by unavailable distributions",
                        file=sys.stdout,
                    )
                missing = sorted(
                    self.unavailable_requirements.values(),
                    key=lambda requirement: requirement.canonical_name,
                )
                message = self.no_matching_distribution_message(missing[0])
                if missing[0].canonical_name not in self.root_requirement_names:
                    print(
                        "Additionally, some packages in these conflicts have no "
                        "matching distributions available for your environment:\n"
                        f"    {missing[0].canonical_name}\n"
                    )
                    raise ResolutionError(f"ResolutionImpossible: {message}")
                raise DistributionNotFound(message)
            for root in requirements:
                if root.url is None:
                    continue
                for candidate in self.provider.find_candidates(root):
                    constraints = self.constraints_by_name.get(
                        candidate.canonical_name, ()
                    )
                    if (
                        any(
                            not constraint.is_satisfied_by(
                                candidate.version, allow_prereleases=True
                            )
                            for constraint in constraints
                        )
                        and candidate.source_kind in SOURCE_KINDS
                    ):
                        raise ResolutionError(
                            f"Cannot install {candidate.name} {candidate.version} "
                            "because it conflicts with a constraint."
                        )
            detail = "; ".join(self.conflicts[-10:]) or "requirements are unsatisfiable"
            if self.debug_internal:
                print(f"conflict is caused by: {detail}", file=sys.stdout)
            raise ResolutionError(
                "package versions have conflicting dependencies: " + detail
            )
        ordered = self.installation_order(selected, graph)
        plan = InstallPlan(
            candidates=[
                self.finalize_source_hashes(selected[name]) for name in ordered
            ],
            graph=graph,
            conflicts=list(self.conflicts),
            satisfied=[
                satisfied[name] for name in sorted(satisfied) if name not in selected
            ],
        )
        self.last_graph = graph
        return plan

    @staticmethod
    def finalize_source_hashes(candidate: WheelCandidate) -> WheelCandidate:
        if isinstance(candidate, LazyWheelCandidate):
            candidate = candidate.materialize()
        if (
            candidate.source_hashes
            or candidate.source_kind in SOURCE_TREE_OR_VCS_KINDS
            or (candidate.from_cache)
        ):
            return candidate
        hashes = actual_hashes_for_candidate(candidate)
        return replace(candidate, source_hashes=hashes or None)

    def get_installation_order(
        self,
        requirement_set: RequirementSet,
        *,
        graph: dict[str, set[str]] | None = None,
    ) -> list[InstallRequirement]:
        active_graph = graph or self.last_graph
        if active_graph is None:
            raise ResolutionError("installation order is unavailable before resolution")
        named = requirement_set.requirements
        ordered_names = self.installation_order(
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
        return topological_weights(graph, requirement_keys)

    def resolve_requirement_set(
        self,
        requirements_input: RequirementSet | Iterable[InstallRequirement] | list[str],
    ) -> RequirementSet:
        plan = self.resolve(requirements_input)
        source_requirements, source_requirements_by_url = self.source_requirement_map(
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
        self,
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
        self,
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

    def search_internal(
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
        frames = [
            self.search_frame_internal(
                SearchRequest(
                    PendingAgenda(pending),
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                )
            )
        ]
        result: bool | None = None
        while frames:
            frame = frames[-1]
            try:
                request = frame.send(result) if result is not None else next(frame)
            except StopIteration as completed:
                frames.pop()
                result = completed.value
                continue
            frames.append(self.search_frame_internal(request))
            result = None
        return bool(result)

    def search_frame_internal(self, request: SearchRequest) -> SearchFrame:
        try:
            resolved = yield from self.search_frame_inner(request)
        except BaseException:
            request.pending.rollback(request.checkpoint)
            raise
        if not resolved:
            request.pending.rollback(request.checkpoint)
        return resolved

    def search_frame_inner(self, request: SearchRequest) -> SearchFrame:
        pending = request.pending
        selected = request.selected
        selected_extras = request.selected_extras
        satisfied = request.satisfied
        graph = request.graph
        source_requirements = request.source_requirements
        source_requirements_by_url = request.source_requirements_by_url
        if (
            selected
            and len(pending) <= 1
            and all(
                name == "<root>" or len(dependencies) <= 1
                for name, dependencies in graph.items()
            )
        ):
            return (
                yield from self.search_uncached(
                    pending,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                )
            )
        state: tuple[object, ...] | None = None
        # Small searches are cheap to key and retain the eager behavior used by
        # callers that inspect the memoization hook.  Once the graph is broad,
        # defer key construction until there is a failed state to consult.
        if self.failed_search_states or len(selected) <= 8:
            state = self.search_state_key_internal(
                pending, selected, selected_extras, satisfied, graph
            )
            if state in self.failed_search_states:
                return False
        resolved = yield from self.search_uncached(
            pending,
            selected,
            selected_extras,
            satisfied,
            graph,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if not resolved:
            if state is None:
                state = self.search_state_key_internal(
                    pending, selected, selected_extras, satisfied, graph
                )
            assert state is not None
            self.failed_search_states.add(state)
        return resolved

    def search_state_key_internal(
        self,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph_internal: dict[str, set[str]],
    ) -> tuple[object, ...]:
        def requirement_key(requirement: Requirement) -> RequirementStateKey:
            key = self.requirement_state_keys.get(id(requirement))
            if key is None:
                key = requirement_state_key(requirement)
                self.requirement_state_keys[id(requirement)] = key
            return key

        def candidate_key(candidate: WheelCandidate) -> tuple[str, str, str, str]:
            key = self.candidate_state_keys.get(id(candidate))
            if key is None:
                key = (
                    candidate.canonical_name,
                    str(candidate.version),
                    candidate.source_url or "",
                    os.fspath(candidate.path),
                )
                self.candidate_state_keys[id(candidate)] = key
            return key

        pending_key = pending.state_key()
        selected_key = tuple(
            sorted(
                (
                    *candidate_key(candidate),
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
        return pending_key, selected_key, satisfied_key

    def search_uncached(
        self,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> SearchFrame:
        if not pending:
            return self.satisfied_dependencies_are_consistent(selected, satisfied)
        entry_id, requirement = self.choose_requirement(pending, selected)
        pending.remove(entry_id)
        remaining = pending
        name = requirement.canonical_name
        constrained = self.apply_constraints(requirement)
        graph.setdefault("<root>", set()).add(name)

        if name in satisfied:
            existing = satisfied[name]
            if not constrained.is_satisfied_by(
                existing.distribution.version,
                allow_prereleases=self.allow_prereleases_internal(requirement),
            ):
                return False
            return (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=remaining.checkpoint(),
                )
            )

        if name in selected:
            selected_candidate = selected[name]
            selected_matches_direct = constrained.url is None or direct_urls_equivalent(
                selected_candidate.source_url, constrained.url
            )
            if selected_matches_direct and constrained.is_satisfied_by(
                selected_candidate.version,
                allow_prereleases=self.allow_prereleases_internal(requirement),
            ):
                branch_checkpoint = remaining.checkpoint()
                merged_extras = selected_extras.get(name, frozenset()) | frozenset(
                    constrained.extras
                )
                if merged_extras != selected_extras.get(name, frozenset()):
                    merged_candidate = self.candidate_with_extras(
                        selected_candidate, constrained, merged_extras
                    )
                    self.remove_candidate_dependencies(name, selected_candidate)
                    selected[name] = merged_candidate
                    self.add_candidate_dependencies(name, merged_candidate)
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
                        remaining.prepend(extra_pending)
                return (
                    yield SearchRequest(
                        remaining,
                        selected,
                        selected_extras,
                        satisfied,
                        graph,
                        source_requirements,
                        source_requirements_by_url,
                        checkpoint=branch_checkpoint,
                    )
                )
            previous_candidate = selected.pop(name)
            self.remove_candidate_dependencies(name, previous_candidate)
            previous_extras = selected_extras.pop(name, frozenset())
            reconsider = self.active_requirements_for(
                name,
                constrained,
                remaining,
            )
            reconsider_key = self.reconsideration_key(name, reconsider)
            if reconsider_key not in self.reconsidering:
                self.reconsidering.add(reconsider_key)
                try:
                    if (
                        yield SearchRequest(
                            PendingAgenda(reconsider),
                            selected,
                            selected_extras,
                            satisfied,
                            graph,
                            source_requirements,
                            source_requirements_by_url,
                        )
                    ):
                        return True
                finally:
                    self.reconsidering.discard(reconsider_key)
            selected[name] = previous_candidate
            self.add_candidate_dependencies(name, previous_candidate)
            if previous_extras:
                selected_extras[name] = previous_extras
            self.conflicts.append(
                f"{constrained.raw or constrained.name} conflicts with selected "
                f"{selected[name].name}=={selected[name].version}"
            )
            return False

        installed = (
            None
            if self.ignore_installed
            else self.find_installed_internal(constrained.name)
        )
        allow_prereleases = self.allow_prereleases_internal(requirement)
        installed_satisfies = installed is not None and constrained.is_satisfied_by(
            installed.version,
            allow_prereleases=True,
        )
        source_requirement = source_requirements.get(name)
        direct_requirement = is_direct_requirement(requirement) and not (
            source_requirement is not None
            and source_requirement.req is not None
            and source_requirement.req.url is None
        )
        upgrade_allowed = self.upgrade_allowed_for(name)
        if (
            installed is not None
            and installed_satisfies
            and not upgrade_allowed
            and not direct_requirement
        ):
            self.warn_missing_installed_extras(constrained, installed)
            if (
                yield from self.search_with_satisfied(
                    constrained,
                    installed,
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements=source_requirements,
                    source_requirements_by_url=source_requirements_by_url,
                )
            ):
                return True

        self.preflight_hash_requirement(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        candidates = self.find_candidates_internal(
            constrained,
            source_requirements=source_requirements,
            source_requirements_by_url=source_requirements_by_url,
        )
        if candidates and (
            name.startswith("file://")
            or (
                requirement.url is not None
                and candidates[0].canonical_name != requirement.canonical_name
            )
        ):
            resolved_name = candidates[0].canonical_name
            graph["<root>"].discard(name)
            self.root_requirement_names.discard(name)
            self.root_requirement_names.add(resolved_name)
            normalized = Requirement(
                name=candidates[0].name,
                specifier=constrained.specifier,
                extras=constrained.extras,
                url=constrained.url,
                marker=constrained.marker,
                raw=constrained.raw,
            )
            branch_checkpoint = remaining.checkpoint()
            remaining.prepend((normalized,))
            return (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=branch_checkpoint,
                )
            )
        best_candidate = best_candidate_internal(
            candidates,
            constrained,
            allow_prereleases=allow_prereleases,
        )
        if best_candidate is None and not allow_prereleases:
            prerelease_candidate = best_candidate_internal(
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
                self.warn_missing_installed_extras(constrained, installed)
                if (
                    yield from self.search_with_satisfied(
                        constrained,
                        installed,
                        remaining,
                        selected,
                        selected_extras,
                        satisfied,
                        graph,
                        source_requirements=source_requirements,
                        source_requirements_by_url=source_requirements_by_url,
                    )
                ):
                    return True
        if not candidates:
            if (
                requirement.canonical_name in self.root_requirement_names
                or requirement.url is not None
            ):
                matching_constraints = self.constraints_by_name.get(
                    requirement.canonical_name, ()
                )
                unconstrained_candidates = self.provider.find_candidates(requirement)
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
            if requirement.canonical_name not in self.root_requirement_names:
                self.unavailable_requirements[requirement.canonical_name] = constrained
                self.conflicts.append(
                    f"{requirement.raw or requirement.name} has no matching distribution"
                )
                return False
            raise DistributionNotFound(
                self.no_matching_distribution_message(constrained)
            )

        attempted_candidates = 0
        root_rejections = 0
        for candidate in candidates:
            if not constrained.is_satisfied_by(
                candidate.version,
                allow_prereleases=allow_prereleases,
            ):
                continue
            self.validate_candidate_policy(candidate)
            self.validate_candidate_constraints(candidate)
            attempted_candidates += 1
            incompatibility_key: tuple[int, frozenset[str]] | None = None
            if self.root_incompatibilities:
                incompatibility_key = self.candidate_incompatibility_key(
                    candidate, constrained.extras
                )
                if incompatibility_key in self.root_incompatibilities:
                    self.root_incompatibility_hits += 1
                    root_rejections += 1
                    self.emit_backtracking_message()
                    continue
            if self.violates_watched_incompatibility(
                candidate, constrained.extras, selected, selected_extras
            ):
                self.emit_backtracking_message()
                continue
            if self.candidate_dependencies_conflict(
                candidate,
                extras=constrained.extras,
                selected=selected,
                selected_extras=selected_extras,
            ):
                if self.last_conflict_was_root:
                    root_rejections += 1
                self.conflicts.append(
                    f"learned incompatibility: {candidate.name}=={candidate.version} "
                    "introduces contradictory exact dependencies"
                )
                self.emit_backtracking_message()
                continue
            self.warn_missing_candidate_extras(constrained, candidate)
            self.validate_candidate_hashes(
                constrained,
                candidate,
                source_requirements=source_requirements,
                source_requirements_by_url=source_requirements_by_url,
            )
            selected[name] = candidate
            self.add_candidate_dependencies(name, candidate)
            selected_extras[name] = frozenset(constrained.extras)
            graph.setdefault(name, set())
            branch_checkpoint = remaining.checkpoint()
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
                remaining.prepend(dependency_pending)
            satisfied_snapshot = dict(satisfied)
            if (
                yield SearchRequest(
                    remaining,
                    selected,
                    selected_extras,
                    satisfied,
                    graph,
                    source_requirements,
                    source_requirements_by_url,
                    checkpoint=branch_checkpoint,
                )
            ):
                return True
            if any(
                self.candidate_cache_key(dependency) in self.root_unsatisfiable_domains
                for _, dependencies in self.grouped_candidate_dependencies(
                    candidate, constrained.extras
                )
                for dependency in dependencies
            ):
                if incompatibility_key is None:
                    incompatibility_key = self.candidate_incompatibility_key(
                        candidate, constrained.extras
                    )
                self.root_incompatibilities.add(incompatibility_key)
                root_rejections += 1
            selected.pop(name, None)
            self.remove_candidate_dependencies(name, candidate)
            selected_extras.pop(name, None)
            satisfied.clear()
            satisfied.update(satisfied_snapshot)
            self.conflicts.append(
                f"learned incompatibility: {candidate.name}=={candidate.version} "
                f"does not satisfy the active dependency set"
            )
            self.emit_backtracking_message()
        if attempted_candidates and root_rejections == attempted_candidates:
            self.root_unsatisfiable_domains.add(self.candidate_cache_key(constrained))
        return False

    def grouped_candidate_dependencies(
        self, candidate: WheelCandidate, extras: frozenset[str]
    ) -> tuple[tuple[str, tuple[Requirement, ...]], ...]:
        key = id(candidate), extras
        cached = self.candidate_dependency_groups.get(key)
        if cached is not None:
            return cached
        grouped: dict[str, list[Requirement]] = {}
        for dependency in candidate.dependencies:
            if not marker_applies(dependency.marker, extras=extras):
                continue
            grouped.setdefault(dependency.canonical_name, []).append(
                self.apply_constraints(dependency)
            )
        result = tuple(
            (name, tuple(dependencies)) for name, dependencies in grouped.items()
        )
        self.candidate_dependency_groups[key] = result
        return result

    def candidate_dependencies_conflict(
        self,
        candidate: WheelCandidate,
        *,
        extras: frozenset[str],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> bool:
        self.last_conflict_was_root = False
        grouped = self.grouped_candidate_dependencies(candidate, extras)
        for target, dependencies in grouped:
            for constrained_dependency in dependencies:
                if (
                    self.candidate_cache_key(constrained_dependency)
                    in self.root_unsatisfiable_domains
                ):
                    self.root_incompatibilities.add(
                        self.candidate_incompatibility_key(candidate, extras)
                    )
                    self.bump_conflict_activity(candidate.canonical_name, target)
                    self.last_conflict_was_root = True
                    return True
        active_targets = self.domains_internal.keys() & dict(grouped).keys()
        if not active_targets:
            return False
        for target, dependencies in grouped:
            if target not in active_targets:
                continue
            domain = self.domains_internal[target]
            constrained_active = domain.constrained_requirements(self.apply_constraints)
            for constrained_dependency in dependencies:
                if not self.dependency_domain_conflicts(
                    constrained_dependency,
                    constrained_active,
                    domain=domain,
                ):
                    continue
                self.learn_watched_incompatibility(
                    candidate,
                    extras,
                    constrained_dependency,
                    domain,
                    selected,
                    selected_extras,
                )
                self.bump_conflict_activity(candidate.canonical_name, target)
                incompatibility_key = self.candidate_incompatibility_key(
                    candidate, extras
                )
                if incompatibility_key in self.seen_candidate_conflicts:
                    constrained_roots = domain.constrained_roots(self.apply_constraints)
                    if constrained_roots and self.dependency_domain_conflicts(
                        constrained_dependency, constrained_roots
                    ):
                        self.root_incompatibilities.add(incompatibility_key)
                        self.last_conflict_was_root = True
                else:
                    self.seen_candidate_conflicts.add(incompatibility_key)
                return True
        return False

    def package_id_internal(self, name: str) -> int:
        package_id = self.package_ids.get(name)
        canonical_name = name
        if package_id is None:
            canonical_name = canonicalize_name(name)
            package_id = self.package_ids.get(canonical_name)
        if package_id is None:
            package_id = len(self.package_names_internal)
            self.package_ids[canonical_name] = package_id
            self.package_names_internal.append(canonical_name)
            self.conflict_activity.append(0)
        return package_id

    def candidate_assignment(
        self, candidate: WheelCandidate, extras: frozenset[str]
    ) -> Assignment:
        package_id = self.package_id_internal(candidate.canonical_name)
        identity = (
            package_id,
            candidate.version,
            candidate.source_url or os.fspath(candidate.path),
        )
        candidate_id = self.candidate_ids.get(identity)
        if candidate_id is None:
            candidate_id = len(self.candidate_ids)
            self.candidate_ids[identity] = candidate_id
        return package_id, candidate_id, extras

    def bump_conflict_activity(self, *names: str) -> None:
        for name in names:
            package_id = self.package_id_internal(name)
            self.conflict_activity[package_id] += 1

    def learn_watched_incompatibility(
        self,
        candidate: WheelCandidate,
        extras: frozenset[str],
        dependency: Requirement,
        domain: PackageDomain,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> None:
        candidate_term = self.candidate_assignment(candidate, extras)
        terms = {candidate_term}
        sources = [source for source in domain.incoming if source in selected]
        if len(sources) > 1:
            roots = tuple(self.apply_constraints(item) for item in domain.roots)
            requirements_by_source = {
                source: tuple(
                    self.apply_constraints(item) for item in domain.incoming[source]
                )
                for source in sources
            }
            exact_sources = self.minimal_exact_conflict_sources(
                dependency, roots, requirements_by_source, sources
            )
            if exact_sources is not None:
                sources = exact_sources
            else:
                necessary_sources = list(sources)
                for source in sources:
                    without_source = tuple(
                        requirement
                        for other in necessary_sources
                        if other != source
                        for requirement in requirements_by_source[other]
                    )
                    if self.dependency_domain_conflicts(
                        dependency, (*roots, *without_source)
                    ):
                        necessary_sources.remove(source)
                sources = necessary_sources
        for source in sources:
            selected_candidate = selected.get(source)
            if selected_candidate is not None:
                terms.add(
                    self.candidate_assignment(
                        selected_candidate,
                        selected_extras.get(source, frozenset()),
                    )
                )
        frozen_terms = frozenset(terms)
        if len(frozen_terms) < 2 or frozen_terms in self.learned_incompatibility_terms:
            return
        other_watch = next(term[0] for term in frozen_terms if term != candidate_term)
        watches = candidate_term[0], other_watch
        incompatibility_id = len(self.learned_incompatibilities)
        self.learned_incompatibilities.append(
            LearnedIncompatibility(frozen_terms, watches)
        )
        self.learned_incompatibility_terms.add(frozen_terms)
        for package_id in watches:
            self.incompatibility_watches.setdefault(package_id, set()).add(
                incompatibility_id
            )

    @staticmethod
    def minimal_exact_conflict_sources(
        dependency: Requirement,
        roots: tuple[Requirement, ...],
        requirements_by_source: dict[str, tuple[Requirement, ...]],
        sources: list[str],
    ) -> list[str] | None:
        dependency_version = exact_pinned_version(dependency)
        if dependency_version is not None:
            if any(
                not requirement.is_satisfied_by(dependency_version)
                for requirement in roots
            ):
                return []
            conflicting = next(
                (
                    source
                    for source in reversed(sources)
                    if any(
                        not requirement.is_satisfied_by(dependency_version)
                        for requirement in requirements_by_source[source]
                    )
                ),
                None,
            )
            return [conflicting] if conflicting is not None else None
        for requirement in roots:
            version = exact_pinned_version(requirement)
            if version is not None and not dependency.is_satisfied_by(version):
                return []
        conflicting = next(
            (
                source
                for source in reversed(sources)
                if any(
                    (version := exact_pinned_version(requirement)) is not None
                    and not dependency.is_satisfied_by(version)
                    for requirement in requirements_by_source[source]
                )
            ),
            None,
        )
        return [conflicting] if conflicting is not None else None

    def violates_watched_incompatibility(
        self,
        candidate: WheelCandidate,
        extras: frozenset[str],
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
    ) -> bool:
        if not self.learned_incompatibilities:
            return False
        candidate_term = self.candidate_assignment(candidate, extras)
        for incompatibility_id in self.incompatibility_watches.get(
            candidate_term[0], ()
        ):
            incompatibility = self.learned_incompatibilities[incompatibility_id]
            if candidate_term not in incompatibility.terms:
                continue
            if all(
                term == candidate_term
                or (
                    (
                        selected_candidate := selected.get(
                            self.package_names_internal[term[0]]
                        )
                    )
                    is not None
                    and self.candidate_assignment(
                        selected_candidate,
                        selected_extras.get(
                            self.package_names_internal[term[0]], frozenset()
                        ),
                    )
                    == term
                )
                for term in incompatibility.terms
            ):
                self.bump_conflict_activity(
                    *(
                        self.package_names_internal[term[0]]
                        for term in incompatibility.terms
                    )
                )
                return True
        return False

    def dependency_domain_conflicts(
        self,
        dependency: Requirement,
        active: tuple[Requirement, ...],
        *,
        domain: PackageDomain | None = None,
    ) -> bool:
        dependency_version = exact_pinned_version(dependency)
        if (
            dependency_version is not None
            and domain is not None
            and len(active) >= INCREMENTAL_STATE_KEY_THRESHOLD
        ):
            versions = self.version_table(dependency)
            active_mask = self.domain_version_mask(domain)
            if versions is not None and active_mask is not None:
                try:
                    version_index = versions.index(dependency_version)
                except ValueError:
                    pass
                else:
                    return not active_mask & (1 << version_index)
        if dependency_version is not None and any(
            not requirement.is_satisfied_by(dependency_version)
            for requirement in active
        ):
            return True
        active_versions = tuple(exact_pinned_version(item) for item in active)
        if any(
            version is not None and not dependency.is_satisfied_by(version)
            for version in active_versions
        ):
            return True
        if dependency_version is not None or any(
            version is not None for version in active_versions
        ):
            return False
        requirements = (dependency, *active)
        if any(requirement.url is not None for requirement in requirements):
            return False
        if specifier_intersection_is_empty(requirements):
            return True
        version_mask = self.requirements_version_mask(requirements)
        if version_mask is not None:
            return version_mask == 0
        domain_key = (
            dependency.canonical_name,
            tuple(sorted(str(item.specifier) for item in requirements)),
        )
        viable = self.domain_viability_cache.get(domain_key)
        if viable is None:
            viable = any(
                all(
                    requirement.is_satisfied_by(
                        summary.version,
                        allow_prereleases=True,
                    )
                    for requirement in requirements
                )
                for summary in self.provider.available_versions(dependency)
            )
            self.domain_viability_cache[domain_key] = viable
        return not viable

    def version_table(self, requirement: Requirement) -> tuple[Version, ...] | None:
        if requirement.url is not None:
            return None
        name = requirement.canonical_name
        cached = self.version_tables.get(name)
        if cached is not None:
            return cached
        summaries = self.provider.available_versions(requirement)
        versions = tuple(dict.fromkeys(summary.version for summary in summaries))
        self.version_tables[name] = versions
        return versions

    def requirement_version_mask(
        self,
        requirement: Requirement,
        versions: tuple[Version, ...],
        *,
        allow_prereleases: bool,
    ) -> int:
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            allow_prereleases,
        )
        cached = self.version_masks.get(key)
        if cached is not None:
            return cached
        mask = sum(
            1 << index
            for index, version in enumerate(versions)
            if requirement.is_satisfied_by(version, allow_prereleases=allow_prereleases)
        )
        self.version_masks[key] = mask
        return mask

    def requirements_version_mask(
        self, requirements: tuple[Requirement, ...]
    ) -> int | None:
        if not requirements or any(item.url is not None for item in requirements):
            return None
        versions = self.version_table(requirements[0])
        if versions is None:
            return None
        parts = tuple(
            (
                str(requirement.specifier),
                self.allow_prereleases_internal(requirement),
            )
            for requirement in requirements
        )
        key = requirements[0].canonical_name, tuple(sorted(parts))
        cached = self.active_version_masks.get(key)
        if cached is not None:
            return cached
        mask = (1 << len(versions)) - 1
        for requirement, (_, allow_prereleases) in zip(requirements, parts):
            requirement_mask = self.requirement_version_mask(
                requirement,
                versions,
                allow_prereleases=allow_prereleases,
            )
            if not requirement_mask and not allow_prereleases:
                requirement_mask = self.requirement_version_mask(
                    requirement,
                    versions,
                    allow_prereleases=True,
                )
            mask &= requirement_mask
            if not mask:
                break
        self.active_version_masks[key] = mask
        return mask

    def domain_version_mask(self, domain: PackageDomain) -> int | None:
        requirements = domain.requirements()
        if not requirements or any(item.url is not None for item in requirements):
            return None
        versions = self.version_table(requirements[0])
        if versions is None:
            return None
        mask = (1 << len(versions)) - 1
        if domain.roots:
            if domain.root_version_mask is None:
                root_mask = self.requirements_version_mask(
                    domain.constrained_roots(self.apply_constraints)
                )
                if root_mask is None:
                    return None
                domain.root_version_mask = root_mask
            mask &= domain.root_version_mask
        for source, incoming in domain.incoming.items():
            incoming_mask = domain.incoming_version_masks.get(source)
            if incoming_mask is None:
                incoming_mask = self.requirements_version_mask(
                    tuple(self.apply_constraints(item) for item in incoming)
                )
                if incoming_mask is None:
                    return None
                domain.incoming_version_masks[source] = incoming_mask
            mask &= incoming_mask
            if not mask:
                break
        return mask

    def active_allowed_versions(
        self, requirement: Requirement
    ) -> tuple[int | None, frozenset[Version] | None]:
        domain = self.domains_internal.get(requirement.canonical_name)
        if domain is None:
            return None, None
        active = domain.constrained_requirements(self.apply_constraints)
        if len(active) < 2:
            return None, None
        mask = self.domain_version_mask(domain)
        versions = self.version_table(requirement)
        if mask is None or versions is None:
            return None, None
        key = requirement.canonical_name, mask
        allowed_versions = self.allowed_versions_cache.get(key)
        if allowed_versions is None:
            allowed_versions = frozenset(
                version for index, version in enumerate(versions) if mask & (1 << index)
            )
            self.allowed_versions_cache[key] = allowed_versions
        return mask, allowed_versions

    @staticmethod
    def candidate_incompatibility_key(
        candidate: WheelCandidate, extras: frozenset[str]
    ) -> tuple[int, frozenset[str]]:
        return id(candidate), extras

    def upgrade_allowed_for(self, name: str) -> bool:
        if not self.upgrade:
            return False
        if self.upgrade_strategy == "eager":
            return True
        return name in self.root_requirement_names

    def validate_candidate_policy(self, candidate: WheelCandidate) -> None:
        self.validate_requires_python(candidate)
        self.validate_external_url_dependencies(candidate)
        if candidate.yanked_reason is not None:
            reason = candidate.yanked_reason or "<none given>"
            print(
                f"WARNING: The candidate selected is a yanked version: {candidate.name}=={candidate.version}",
                file=sys.stderr,
            )
            print(f"Reason for being yanked: {reason}", file=sys.stderr)

    def validate_requires_python(self, candidate: WheelCandidate) -> None:
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

    def validate_external_url_dependencies(self, candidate: WheelCandidate) -> None:
        if not is_pypi_hosted_url(candidate.source_url):
            return
        for dependency in candidate.dependencies:
            if dependency.url is None or is_pypi_hosted_url(dependency.url):
                continue
            raise InstallationError(
                "Packages installed from PyPI cannot depend on packages "
                "which are not also hosted on PyPI.\n"
                f"{candidate.name} depends on {dependency}"
            )

    def validate_candidate_constraints(self, candidate: WheelCandidate) -> None:
        matching = [
            constraint
            for constraint in self.constraints_by_name.get(candidate.canonical_name, ())
            if marker_applies(constraint.marker, extras=())
        ]
        for constraint in matching:
            if not constraint.is_satisfied_by(
                candidate.version, allow_prereleases=True
            ):
                if candidate.source_kind in SOURCE_KINDS:
                    raise ResolutionError(
                        f"Cannot install {candidate.name} {candidate.version} "
                        "because it conflicts with a constraint."
                    )
                raise ResolutionError(
                    f"Cannot install {candidate.name} {candidate.version} because these "
                    "package versions have conflicting dependencies."
                )

    def warn_missing_candidate_extras(
        self, requirement: Requirement, candidate: WheelCandidate
    ) -> None:
        if requirement.url is not None and requirement.name.startswith("file://"):
            return
        self.warn_missing_extras(
            candidate.name,
            requirement.extras,
            candidate.provided_extras,
            version=str(candidate.version),
        )

    def warn_missing_installed_extras(
        self, requirement: Requirement, installed: InstalledDistribution
    ) -> None:
        provided = frozenset(
            canonicalize_name(value.strip())
            for value in installed.raw.metadata.get_all("Provides-Extra", [])
            if value.strip()
        )
        self.warn_missing_extras(
            requirement.name,
            requirement.extras,
            provided,
            version=installed.version,
        )

    def warn_missing_extras(
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
            if normalized in normalized_provided or key in self.warned_missing_extras:
                continue
            version_text = f" {version}" if version is not None else ""
            print(
                f"WARNING: {project_name}{version_text} "
                f"{self.does_not_provide_extra_text(extra)}",
                file=sys.stderr,
            )
            self.warned_missing_extras.add(key)

    @staticmethod
    def does_not_provide_extra_text(extra: str) -> str:
        return f"does not provide the extra '{extra}'"

    def no_matching_distribution_message(self, requirement: Requirement) -> str:
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

    def search_with_satisfied(
        self,
        requirement: Requirement,
        installed: InstalledDistribution,
        remaining: PendingAgenda,
        selected: dict[str, WheelCandidate],
        selected_extras: dict[str, frozenset[str]],
        satisfied: dict[str, SatisfiedRequirement],
        graph: dict[str, set[str]],
        *,
        source_requirements: dict[str, InstallRequirement],
        source_requirements_by_url: dict[str, InstallRequirement],
    ) -> SearchFrame:
        previous = satisfied.get(requirement.canonical_name)
        satisfied[requirement.canonical_name] = SatisfiedRequirement(
            requirement=requirement,
            distribution=installed,
        )
        dependency_pending: list[Requirement] = []
        if not self.no_deps:
            dependencies = installed.dependencies(requirement.extras)
            graph.setdefault(requirement.canonical_name, set())
            for dependency in sorted(
                dependencies,
                key=lambda item: item.canonical_name,
            ):
                graph[requirement.canonical_name].add(dependency.canonical_name)
                dependency_pending.insert(0, dependency)
        branch_checkpoint = remaining.checkpoint()
        remaining.prepend(dependency_pending)
        if (
            yield SearchRequest(
                remaining,
                selected,
                selected_extras,
                satisfied,
                graph,
                source_requirements,
                source_requirements_by_url,
                checkpoint=branch_checkpoint,
            )
        ):
            return True
        if previous is None:
            satisfied.pop(requirement.canonical_name, None)
        else:
            satisfied[requirement.canonical_name] = previous
        return False

    def satisfied_dependencies_are_consistent(
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
                        allow_prereleases=self.allow_prereleases_internal(dependency),
                    ):
                        return False
                    continue
                existing = satisfied.get(dependency.canonical_name)
                if existing is not None:
                    if not dependency.is_satisfied_by(
                        existing.distribution.version,
                        allow_prereleases=self.allow_prereleases_internal(dependency),
                    ):
                        return False
                    continue
                installed = self.find_installed_internal(dependency.name)
                if installed is None or not dependency.is_satisfied_by(
                    installed.version,
                    allow_prereleases=self.allow_prereleases_internal(dependency),
                ):
                    return False
        return True

    def find_installed_internal(self, name: str) -> InstalledDistribution | None:
        if self.installed_by_name_internal is None:
            self.installed_by_name_internal = {
                distribution.canonical_name: distribution
                for distribution in iter_installed_distributions()
            }
        return self.installed_by_name_internal.get(canonicalize_name(name))

    def candidate_with_extras(
        self,
        candidate: WheelCandidate,
        requirement: Requirement,
        extras: frozenset[str],
    ) -> WheelCandidate:
        if isinstance(candidate, LazyWheelCandidate):
            candidate = candidate.materialize()
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
        for extra_candidate in self.find_candidates_internal(extra_requirement):
            if extra_candidate.version == candidate.version:
                return extra_candidate
        return candidate

    def active_requirements_for(
        self,
        name: str,
        current: Requirement,
        remaining: Iterable[Requirement],
    ) -> list[Requirement]:
        relevant: list[Requirement] = [current]
        deferred: list[Requirement] = []
        for requirement in remaining:
            if requirement.canonical_name == name:
                relevant.append(requirement)
            else:
                deferred.append(requirement)
        domain = self.domains_internal.get(name)
        if domain is not None:
            relevant.extend(domain.requirements())
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

    def add_candidate_dependencies(
        self, source: str, candidate: WheelCandidate
    ) -> None:
        dependencies_by_name: dict[str, list[Requirement]] = {}
        for dependency in candidate.dependencies:
            target = dependency.canonical_name
            if target == source:
                continue
            dependencies_by_name.setdefault(target, []).append(dependency)
        for target, dependencies in dependencies_by_name.items():
            domain = self.domains_internal.setdefault(target, PackageDomain())
            domain.set_incoming(source, tuple(dependencies))
            self.incoming_requirements[target] = domain.incoming

    def remove_candidate_dependencies(
        self, source: str, candidate: WheelCandidate
    ) -> None:
        for target in {
            dependency.canonical_name
            for dependency in candidate.dependencies
            if dependency.canonical_name != source
        }:
            incoming = self.incoming_requirements.get(target)
            if incoming is None:
                continue
            domain = self.domains_internal[target]
            domain.remove_incoming(source)
            if not incoming:
                self.incoming_requirements.pop(target, None)
                if not domain.roots:
                    self.domains_internal.pop(target, None)

    def reconsideration_key(
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

    def emit_backtracking_message(self) -> None:
        self.backtrack_count += 1
        if self.backtrack_count in {1, 8}:
            print("This could take a while.", file=sys.stdout)
        if self.backtrack_count == 13:
            print("If you want to abort this run, press Ctrl + C.", file=sys.stdout)

    def apply_constraints(self, requirement: Requirement) -> Requirement:
        return self.constraint_store.apply(requirement)

    def choose_requirement(
        self,
        pending: PendingAgenda,
        selected: dict[str, WheelCandidate],
    ) -> tuple[int, Requirement]:
        if len(pending) == 1:
            return pending.first()
        if len(pending.by_name) >= 8:
            first_unresolved: tuple[int, Requirement] | None = None
            direct: tuple[int, Requirement] | None = None
            best: tuple[int, Requirement] | None = None
            best_score: tuple[int, int, int] | None = None
            for name, entry_ids in pending.by_name.items():
                if name in selected:
                    continue
                if len(entry_ids) == 1:
                    entry_id = next(iter(entry_ids))
                else:
                    entry_id = min(
                        entry_ids, key=lambda item: pending.entries_internal[item].order
                    )
                requirement = pending.entries_internal[entry_id].requirement
                if first_unresolved is None:
                    first_unresolved = entry_id, requirement
                if requirement.url is not None or looks_like_path_requirement(
                    requirement.raw
                ):
                    if direct is None or (
                        pending.entries_internal[entry_id].order
                        < pending.entries_internal[direct[0]].order
                    ):
                        direct = entry_id, requirement
                    continue
                domain = self.domains_internal.get(name)
                candidate_count = (
                    domain.decision_count
                    if domain is not None and domain.decision_count is not None
                    else self.decision_candidate_count(requirement)
                )
                score = (
                    candidate_count or 10**9,
                    -self.conflict_activity[self.package_id_internal(name)],
                    pending.entries_internal[entry_id].order,
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best = entry_id, requirement
            return direct or best or first_unresolved or pending.first()
        first_unresolved: tuple[int, Requirement] | None = None
        best: tuple[int, Requirement] | None = None
        best_score: tuple[int, int, int] | None = None
        queued_names: set[str] = set()
        for index, (entry_id, requirement) in enumerate(pending.iter_entries()):
            name = requirement.canonical_name
            if name in selected:
                continue
            if first_unresolved is None:
                first_unresolved = entry_id, requirement
            if requirement.url is not None or looks_like_path_requirement(
                requirement.raw
            ):
                return entry_id, requirement
            if name in queued_names:
                continue
            queued_names.add(name)
            domain = self.domains_internal.get(name)
            candidate_count = (
                domain.decision_count
                if domain is not None and domain.decision_count is not None
                else self.decision_candidate_count(requirement)
            )
            score = (
                candidate_count or 10**9,
                -self.conflict_activity[self.package_id_internal(name)],
                index,
            )
            if best_score is None or score < best_score:
                best = entry_id, requirement
                best_score = score
        return best or first_unresolved or pending.first()

    def decision_candidate_count(self, requirement: Requirement) -> int:
        domain = self.domains_internal.get(requirement.canonical_name)
        if domain is not None:
            if domain.decision_count is not None:
                return domain.decision_count
            active = domain.constrained_requirements(self.apply_constraints)
            if len(active) > 1:
                active_mask = self.domain_version_mask(domain)
                if active_mask is not None:
                    domain.decision_count = active_mask.bit_count()
                    return domain.decision_count
        key = id(requirement)
        cached = self.decision_count_cache.get(key)
        if cached is None:
            cached = self.candidate_count_internal(self.apply_constraints(requirement))
            self.decision_count_cache[key] = cached
        if domain is not None:
            domain.decision_count = cached
        return cached

    def candidate_count_internal(self, requirement: Requirement) -> int:
        allow_prereleases = self.allow_prereleases_internal(requirement)
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.marker,
            allow_prereleases,
        )
        cached = self.candidate_count_cache.get(key)
        if cached is not None:
            return cached
        exact_version = exact_pinned_version(requirement)
        if exact_version is not None:
            summaries = self.provider.available_versions_for(requirement, exact_version)
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
        else:
            summaries = self.provider.matching_versions(
                requirement,
                allow_prereleases=True,
            )
            count = (
                len(summaries)
                if allow_prereleases
                else sum(not summary.version.is_prerelease for summary in summaries)
            )
            if not count and not allow_prereleases:
                count = len(summaries)
        self.candidate_count_cache[key] = count
        return count

    def find_candidates_internal(
        self,
        requirement: Requirement,
        *,
        source_requirements: dict[str, InstallRequirement] | None = None,
        source_requirements_by_url: dict[str, InstallRequirement] | None = None,
    ) -> CandidateStream:
        active_mask, allowed_versions = self.active_allowed_versions(requirement)
        source_req = (
            source_requirements.get(requirement.canonical_name)
            if source_requirements is not None
            else None
        )
        if source_req is None and source_requirements_by_url is not None:
            source_req = source_requirements_by_url.get(requirement.url or "")
        source_hash_key = (
            tuple(
                sorted(
                    (algorithm, tuple(sorted(digests)))
                    for algorithm, digests in source_req.hash_options.items()
                )
            )
            if source_req is not None
            else ()
        )
        provider_hashes = self.provider.hashes_by_name.get(requirement.canonical_name)
        provider_hash_key = (
            tuple(
                sorted(
                    (algorithm, tuple(sorted(digests)))
                    for algorithm, digests in provider_hashes.allowed_internal.items()
                )
            )
            if provider_hashes is not None
            else ()
        )
        key = (
            *self.candidate_cache_key(requirement),
            active_mask,
            source_hash_key,
            provider_hash_key,
        )
        if key not in self.candidate_cache:
            logger.debug(
                f"candidate cache miss requirement={requirement.raw or requirement.name}"
            )
            if provider_hashes is not None and not provider_hashes.allowed_internal:
                if source_req is not None and "--hash" in str(source_req):
                    raise HashMismatch(
                        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE "
                        "REQUIREMENTS FILE."
                    )
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are "
                    "missing from some requirements."
                )
            # An empty active intersection is useful for conflict detection, but
            # must not discard every candidate before the resolver can explain
            # which requirement conflicts with the selected version.
            candidates = (
                self.provider.find_candidates(
                    requirement, allowed_versions=allowed_versions
                )
                if allowed_versions and requirement.url is None
                else self.provider.find_candidates(requirement)
            )
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
                    for algorithm, digests in provider_hashes.allowed_internal.items()
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
                    return value is not None and value in allowed_sha256

                def decisive(candidate: WheelCandidate) -> bool:
                    value = digest(candidate)
                    return value is not None and value in allowed_sha256

                if self.debug_internal:
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
                    candidates = CandidateStream(iter(materialized)).prefer(
                        keep, decisive=decisive
                    )
                candidates = candidates.prefer(keep, decisive=decisive)
            self.candidate_cache[key] = candidates
        else:
            logger.debug(
                f"candidate cache hit requirement={requirement.raw or requirement.name}"
            )
        candidates = self.candidate_cache[key]
        if source_req is not None and source_req.hash_options:
            allowed_sha256 = {
                digest.lower() for digest in source_req.hash_options.get("sha256", ())
            }

            def keep_hashed(candidate: WheelCandidate) -> bool:
                digest = (candidate.source_hashes or {}).get("sha256")
                return digest is not None and digest.lower() in allowed_sha256

            def decisive_hashed(candidate: WheelCandidate) -> bool:
                digest = (candidate.source_hashes or {}).get("sha256")
                return digest is not None and digest.lower() in allowed_sha256

            if allowed_sha256:
                candidates = candidates.prefer(keep_hashed, decisive=decisive_hashed)
        logger.debug(
            "candidate cache ready requirement=%s",
            requirement.raw or requirement.name,
        )
        if self.ignore_requires_python:
            return candidates
        return candidates.prefer(self.candidate_matches_python)

    def candidate_matches_python(self, candidate: WheelCandidate) -> bool:
        if not candidate.requires_python:
            return True
        try:
            return SpecifierSet(candidate.requires_python).contains(self.python_version)
        except ValueError:
            return True

    def allow_prereleases_internal(self, requirement: Requirement) -> bool:
        key = (
            requirement.canonical_name,
            str(requirement.specifier),
            requirement.url,
            requirement.raw,
        )
        cached = self.allow_prereleases_cache.get(key)
        if cached is not None:
            return cached
        controlled = self.provider.release_control
        if controlled is not None:
            value = controlled.allows_prereleases(requirement.name)
            if value is not None:
                self.allow_prereleases_cache[key] = value
                return value
        mentions_prerelease = any(
            spec.operator != "==="
            and not spec.version.endswith(".*")
            and spec.parsed_version.is_prerelease
            for spec in requirement.specifier.specifiers
        )
        result = (
            self.allow_prereleases
            or is_direct_requirement(requirement)
            or mentions_prerelease
        )
        self.allow_prereleases_cache[key] = result
        return result

    @staticmethod
    def candidate_cache_key(
        requirement: Requirement,
    ) -> tuple[str, str, tuple[str, ...], str | None, str]:
        return (
            requirement.canonical_name,
            str(requirement.specifier),
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.raw,
        )

    def preflight_hash_requirement(
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

    def validate_candidate_hashes(
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
            self.validate_link_hashes(requirement, candidate, source_req)
            return
        if source_req is None:
            if provider_hashes is not None and provider_hashes.allowed_internal:
                allowed = hash_sets(provider_hashes.allowed_internal)
                actual = actual_hashes_for_candidate(candidate)
                if hashes_match(allowed, actual):
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
        allowed_hashes = allowed_hashes_internal(source_req)
        if not allowed_hashes:
            if provider_hashes is not None and provider_hashes.allowed_internal:
                allowed_hashes = hash_sets(provider_hashes.allowed_internal)
        if (
            not allowed_hashes
            and source_req.link is not None
            and source_req.link.hashes
        ):
            allowed_hashes = hash_sets(source_req.link.hashes)
        if (
            not allowed_hashes
            and source_req.user_supplied
            and source_req.link is not None
            and source_req.link.url == candidate.source_url
            and candidate.source_hashes
        ):
            allowed_hashes = hash_sets(candidate.source_hashes)
        actual_hashes = actual_hashes_for_candidate(candidate)
        if not allowed_hashes:
            suggestion = ""
            sha256 = actual_hashes.get("sha256")
            if sha256:
                suggestion = f" --hash=sha256:{sha256}"
            raise HashMissing(
                "Hashes are required in --require-hashes mode, but they are missing "
                f"from some requirements. Missing hash for:\n    {source_req}{suggestion}"
            )
        if hashes_match(allowed_hashes, actual_hashes):
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

    def validate_link_hashes(
        self,
        requirement: Requirement,
        candidate: WheelCandidate,
        source_req: InstallRequirement | None,
    ) -> None:
        if not candidate.source_hashes:
            return
        if source_req is not None and allowed_hashes_internal(source_req):
            return
        if not is_direct_requirement(requirement):
            return
        actual_hashes = actual_hashes_for_candidate(candidate)
        if not actual_hashes or hashes_match(
            hash_sets(candidate.source_hashes), actual_hashes
        ):
            return
        expected_algorithm, expected_digests = next(
            iter(sorted(hash_sets(candidate.source_hashes).items()))
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

    def installation_order(
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


def best_candidate_internal(
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


def exact_pinned_version(requirement: Requirement) -> Version | None:
    return next(
        (
            specifier.parsed_version
            for specifier in requirement.specifier.specifiers
            if specifier.operator == "==" and not specifier.version.endswith(".*")
        ),
        None,
    )


def specifier_intersection_is_empty(requirements: Iterable[Requirement]) -> bool:
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    excluded: set[Version] = set()

    def tighten_lower(version: Version, inclusive: bool) -> None:
        nonlocal lower
        if lower is None or version > lower[0]:
            lower = (version, inclusive)
        elif version == lower[0]:
            lower = (version, lower[1] and inclusive)

    def tighten_upper(version: Version, inclusive: bool) -> None:
        nonlocal upper
        if upper is None or version < upper[0]:
            upper = (version, inclusive)
        elif version == upper[0]:
            upper = (version, upper[1] and inclusive)

    for requirement in requirements:
        for specifier in requirement.specifier.specifiers:
            if specifier.operator == "==" and not specifier.version.endswith(".*"):
                tighten_lower(specifier.parsed_version, True)
                tighten_upper(specifier.parsed_version, True)
            elif specifier.operator == ">=":
                tighten_lower(specifier.parsed_version, True)
            elif specifier.operator == ">":
                tighten_lower(specifier.parsed_version, False)
            elif specifier.operator == "<=":
                tighten_upper(specifier.parsed_version, True)
            elif specifier.operator == "<":
                tighten_upper(specifier.parsed_version, False)
            elif specifier.operator == "~=":
                tighten_lower(specifier.parsed_version, True)
                tighten_upper(specifier.compatible_upper_bound, False)
            elif specifier.operator == "!=" and not specifier.version.endswith(".*"):
                excluded.add(specifier.parsed_version)

    if lower is None or upper is None:
        return False
    if lower[0] > upper[0]:
        return True
    if lower[0] < upper[0]:
        return False
    return not (lower[1] and upper[1]) or lower[0] in excluded


def topological_weights(
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


def allowed_hashes_internal(requirement: InstallRequirement) -> dict[str, set[str]]:
    return {
        algorithm: set(digests)
        for algorithm, digests in requirement.hash_options.items()
        if digests
    }


def hash_sets(hashes: Mapping[str, str | list[str]]) -> dict[str, set[str]]:
    return {
        algorithm: {
            digest.lower() for digest in (value if isinstance(value, list) else [value])
        }
        for algorithm, value in hashes.items()
        if value
    }


def actual_hashes_for_candidate(candidate: WheelCandidate) -> dict[str, str]:
    if candidate.source_kind in SOURCE_KINDS and candidate.source_hashes:
        return dict(candidate.source_hashes)
    if candidate.source_url:
        parsed_url = urllib.parse.urlparse(candidate.source_url)
    else:
        parsed_url = None
    if parsed_url is not None and parsed_url.scheme == "file":
        try:
            path = Path(url_to_path(candidate.source_url or ""))
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


def hashes_match(
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


def is_direct_requirement(requirement: Requirement) -> bool:
    raw = requirement.raw.strip()
    return (
        requirement.url is not None
        or raw.startswith((".", "/", "~"))
        or os.sep in raw
        or (os.altsep is not None and os.altsep in raw)
    )


def direct_urls_equivalent(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return first == second
    first_parts = urllib.parse.urlsplit(first)
    second_parts = urllib.parse.urlsplit(second)

    def local_path(parts: urllib.parse.SplitResult, original: str) -> Path | None:
        if parts.scheme.lower() == "file":
            return Path(url_to_path(original)).resolve()
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
        if (
            parts.scheme.lower() == "file"
            and parts.netloc.lower() in LOCAL_FILE_NETLOCS
        ):
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


def is_pypi_hosted_url(url: str | None) -> bool:
    if not url:
        return False
    if url.partition(":")[0].casefold() not in HTTP_URL_SCHEMES:
        return False
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return host in PYPI_HOSTS
