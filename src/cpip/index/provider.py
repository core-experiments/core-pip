from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import stat
import time
import urllib.parse
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from types import MappingProxyType

from cpip.core.errors import InstallationError
from cpip.core.hashes import Hashes
from cpip.core.packaging import Requirement
from cpip.core.versions import Version
from cpip.core.release_control import ReleaseControl
from cpip.core.urls import path_to_url, url_to_path
from cpip.core.wheel import supported_wheel_tags, wheel_tag_rank
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.candidate_materialization import CandidateMaterializer
from cpip.index.candidates import InstallationCandidate
from cpip.index.catalog_cache import (
    RECORD_REQUIRES_PYTHON,
    RECORD_YANKED,
    SDIST_RECORD,
    WHEEL_RECORD,
    link_from_record,
    load_catalog,
    load_choices,
    save_choices,
    wheel_file_from_record,
)
from cpip.index.config import DEFAULT_INDEX_URL
from cpip.index.links import Link
from cpip.index.prefetch import Prefetcher, PrefetchPolicy
from cpip.index.source_locations import (
    FindLinksSource,
    SimpleIndexSource,
    is_remote_source_location,
    looks_like_path_requirement,
)
from cpip.index.source_models import (
    INSTALLABLE_ARTIFACT_KINDS,
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateRecord,
    CandidateSelection,
    CandidateSummary,
    PackageCatalog,
    PackageSource,
    RejectedCandidate,
    RejectionReason,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from cpip.core.format_control import FormatControl
    from cpip.core.wheel import TargetContext, WheelFile
    from cpip.index.candidate_materialization import CandidateStream


PYPI_HOSTS = frozenset(("pypi.org", "pypi.python.org"))

_CATALOG_WORKERS = 32

_CATALOG_METADATA_PREFETCH = 2


CatalogSummaryGroup = tuple[
    str,
    str,
    tuple[object, ...],
    list[tuple[int, str | None, str | None]],
]

CatalogSourceSummary = tuple[list[CatalogSummaryGroup], str, str]


def is_unnamed_direct_requirement_internal(requirement: Requirement) -> bool:
    return requirement.url is not None or looks_like_path_requirement(requirement.raw)


class CandidateProvider:
    def __init__(
        self,
        sources: tuple[PackageSource, ...],
        find_links: list[str],
        index_urls: list[str],
        no_index: bool = False,
        allow_yanked: bool = False,
        release_control: ReleaseControl | None = None,
        format_control: FormatControl | None = None,
        prefer_binary: bool = False,
        target: TargetContext | None = None,
        build_options: dict[str, dict[str, object]] | None = None,
        build_constraints: list[str] | None = None,
        wheel_cache_dir: str | os.PathLike[str] | None = None,
        trusted_hosts: tuple[str, ...] = (),
        build_isolation: bool = True,
        dry_run: bool = False,
        locked_links: dict[str, Link] | None = None,
        session: Any = None,
        uploaded_prior_to: datetime.datetime | None = None,
        compute_source_hashes: bool = False,
        hashes_by_name: dict[str, Hashes] | None = None,
    ) -> None:
        self.sources = sources

        self.find_links = find_links

        self.index_urls = index_urls

        self.no_index = no_index

        self.prefetch_remote_sources = bool(index_urls) or any(
            is_remote_source_location(value) for value in find_links
        )

        self.allow_yanked = allow_yanked

        self.release_control = release_control

        self.format_control = format_control

        self.prefer_binary = prefer_binary

        self.target = target

        self.build_options = build_options

        self.build_constraints = build_constraints

        self.wheel_cache_dir = wheel_cache_dir

        self.trusted_hosts = trusted_hosts

        self.build_isolation = build_isolation

        self.dry_run = dry_run

        self.locked_links = locked_links if locked_links is not None else {}

        self.session = session

        self.uploaded_prior_to = uploaded_prior_to

        self.compute_source_hashes = compute_source_hashes

        self.hashes_by_name = hashes_by_name if hashes_by_name is not None else {}

        self.last_rejected_requires_python: dict[str, str] = {}

        self.link_cache = {}

        self.find_links_cache = None

        self.find_links_by_name_cache = None

        self.parsed_link_cache = {}

        self.catalog_link_cache: dict[str, Link] = {}

        self.catalog_artifact_group_cache: dict[
            tuple[str, str],
            dict[str, list[tuple[int, tuple[object, ...]]]],
        ] = {}

        self.catalog_choice_cache: dict[
            tuple[str, str, str, bool, bool],
            dict[str, tuple[tuple[object, ...], int, int | None] | None],
        ] = {}

        self.catalog_groups_cache: dict[
            str,
            tuple[CatalogSourceSummary, ...],
        ] = {}

        self.catalog_supported_tags: tuple[Any, ...] | None = None

        self.catalog_target_key: str | None = None

        self.catalog_candidate_cache: dict[
            tuple[tuple[str, bool, bool], Version, bool],
            tuple[CandidateRecord, ...],
        ] = {}

        self.candidate_record_cache: dict[Link, CandidateRecord] = {}

        self.candidate_selection_cache = {}

        self.matching_versions_cache = {}

        self.package_catalog_cache = {}

        self.warm_catalog_cache: dict[tuple[str, bool, bool], bool] = {}

        self.prefetch_settled: set[tuple[str, bool, bool]] = set()

        self.candidate_work_cost_cache = {}

        self.cache_lock = RLock()

        self.prefetcher = None

        self.prefetch_policy = PrefetchPolicy()

        self.materializer_internal = None

        self.index_executor: ThreadPoolExecutor | None = None

        self.index_sources = tuple(
            source for source in sources if isinstance(source, SimpleIndexSource)
        )

    @classmethod
    def from_options(
        cls,
        *,
        find_links: list[str] | tuple[str, ...] = (),
        index_url: str | None = DEFAULT_INDEX_URL,
        extra_index_urls: list[str] | tuple[str, ...] = (),
        no_index: bool = False,
        format_control: FormatControl | None = None,
        prefer_binary: bool = False,
        target: TargetContext | None = None,
        build_options: dict[str, dict[str, object]] | None = None,
        build_constraints: list[str] | None = None,
        wheel_cache_dir: str | os.PathLike[str] | None = None,
        trusted_hosts: list[str] | tuple[str, ...] = (),
        build_isolation: bool = True,
        dry_run: bool = False,
        locked_links: dict[str, Link] | None = None,
        session: Any = None,
        uploaded_prior_to: datetime.datetime | None = None,
    ) -> CandidateProvider:
        normalized_find_links = list(find_links)

        normalized_index_urls = (
            [url for url in (index_url, *extra_index_urls) if url]
            if not no_index
            else []
        )

        sources: list[PackageSource] = []

        if normalized_find_links:
            sources.append(
                FindLinksSource(
                    tuple(normalized_find_links),
                    tuple(trusted_hosts),
                    session,
                ),
            )

        sources.extend(
            SimpleIndexSource(url, tuple(trusted_hosts), session)
            for url in normalized_index_urls
        )

        return cls(
            tuple(sources),
            normalized_find_links,
            normalized_index_urls,
            no_index,
            format_control=format_control,
            release_control=ReleaseControl(),
            prefer_binary=prefer_binary,
            target=target,
            build_options=build_options,
            build_constraints=build_constraints,
            wheel_cache_dir=os.fspath(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None,
            trusted_hosts=tuple(trusted_hosts),
            build_isolation=build_isolation,
            dry_run=dry_run,
            locked_links=dict(locked_links or {}),
            session=session,
            uploaded_prior_to=uploaded_prior_to,
        )

    def collect_links(self, requirement: Requirement) -> list[Link]:
        locked = self.locked_links.get(requirement.canonical_name)

        if locked is not None:
            return [locked]

        if requirement.url is not None or looks_like_path_requirement(requirement.raw):
            if requirement.url is not None:
                url = requirement.url
                if url.startswith("file://"):
                    try:
                        url = path_to_url(url_to_path(url))
                    except ValueError:
                        pass
                link = Link.from_url(url, source_url=None)
                if link.kind is ArtifactKind.UNKNOWN and url.startswith(
                    ("http://", "https://")
                ):
                    # Some archive endpoints (notably GitHub's /tarball/
                    # URLs) omit the archive suffix.  The downloader can
                    # determine the real filename from the response, but
                    # the link must be admitted as a source artifact first.
                    link.kind = ArtifactKind.SDIST
                return [link]

            path = requirement.raw

            try:
                path_stat = os.stat(path)

            except OSError:
                return []

            identity = (
                f"stat:{path_stat.st_dev}:{path_stat.st_ino}:"
                f"{path_stat.st_size}:{path_stat.st_mtime_ns}"
            )

            return [
                Link.from_path(
                    path,
                    source_url=None,
                    is_dir=stat.S_ISDIR(path_stat.st_mode),
                    local_identity=identity,
                ),
            ]

        links: list[Link] = []

        cache_key = requirement.canonical_name

        cached = self.link_cache.get(cache_key)

        if cached is not None:
            return list(cached)

        seen: set[str] = set()

        sources: list[PackageSource] = []

        if self.find_links:
            if self.find_links_cache is None:
                source = FindLinksSource(
                    tuple(self.find_links),
                    self.trusted_hosts,
                    self.session,
                )

                self.find_links_cache = tuple(source.collect_links(requirement))

            for link in self.find_links_cache:
                if link.url in seen:
                    continue

                seen.add(link.url)

                links.append(link)

        sources.extend(self.index_sources)

        for link_group in self.collect_index_links(requirement):
            for link in link_group:
                if link.url in seen:
                    continue

                seen.add(link.url)

                links.append(link)

        self.link_cache[cache_key] = tuple(links)

        return links

    def catalog_links(self, requirement: Requirement) -> tuple[Link, ...]:
        """Return project links without rescanning unrelated find-links entries."""

        if (
            requirement.canonical_name in self.locked_links
            or requirement.url is not None
            or looks_like_path_requirement(requirement.raw)
        ):
            return tuple(self.collect_links(requirement))

        cached_links = self.link_cache.get(requirement.canonical_name)

        if cached_links is not None:
            return cached_links

        if self.find_links_cache is None:
            source = FindLinksSource(
                tuple(self.find_links),
                self.trusted_hosts,
                self.session,
            )

            self.find_links_cache = tuple(source.collect_links(requirement))

        if self.find_links_by_name_cache is None:
            grouped: dict[str, list[Link]] = {}

            for link in self.find_links_cache:
                parsed = self.parsed_link_cache.get(link)

                if parsed is None:
                    try:
                        parsed = InstallationCandidate.from_link(
                            link,
                            target=self.target,
                        )

                    except ValueError:
                        continue

                    self.parsed_link_cache[link] = parsed

                if isinstance(parsed, InstallationCandidate):
                    name = parsed.canonical_name
                    bucket = grouped.get(name)
                    if bucket is None:
                        bucket = []
                        grouped[name] = bucket
                    bucket.append(link)

            self.find_links_by_name_cache = {
                name: tuple(links) for name, links in grouped.items()
            }

        links = list(self.find_links_by_name_cache.get(requirement.canonical_name, ()))

        seen = {link.url for link in links}

        for link_group in self.collect_index_links(requirement):
            for link in link_group:
                if link.url not in seen:
                    seen.add(link.url)

                    links.append(link)

        result = tuple(links)

        self.link_cache[requirement.canonical_name] = result

        return result

    def catalog_groups(
        self,
        requirement: Requirement,
    ) -> tuple[CatalogSourceSummary, ...] | None:
        if (
            self.find_links
            or requirement.url is not None
            or looks_like_path_requirement(requirement.raw)
            or not self.index_sources
        ):
            return None

        cached_result = self.catalog_groups_cache.get(requirement.canonical_name)

        if cached_result is not None:
            return cached_result

        result: list[CatalogSourceSummary] = []

        for source in self.index_sources:
            cached = source.collect_cached_catalog_summary(requirement)

            if cached is None:
                return None

            source_url = source.project_page_url(
                source.index_url,
                requirement.canonical_name,
            )

            generation, groups, _has_unparsed, choice_profiles = cached

            for profile_key, choices in choice_profiles.items():
                if (
                    not isinstance(profile_key, tuple)
                    or len(profile_key) != 3
                    or not isinstance(profile_key[0], str)
                    or not isinstance(profile_key[1], bool)
                    or not isinstance(profile_key[2], bool)
                    or not isinstance(choices, dict)
                ):
                    continue

                self.catalog_choice_cache[
                    (
                        source_url,
                        generation,
                        profile_key[0],
                        profile_key[1],
                        profile_key[2],
                    )
                ] = choices

            # Keep the source metadata at the source boundary. Flattening the

            # groups would allocate one new tuple per release just to repeat

            # these two values tens of thousands of times.

            result.append((groups, source_url, generation))

        cached_result = tuple(result)

        self.catalog_groups_cache[requirement.canonical_name] = cached_result

        return cached_result

    def catalog_target_internal(self) -> tuple[tuple[Any, ...], str]:
        """Return the immutable target tags and their persistent cache key."""

        supported_tags = self.catalog_supported_tags

        target_key = self.catalog_target_key

        if supported_tags is None or target_key is None:
            supported_tags = tuple(supported_wheel_tags(self.target))

            target_key = hashlib.sha256(
                "\0".join(str(tag) for tag in supported_tags).encode(),
            ).hexdigest()

            self.catalog_supported_tags = supported_tags

            self.catalog_target_key = target_key

        return supported_tags, target_key

    def link_from_catalog_record(
        self,
        record: tuple[object, ...],
        source_url: str,
    ) -> Link:
        url = record[0]

        if isinstance(url, str):
            cached = self.catalog_link_cache.get(url)

            if cached is not None:
                return cached

        link = link_from_record(record, source_url=source_url)

        if isinstance(url, str):
            self.catalog_link_cache[url] = link

        return link

    def _catalog_choices_for(
        self,
        catalog_key: tuple[str, bool, bool],
        target_key: str,
        persistent_cache: Any,
        source_url: str,
        generation: str,
    ) -> dict[str, tuple[tuple[object, ...], int, int | None] | None]:
        key = (
            source_url,
            generation,
            target_key,
            catalog_key[1],
            catalog_key[2],
        )

        choices = self.catalog_choice_cache.get(key)

        if choices is None:
            choices = load_choices(
                persistent_cache,
                source_url,
                generation,
                target_key,
                catalog_key[1],
                catalog_key[2],
            )

            self.catalog_choice_cache[key] = choices

        return choices

    def _catalog_artifacts_for(
        self,
        catalog_key: tuple[str, bool, bool],
        persistent_cache: Any,
        source_url: str,
        generation: str,
        version: Version,
    ) -> list[tuple[int, tuple[object, ...]]]:
        key = (source_url, generation)

        groups = self.catalog_artifact_group_cache.get(key)

        if groups is None:
            groups = {}

            loaded = load_catalog(persistent_cache, source_url)

            if loaded is not None:
                for name, version_text, artifacts, _facts in loaded[0]:
                    if name == catalog_key[0]:
                        existing = groups.get(version_text)
                        if existing is None:
                            groups[version_text] = list(artifacts)
                        else:
                            existing.extend(artifacts)

            self.catalog_artifact_group_cache[key] = groups

        return groups.get(version.public, [])

    @staticmethod
    def _eligible_catalog_records(
        catalog_key: tuple[str, bool, bool],
        artifacts: list[tuple[int, tuple[object, ...]]],
    ) -> Iterator[tuple[tuple[object, ...], int]]:
        for record_kind, record in artifacts:
            if record_kind == WHEEL_RECORD:
                if not catalog_key[1]:
                    continue

            elif record_kind == SDIST_RECORD:
                if not catalog_key[2]:
                    continue

            else:
                continue

            requires_python = record[RECORD_REQUIRES_PYTHON]

            if isinstance(requires_python, str):
                try:
                    if not CandidateEvaluator.requires_python_matches(
                        requires_python,
                    ):
                        continue

                except ValueError:
                    continue

            yield record, record_kind

    @staticmethod
    def _parse_catalog_descriptor(
        catalog_key: tuple[str, bool, bool],
        choice: tuple[tuple[object, ...], int, int | None],
        source_url: str,
        version: Version,
    ) -> tuple[tuple[object, ...], WheelFile | None, int | None, str] | None:
        record, record_kind, tag_rank = choice

        if record_kind == WHEEL_RECORD:
            parsed_wheel = wheel_file_from_record(
                record,
                name=catalog_key[0],
                version=version,
            )

            if parsed_wheel is None:
                return None

        elif record_kind == SDIST_RECORD:
            parsed_wheel = None

            tag_rank = None

        else:
            return None

        return record, parsed_wheel, tag_rank, source_url

    def _select_catalog_choice(
        self,
        catalog_key: tuple[str, bool, bool],
        supported_tags: tuple[Any, ...],
        artifacts: list[tuple[int, tuple[object, ...]]],
        version: Version,
    ) -> tuple[tuple[object, ...], int, int | None] | None:
        best: tuple[tuple[object, ...], int, int | None] | None = None

        best_rank: tuple[int, int, int] | None = None

        unsupported: tuple[tuple[object, ...], int, int | None] | None = None

        for record, record_kind in self._eligible_catalog_records(
            catalog_key,
            artifacts,
        ):
            if record_kind == WHEEL_RECORD:
                parsed_wheel = wheel_file_from_record(
                    record,
                    name=catalog_key[0],
                    version=version,
                )

                if parsed_wheel is None:
                    continue

                tag_rank = wheel_tag_rank(parsed_wheel.tags, supported_tags)

                if tag_rank is None:
                    if unsupported is None:
                        unsupported = record, record_kind, None

                    continue

            else:
                tag_rank = None

            yanked = record[RECORD_YANKED]

            rank = (
                int(yanked is None),
                int(record_kind == WHEEL_RECORD),
                -(tag_rank if tag_rank is not None else 1_000_000),
            )

            if best_rank is None or rank > best_rank:
                best_rank = rank

                best = record, record_kind, tag_rank

        return best if best is not None else unsupported

    def _materialize_catalog_descriptor(
        self,
        catalog_key: tuple[str, bool, bool],
        descriptor: tuple[
            tuple[object, ...],
            WheelFile | None,
            int | None,
            str,
        ],
        version: Version,
        destination: list[CandidateRecord],
    ) -> None:
        record, parsed_wheel, tag_rank, source_url = descriptor

        try:
            link = self.link_from_catalog_record(record, source_url)

        except ValueError:
            return

        candidate = CandidateRecord(
            name=catalog_key[0],
            version=version,
            link=link,
            wheel=parsed_wheel,
            tag_rank=tag_rank,
        )

        self.candidate_record_cache.setdefault(link, candidate)

        destination.append(candidate)

    def candidate_records_from_catalog(
        self,
        catalog_key: tuple[str, bool, bool],
        catalog: PackageCatalog,
        versions: tuple[Version, ...],
        *,
        primary_only: bool = False,
    ) -> tuple[CandidateRecord, ...]:
        """Materialize cached artifact records only for requested releases."""

        records_by_version = catalog.records_by_version

        if records_by_version is None:
            return tuple(
                candidate
                for version in versions
                for candidate in catalog.candidates_by_version.get(version, ())
            )

        supported_tags, target_key = self.catalog_target_internal()

        persistent_cache = getattr(self.session, "cache", None)

        dirty_choices: set[tuple[str, str, str, bool, bool]] = set()

        result: list[CandidateRecord] = []

        for version in dict.fromkeys(versions):
            candidate_key = (catalog_key, version, primary_only)

            candidates = self.catalog_candidate_cache.get(candidate_key)

            if candidates is None:
                materialized: list[CandidateRecord] = []

                unsupported: tuple[tuple[object, ...], WheelFile, str] | None = None

                has_supported_wheel = False

                best_descriptor: (
                    tuple[tuple[object, ...], WheelFile | None, int | None, str] | None
                ) = None

                best_rank: tuple[int, int, int] | None = None

                entries = records_by_version.get(version, ())

                for entry in entries:
                    if not isinstance(entry, tuple) or len(entry) != 2:
                        continue

                    source_url = entry[0]

                    generation = entry[1]

                    if not isinstance(source_url, str) or not isinstance(
                        generation,
                        str,
                    ):
                        continue

                    if primary_only:
                        choices = self._catalog_choices_for(
                            catalog_key,
                            target_key,
                            persistent_cache,
                            source_url,
                            generation,
                        )

                        version_text = version.public

                        if version_text not in choices:
                            choices[version_text] = self._select_catalog_choice(
                                catalog_key,
                                supported_tags,
                                self._catalog_artifacts_for(
                                    catalog_key,
                                    persistent_cache,
                                    source_url,
                                    generation,
                                    version,
                                ),
                                version,
                            )

                            dirty_choices.add(
                                (
                                    source_url,
                                    generation,
                                    target_key,
                                    catalog_key[1],
                                    catalog_key[2],
                                ),
                            )

                        choice = choices[version_text]

                        if choice is None:
                            continue

                        descriptor = self._parse_catalog_descriptor(
                            catalog_key,
                            choice,
                            source_url,
                            version,
                        )

                        if descriptor is None:
                            continue

                        parsed_wheel = descriptor[1]

                        tag_rank = descriptor[2]

                        if parsed_wheel is not None and tag_rank is None:
                            if unsupported is None:
                                unsupported = (
                                    descriptor[0],
                                    parsed_wheel,
                                    source_url,
                                )

                            continue

                        yanked = descriptor[0][RECORD_YANKED]

                        rank = (
                            int(yanked is None),
                            int(parsed_wheel is not None),
                            -(tag_rank if tag_rank is not None else 1_000_000),
                        )

                        if best_rank is None or rank > best_rank:
                            best_rank = rank

                            best_descriptor = descriptor

                        continue

                    for record, record_kind in self._eligible_catalog_records(
                        catalog_key,
                        self._catalog_artifacts_for(
                            catalog_key,
                            persistent_cache,
                            source_url,
                            generation,
                            version,
                        ),
                    ):
                        if record_kind == WHEEL_RECORD:
                            descriptor = self._parse_catalog_descriptor(
                                catalog_key,
                                (record, record_kind, None),
                                source_url,
                                version,
                            )

                            if descriptor is None or descriptor[1] is None:
                                continue

                            tag_rank = wheel_tag_rank(
                                descriptor[1].tags,
                                supported_tags,
                            )

                            if tag_rank is None:
                                if unsupported is None:
                                    unsupported = (
                                        descriptor[0],
                                        descriptor[1],
                                        source_url,
                                    )

                                continue

                            has_supported_wheel = True

                            descriptor = (
                                descriptor[0],
                                descriptor[1],
                                tag_rank,
                                source_url,
                            )

                        else:
                            descriptor = (record, None, None, source_url)

                        self._materialize_catalog_descriptor(
                            catalog_key,
                            descriptor,
                            version,
                            materialized,
                        )

                if primary_only and best_descriptor is not None:
                    self._materialize_catalog_descriptor(
                        catalog_key,
                        best_descriptor,
                        version,
                        materialized,
                    )

                elif primary_only and unsupported is not None:
                    record, parsed_wheel, source_url = unsupported

                    self._materialize_catalog_descriptor(
                        catalog_key,
                        (record, parsed_wheel, None, source_url),
                        version,
                        materialized,
                    )

                elif unsupported is not None and not has_supported_wheel:
                    record, parsed_wheel, source_url = unsupported

                    self._materialize_catalog_descriptor(
                        catalog_key,
                        (record, parsed_wheel, None, source_url),
                        version,
                        materialized,
                    )

                candidates = tuple(materialized)

                self.catalog_candidate_cache[candidate_key] = candidates

            result.extend(candidates)

        for (
            source_url,
            generation,
            choice_target,
            allow_binary,
            allow_source,
        ) in dirty_choices:
            choices = self.catalog_choice_cache[
                (
                    source_url,
                    generation,
                    choice_target,
                    allow_binary,
                    allow_source,
                )
            ]

            save_choices(
                persistent_cache,
                source_url,
                generation,
                choice_target,
                allow_binary,
                allow_source,
                choices,
            )

        return tuple(result)

    @staticmethod
    def evaluate_catalog_candidate(
        candidate: CandidateRecord,
        requirement: Requirement,
        *,
        allow_yanked: bool,
        allow_binary: bool,
        allow_source: bool,
    ) -> CandidateRecord | RejectedCandidate:
        link = candidate.link

        if link.kind is ArtifactKind.WHEEL and not allow_binary:
            return RejectedCandidate(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "binary distributions are disabled",
            )

        if link.kind in SOURCE_ARTIFACT_KINDS and not allow_source:
            return RejectedCandidate(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "source distributions are disabled",
            )

        if link.is_yanked and not (
            allow_yanked
            or any(
                spec.operator in {"==", "==="} and not spec.version.endswith(".*")
                for spec in requirement.specifier.specifiers
            )
        ):
            return RejectedCandidate(
                link,
                RejectionReason.YANKED,
                link.yanked_reason or "yanked",
            )

        if link.kind is ArtifactKind.WHEEL and candidate.tag_rank is None:
            return RejectedCandidate(
                link,
                RejectionReason.UNSUPPORTED_WHEEL,
                "wheel tags are not supported by this interpreter",
            )

        return candidate

    def collect_index_links(self, requirement: Requirement) -> tuple[list[Link], ...]:
        """Fetch configured index pages concurrently, preserving source order."""

        if len(self.index_sources) <= 1 or self.session is None:
            return tuple(
                source.collect_links(requirement) for source in self.index_sources
            )

        if self.index_executor is None:
            self.index_executor = ThreadPoolExecutor(
                max_workers=min(8, len(self.index_sources)),
            )

        return tuple(
            self.index_executor.map(
                lambda source: source.collect_links(requirement),
                self.index_sources,
            ),
        )

    def evaluate_links(
        self,
        requirement: Requirement,
        *,
        allowed_versions: frozenset[Version] | None = None,
        primary_only: bool = False,
    ) -> CandidateSelection:
        accepted: list[CandidateRecord] = []

        rejected: list[RejectedCandidate] = []

        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        allowed_version_key = (
            ()
            if not allowed_versions
            else tuple(sorted(version.public for version in allowed_versions))
        )

        selection_key = (
            requirement.canonical_name,
            requirement.specifier.text,
            requirement.extras,
            requirement.url,
            requirement.marker,
            requirement.raw,
            allow_binary,
            allow_source,
            self.allow_yanked,
            self.prefer_binary,
            self.uploaded_prior_to,
            primary_only,
            allowed_version_key,
        )

        cached_selection = self.candidate_selection_cache.get(selection_key)

        if cached_selection is not None:
            return cached_selection

        catalog_key = (
            requirement.canonical_name,
            allow_binary,
            allow_source,
        )

        links: tuple[Link, ...] | None = None

        catalog_candidates: tuple[CandidateRecord, ...] | None = None

        exact_version = self.exact_version_internal(requirement)

        catalog = self.package_catalog_cache.get(catalog_key)

        if (
            catalog is None
            and requirement.url is None
            and not self.find_links
            and self.session is not None
            and self.index_sources
        ):
            self.available_versions(requirement)

            catalog = self.package_catalog_cache.get(catalog_key)

        if (
            catalog is None
            and requirement.url is None
            and self.prefetcher is not None
            and self.prefetcher.pending(catalog_key)
        ):
            self.available_versions(requirement)

            catalog = self.package_catalog_cache.get(catalog_key)

        if (
            requirement.url is None
            and exact_version is not None
            and catalog is not None
        ):
            if catalog.records_by_version is not None:
                links = ()

                catalog_candidates = self.candidate_records_from_catalog(
                    catalog_key,
                    catalog,
                    (exact_version,),
                    primary_only=primary_only,
                )

            else:
                links = catalog.links_by_version.get(exact_version, ())

                catalog_candidates = catalog.candidates_by_version.get(
                    exact_version,
                    (),
                )

        elif requirement.url is None and catalog is not None:
            matching_versions = self.matching_versions(
                requirement,
                allow_prereleases=True,
            )

            if allowed_versions is not None:
                matching_versions = tuple(
                    summary
                    for summary in matching_versions
                    if summary.version in allowed_versions
                )

            if catalog.records_by_version is not None:
                links = ()

                catalog_candidates = self.candidate_records_from_catalog(
                    catalog_key,
                    catalog,
                    tuple(summary.version for summary in matching_versions),
                    primary_only=primary_only,
                )

            else:
                matching_links = tuple(
                    link
                    for summary in matching_versions
                    for link in catalog.links_by_version.get(summary.version, ())
                )

            if catalog.records_by_version is None and matching_links:
                links = tuple(dict.fromkeys(matching_links))

                catalog_candidates = tuple(
                    candidate
                    for summary in matching_versions
                    for candidate in catalog.candidates_by_version.get(
                        summary.version,
                        (),
                    )
                )

        if links is None:
            links = self.catalog_links(requirement)

        if (
            requirement.url is None
            and links
            and all(
                link.is_file
                and link.kind is ArtifactKind.WHEEL
                and not link.requires_python
                and not link.is_yanked
                for link in links
            )
        ):
            accepted: list[CandidateRecord] = []

            for link in links:
                parsed = self.parsed_link_cache.get(link)

                if parsed is None:
                    try:
                        parsed = InstallationCandidate.from_link(
                            link,
                            target=self.target,
                        )

                    except ValueError:
                        continue

                    self.parsed_link_cache[link] = parsed

                if isinstance(parsed, InstallationCandidate):
                    candidate = self.candidate_record_cache.get(link)

                    if candidate is None:
                        candidate = parsed.to_record()

                        self.candidate_record_cache[link] = candidate

                    parsed = candidate

                if (
                    allowed_versions is not None
                    and isinstance(parsed, CandidateRecord)
                    and parsed.version not in allowed_versions
                ):
                    continue

                result = CandidateEvaluator.evaluate_parsed_link(
                    link,
                    parsed,
                    requirement,
                    allow_yanked=self.allow_yanked,
                    allow_binary=allow_binary,
                    allow_source=allow_source,
                )

                if isinstance(result, CandidateRecord):
                    accepted.append(result)

                else:
                    rejected.append(result)

            accepted.sort(
                key=lambda candidate: candidate.sort_key(
                    prefer_binary=self.prefer_binary,
                ),
                reverse=True,
            )

            selection = CandidateSelection(tuple(accepted), tuple(rejected))

            self.candidate_selection_cache[selection_key] = selection

            return selection

        cached_catalog_candidates = (
            catalog is not None and catalog.records_by_version is not None
        )

        candidate_items: tuple[Link | CandidateRecord, ...] = (
            catalog_candidates if catalog_candidates is not None else tuple(links)
        )

        for item in candidate_items:
            if (
                cached_catalog_candidates
                and self.uploaded_prior_to is None
                and isinstance(item, CandidateRecord)
            ):
                if (
                    allowed_versions is not None
                    and item.version not in allowed_versions
                ):
                    continue

                result = self.evaluate_catalog_candidate(
                    item,
                    requirement,
                    allow_yanked=self.allow_yanked,
                    allow_binary=allow_binary,
                    allow_source=allow_source,
                )

                if isinstance(result, CandidateRecord):
                    accepted.append(result)

                else:
                    rejected.append(result)

                continue

            if isinstance(item, CandidateRecord):
                link = item.link

                parsed: CandidateRecord | RejectedCandidate = item

            elif isinstance(item, RejectedCandidate):
                link = item.link

                parsed = item

                rejected.append(item)

                continue

            else:
                link = item

                parsed = self.parsed_link_cache.get(link)

            if (
                allowed_versions is not None
                and isinstance(parsed, CandidateRecord)
                and parsed.version not in allowed_versions
            ):
                continue

            if self.uploaded_prior_to is not None:
                # Upload timestamps describe index-hosted artifacts. Local

                # files, directories, and VCS checkouts are already under the

                # user's control and must not be rejected by this filter.

                if link.is_file or link.is_existing_dir or link.is_vcs:
                    pass

                elif link.upload_time is None or (
                    link.upload_time.replace(tzinfo=datetime.timezone.utc)
                    if link.upload_time.tzinfo is None
                    else link.upload_time
                ) >= (
                    self.uploaded_prior_to.replace(tzinfo=datetime.timezone.utc)
                    if self.uploaded_prior_to.tzinfo is None
                    else self.uploaded_prior_to
                ):
                    host = urllib.parse.urlparse(link.source_url or "").hostname

                    cutoff = self.uploaded_prior_to

                    if cutoff.tzinfo is None:
                        cutoff = cutoff.replace(tzinfo=datetime.timezone.utc)

                    if (
                        link.upload_time is None
                        and host in PYPI_HOSTS
                        and cutoff > datetime.datetime.now(datetime.timezone.utc)
                    ):
                        continue

                    rejected.append(
                        RejectedCandidate(
                            link,
                            RejectionReason.MISSING_ARTIFACT,
                            "does not provide upload-time metadata before the cutoff",
                        ),
                    )

                    continue

            if parsed is None:
                try:
                    parsed = InstallationCandidate.from_link(link, target=self.target)

                except ValueError:
                    rejected.append(
                        RejectedCandidate(
                            link,
                            RejectionReason.INVALID_VERSION,
                            "could not parse project and version",
                        ),
                    )

                    continue

                if not CandidateEvaluator.is_unnamed_direct_requirement(requirement):
                    self.parsed_link_cache[link] = parsed

            if isinstance(parsed, InstallationCandidate):
                candidate = self.candidate_record_cache.get(link)

                if candidate is None:
                    candidate = parsed.to_record()

                    self.candidate_record_cache[link] = candidate

                parsed = candidate

            if (
                allowed_versions is not None
                and isinstance(parsed, CandidateRecord)
                and parsed.version not in allowed_versions
            ):
                continue

            result = (
                self.evaluate_catalog_candidate(
                    parsed,
                    requirement,
                    allow_yanked=self.allow_yanked,
                    allow_binary=allow_binary,
                    allow_source=allow_source,
                )
                if cached_catalog_candidates and isinstance(parsed, CandidateRecord)
                else CandidateEvaluator.evaluate_parsed_link(
                    link,
                    parsed,
                    requirement,
                    allow_yanked=self.allow_yanked,
                    allow_binary=allow_binary,
                    allow_source=allow_source,
                )
            )

            if isinstance(result, CandidateRecord):
                accepted.append(result)

            else:
                rejected.append(result)

        accepted.sort(
            key=lambda candidate: candidate.sort_key(prefer_binary=self.prefer_binary),
            reverse=True,
        )

        selection = CandidateSelection(tuple(accepted), tuple(rejected))

        self.candidate_selection_cache[selection_key] = selection

        return selection

    def applicable_candidate_records(
        self,
        requirement: Requirement,
        *,
        allowed_versions: frozenset[Version] | None = None,
    ) -> tuple[CandidateRecord, ...]:
        """Return policy-filtered candidate records without loading metadata."""

        hashes = self.hashes_by_name.get(requirement.canonical_name)

        selection = self.evaluate_links(
            requirement,
            allowed_versions=allowed_versions,
            primary_only=(
                self.uploaded_prior_to is None
                and not (hashes is not None and hashes.allowed_internal)
            ),
        )

        accepted = selection.accepted

        for rejected in selection.rejected:
            if rejected.link.requires_python:
                self.last_rejected_requires_python[requirement.canonical_name] = (
                    rejected.link.requires_python
                )

        if (
            not accepted
            and requirement.url is not None
            and selection.rejected
            and selection.rejected[0].link.is_vcs
        ):
            raise InstallationError(selection.rejected[0].detail)

        if not accepted and selection.rejected:
            upload_rejection = next(
                (
                    rejected
                    for rejected in selection.rejected
                    if rejected.reason is RejectionReason.MISSING_ARTIFACT
                ),
                None,
            )

            if upload_rejection is not None:
                host = urllib.parse.urlparse(
                    upload_rejection.link.source_url or "",
                ).hostname

                if host not in PYPI_HOSTS:
                    raise InstallationError(upload_rejection.detail)

        if requirement.url is None and any(
            candidate.version.is_prerelease for candidate in accepted
        ):
            accepted = tuple(
                CandidateEvaluator.create(
                    requirement.name,
                    release_control=self.release_control,
                    prefer_binary=self.prefer_binary,
                    specifier=requirement.specifier,
                    target=self.target,
                    hashes=None,
                ).get_applicable_candidates(list(accepted)),
            )

        if hashes is not None and hashes.allowed_internal:
            allowed = {
                digest.lower()
                for digests in hashes.allowed_internal.values()
                for digest in digests
            }

            matching = tuple(
                candidate
                for candidate in accepted
                if not candidate.link.hashes
                or any(
                    digest.lower() in allowed
                    for digest in candidate.link.hashes.values()
                )
            )

            if matching and len(matching) != len(accepted):
                accepted = matching

        accepted = tuple(self.deduplicate_candidates(list(accepted)))

        preferred = self.best_accepted_candidates(accepted)

        preferred_set = set(preferred)

        ordered = preferred + tuple(
            candidate for candidate in accepted if candidate not in preferred_set
        )

        return ordered

    @staticmethod
    def catalog_summary_bounds(
        groups: Sequence[CatalogSummaryGroup],
        requirement: Requirement,
    ) -> tuple[int, int]:
        """Locate a specifier's contiguous range in a sorted catalog summary."""

        def bound(key: Any, *, right: bool) -> int:
            low = 0

            high = len(groups)

            while low < high:
                middle = (low + high) // 2

                state = groups[middle][2]

                if not isinstance(state, tuple) or len(state) != 8:
                    return 0 if not right else len(groups)

                candidate_key: Any = state[7]

                if candidate_key < key or (right and candidate_key == key):
                    low = middle + 1

                else:
                    high = middle

            return low

        lower, upper = requirement.specifier.bounds()

        start = 0

        stop = len(groups)

        if lower is not None:
            start = bound(lower[0].key_internal(), right=not lower[1])

        if upper is not None:
            stop = bound(upper[0].key_internal(), right=upper[1])

        return min(start, stop), stop

    def lazy_summary_records(
        self,
        requirement: Requirement,
        groups: Sequence[CatalogSummaryGroup],
        source_url: str,
        generation: str,
        *,
        allowed_versions: frozenset[Version] | None,
    ) -> Iterator[CandidateRecord] | None:
        """Select from a compiled one-index summary without rebuilding a catalog.



        The target-choice profile is generation scoped and contains the same

        artifact decision made by the full catalog evaluator.  When it covers

        the requested range, the resolver only restores versions that can

        actually participate in this requirement.  An incomplete profile

        delegates to the full path, which fills the missing choices for later

        warm resolutions.

        """

        if len(self.index_sources) != 1 or not groups:
            return None

        _supported_tags, target_key = self.catalog_target_internal()

        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        cache_identity = (
            source_url,
            generation,
            target_key,
            allow_binary,
            allow_source,
        )

        choices = self.catalog_choice_cache.get(cache_identity)

        if choices is None:
            choices = load_choices(
                getattr(self.session, "cache", None),
                source_url,
                generation,
                target_key,
                allow_binary,
                allow_source,
            )

            self.catalog_choice_cache[cache_identity] = choices

        if not choices:
            return None

        start, stop = self.catalog_summary_bounds(groups, requirement)

        descriptor_buckets: tuple[
            list[tuple[Version, tuple[object, ...], int, int | None, str]],
            list[tuple[Version, tuple[object, ...], int, int | None, str]],
            list[tuple[Version, tuple[object, ...], int, int | None, str]],
            list[tuple[Version, tuple[object, ...], int, int | None, str]],
        ] = ([], [], [], [])

        seen_versions: set[Version] = set()

        bounded_only = all(
            specifier.operator in {"==", ">=", ">", "<=", "<", "~="}
            and not specifier.version.endswith(".*")
            for specifier in requirement.specifier.specifiers
        )

        exact_pin = any(
            specifier.operator in {"==", "==="} and not specifier.version.endswith(".*")
            for specifier in requirement.specifier.specifiers
        )

        for group in groups[start:stop]:
            name, version_text, version_state = group[:3]

            if name != requirement.canonical_name or not isinstance(
                version_text,
                str,
            ):
                continue

            version = Version.from_cache_state(version_state)

            if version in seen_versions:
                continue

            if allowed_versions is not None and version not in allowed_versions:
                continue

            if not bounded_only and not requirement.is_satisfied_by(
                version,
                allow_prereleases=True,
            ):
                continue

            if version_text not in choices:
                return None

            seen_versions.add(version)

            choice = choices[version_text]

            if choice is None:
                continue

            record, record_kind, tag_rank = choice

            if record_kind == WHEEL_RECORD and tag_rank is None:
                continue

            if record_kind not in {WHEEL_RECORD, SDIST_RECORD}:
                continue

            if (
                record[RECORD_YANKED] is not None
                and not self.allow_yanked
                and not exact_pin
            ):
                continue

            is_yanked = record[RECORD_YANKED] is not None

            is_wheel = record_kind == WHEEL_RECORD

            bucket = (2 if is_yanked else 0) + (
                0 if is_wheel or not self.prefer_binary else 1
            )

            descriptor_buckets[bucket].append(
                (version, record, record_kind, tag_rank, source_url),
            )

        if self.prefer_binary:
            descriptors = [
                descriptor
                for bucket in descriptor_buckets
                for descriptor in reversed(bucket)
            ]

        else:
            descriptors = [
                *reversed(descriptor_buckets[0] + descriptor_buckets[1]),
                *reversed(descriptor_buckets[2] + descriptor_buckets[3]),
            ]

        if any(descriptor[0].is_prerelease for descriptor in descriptors):
            allow_prereleases = (
                None
                if self.release_control is None
                else self.release_control.allows_prereleases(requirement.name)
            )

            if allow_prereleases is False:
                descriptors = [
                    descriptor
                    for descriptor in descriptors
                    if not descriptor[0].is_prerelease
                ]

            elif allow_prereleases is None:
                explicitly_allowed = any(
                    specifier.operator != "==="
                    and not specifier.version.endswith(".*")
                    and specifier.parsed_version.is_prerelease
                    for specifier in requirement.specifier.specifiers
                )

                if not explicitly_allowed:
                    stable = [
                        descriptor
                        for descriptor in descriptors
                        if not descriptor[0].is_prerelease
                    ]

                    if stable:
                        descriptors = stable

        preferred_indexes: list[int] = []

        seen_slots: set[tuple[str, bool]] = set()

        for index, descriptor in enumerate(descriptors):
            version, _record, record_kind, _tag_rank, _source_url = descriptor

            slot = (
                "source" if record_kind == SDIST_RECORD else "wheel",
                version.is_prerelease,
            )

            if slot in seen_slots:
                continue

            seen_slots.add(slot)

            preferred_indexes.append(index)

        preferred_set = set(preferred_indexes)

        ordered = [descriptors[index] for index in preferred_indexes]

        ordered.extend(
            descriptor
            for index, descriptor in enumerate(descriptors)
            if index not in preferred_set
        )

        catalog_key = (requirement.canonical_name, allow_binary, allow_source)

        return self._generate_catalog_candidates(catalog_key, ordered)

    def _catalog_descriptor_sort_key(
        self,
        descriptor: tuple[
            Version,
            tuple[object, ...],
            int,
            int | None,
            str,
        ],
    ) -> tuple[object, ...]:
        version, record, record_kind, tag_rank, _source_url = descriptor

        yanked_rank = int(record[RECORD_YANKED] is None)

        wheel_rank = int(record_kind == WHEEL_RECORD)

        tag_sort = -(tag_rank if tag_rank is not None else 1_000_000)

        if self.prefer_binary:
            return yanked_rank, wheel_rank, version, tag_sort

        return yanked_rank, version, wheel_rank, tag_sort

    def _generate_catalog_candidates(
        self,
        catalog_key: tuple[str, bool, bool],
        ordered: list[tuple[Version, tuple[object, ...], int, int | None, str]],
    ) -> Iterator[CandidateRecord]:
        for version, record, record_kind, tag_rank, source_url in ordered:
            parsed_wheel: WheelFile | None = None

            if record_kind == WHEEL_RECORD:
                parsed_wheel = wheel_file_from_record(
                    record,
                    name=catalog_key[0],
                    version=version,
                )

                if parsed_wheel is None:
                    continue

            try:
                link = self.link_from_catalog_record(record, source_url)

            except ValueError:
                continue

            candidate = CandidateRecord(
                name=catalog_key[0],
                version=version,
                link=link,
                wheel=parsed_wheel,
                tag_rank=tag_rank,
            )

            self.candidate_record_cache.setdefault(link, candidate)

            yield candidate

    def lazy_catalog_records(
        self,
        requirement: Requirement,
        *,
        allowed_versions: frozenset[Version] | None = None,
    ) -> Iterator[CandidateRecord] | None:
        """Stream primary cached artifacts without constructing every link."""

        hashes = self.hashes_by_name.get(requirement.canonical_name)

        if (
            requirement.url is not None
            or self.find_links
            or self.uploaded_prior_to is not None
            or (hashes is not None and hashes.allowed_internal)
        ):
            return None

        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        catalog_key = (
            requirement.canonical_name,
            allow_binary,
            allow_source,
        )

        cached_groups = self.catalog_groups(requirement)

        if cached_groups is not None and len(cached_groups) == 1:
            groups, source_url, generation = cached_groups[0]

            direct = self.lazy_summary_records(
                requirement,
                groups,
                source_url,
                generation,
                allowed_versions=allowed_versions,
            )

            if direct is not None:
                return direct

        catalog = self.package_catalog_cache.get(catalog_key)

        if catalog is None:
            self.available_versions(requirement)

            catalog = self.package_catalog_cache.get(catalog_key)

        if catalog is None or catalog.records_by_version is None:
            return None

        exact_version = self.exact_version_internal(requirement)

        if exact_version is not None:
            versions = (exact_version,)

        else:
            versions = tuple(
                dict.fromkeys(
                    summary.version
                    for summary in self.matching_versions(
                        requirement,
                        allow_prereleases=True,
                    )
                ),
            )

        if allowed_versions is not None:
            versions = tuple(
                version for version in versions if version in allowed_versions
            )

        supported_tags = supported_wheel_tags(self.target)

        target_key = self.catalog_target_key

        if target_key is None:
            target_key = hashlib.sha256(
                "\0".join(str(tag) for tag in supported_tags).encode(),
            ).hexdigest()

            self.catalog_target_key = target_key

        persistent_cache = getattr(self.session, "cache", None)

        descriptor_list: list[
            tuple[Version, tuple[object, ...], int, int | None, str]
        ] = []

        for version in versions:
            best: tuple[Version, tuple[object, ...], int, int | None, str] | None = None

            best_rank: tuple[int, int, int] | None = None

            for entry in catalog.records_by_version.get(version, ()):
                if not isinstance(entry, tuple) or len(entry) != 2:
                    continue

                source_url = entry[0]

                generation = entry[1]

                if not isinstance(source_url, str) or not isinstance(
                    generation,
                    str,
                ):
                    continue

                cache_identity = (
                    source_url,
                    generation,
                    target_key,
                    allow_binary,
                    allow_source,
                )

                choices = self.catalog_choice_cache.get(cache_identity)

                if choices is None:
                    choices = load_choices(
                        persistent_cache,
                        source_url,
                        generation,
                        target_key,
                        allow_binary,
                        allow_source,
                    )

                    self.catalog_choice_cache[cache_identity] = choices

                version_text = version.public

                if version_text not in choices:
                    return None

                choice = choices[version_text]

                if choice is None:
                    continue

                record, record_kind, tag_rank = choice

                if record_kind == WHEEL_RECORD and tag_rank is None:
                    continue

                if record_kind not in {WHEEL_RECORD, SDIST_RECORD}:
                    continue

                yanked = record[RECORD_YANKED]

                rank = (
                    int(yanked is None),
                    int(record_kind == WHEEL_RECORD),
                    -(tag_rank if tag_rank is not None else 1_000_000),
                )

                if best_rank is None or rank > best_rank:
                    best_rank = rank

                    best = version, record, record_kind, tag_rank, source_url

            if best is not None:
                descriptor_list.append(best)

        exact_pin = any(
            specifier.operator in {"==", "==="} and not specifier.version.endswith(".*")
            for specifier in requirement.specifier.specifiers
        )

        descriptor_list = [
            descriptor
            for descriptor in descriptor_list
            if descriptor[1][RECORD_YANKED] is None or self.allow_yanked or exact_pin
        ]

        descriptor_list.sort(key=self._catalog_descriptor_sort_key, reverse=True)

        if any(descriptor[0].is_prerelease for descriptor in descriptor_list):
            allow_prereleases = (
                None
                if self.release_control is None
                else self.release_control.allows_prereleases(requirement.name)
            )

            if allow_prereleases is False:
                descriptor_list = [
                    descriptor
                    for descriptor in descriptor_list
                    if not descriptor[0].is_prerelease
                ]

            elif allow_prereleases is None:
                explicitly_allowed = any(
                    specifier.operator != "==="
                    and not specifier.version.endswith(".*")
                    and specifier.parsed_version.is_prerelease
                    for specifier in requirement.specifier.specifiers
                )

                if not explicitly_allowed:
                    stable = [
                        descriptor
                        for descriptor in descriptor_list
                        if not descriptor[0].is_prerelease
                    ]

                    if stable:
                        descriptor_list = stable

        # The target-choice cache contributes at most one winning artifact for

        # each unique release.  ``versions`` is already de-duplicated using PEP

        # 440 equality, so a second filename/hash identity pass cannot remove

        # anything and only reparses every release URL.

        deduplicated = descriptor_list

        preferred_indexes: list[int] = []

        seen_slots: set[tuple[str, bool]] = set()

        for index, descriptor in enumerate(deduplicated):
            version, _record, record_kind, _tag_rank, _source_url = descriptor

            slot = (
                "source" if record_kind == SDIST_RECORD else "wheel",
                version.is_prerelease,
            )

            if slot in seen_slots:
                continue

            seen_slots.add(slot)

            preferred_indexes.append(index)

        preferred_set = set(preferred_indexes)

        ordered = [deduplicated[index] for index in preferred_indexes]

        ordered.extend(
            descriptor
            for index, descriptor in enumerate(deduplicated)
            if index not in preferred_set
        )

        return self._generate_catalog_candidates(catalog_key, ordered)

    @contextlib.contextmanager
    def yanked_allowed(self) -> Iterator[None]:
        """Admit yanked releases for the duration of the block.

        Yanked releases are excluded by policy, but a caller that has found
        nothing else may deliberately fall back to one.  Owning the flip here
        keeps the relaxation scoped and exception-safe instead of leaving
        callers to save and restore ``allow_yanked`` themselves.

        ``find_candidates`` can return a lazily evaluated stream, so consume
        the result inside the block; a stream materialized afterwards sees the
        restored policy.
        """
        previous = self.allow_yanked
        self.allow_yanked = True
        try:
            yield
        finally:
            self.allow_yanked = previous

    def find_candidates(
        self,
        requirement: Requirement,
        *,
        allowed_versions: frozenset[Version] | None = None,
    ) -> CandidateStream:
        if requirement.name.startswith(("file://", "http://", "https://")):
            name = (
                urllib.parse.urlsplit(requirement.name)
                .path.rstrip("/")
                .rsplit("/", 1)[-1]
            )
            requirement = Requirement(
                name=name or requirement.name,
                specifier=requirement.specifier,
                extras=requirement.extras,
                url=requirement.url or requirement.name,
                marker=requirement.marker,
                raw=requirement.raw,
            )
        if requirement.url is not None and requirement.url.startswith("file://"):
            parts = urllib.parse.urlsplit(requirement.url)
            if parts.netloc == "localhost":
                requirement = requirement.copy_with(
                    url=urllib.parse.urlunsplit(
                        ("file", "", parts.path, parts.query, parts.fragment)
                    ),
                )
        lazy_records = self.lazy_catalog_records(
            requirement,
            allowed_versions=allowed_versions,
        )

        if lazy_records is not None:
            return self.get_materializer_internal().materialize(
                requirement,
                lazy_records,
            )

        records = self.applicable_candidate_records(
            requirement,
            allowed_versions=allowed_versions,
        )

        return self.get_materializer_internal().materialize(requirement, records)

    def get_materializer_internal(self) -> CandidateMaterializer:
        materializer = self.materializer_internal

        if materializer is not None:
            return materializer

        with self.cache_lock:
            materializer = self.materializer_internal

            if materializer is None:
                materializer = CandidateMaterializer(
                    build_options=self.build_options,
                    build_constraints=self.build_constraints,
                    wheel_cache_dir=self.wheel_cache_dir,
                    build_isolation=self.build_isolation,
                    dry_run=self.dry_run,
                    compute_source_hashes=self.compute_source_hashes,
                    session=self.session,
                )

                self.materializer_internal = materializer

        return materializer

    def available_versions(
        self,
        requirement: Requirement,
    ) -> tuple[CandidateSummary, ...]:
        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        cache_key = (
            requirement.canonical_name,
            allow_binary,
            allow_source,
        )

        with self.cache_lock:
            catalog = self.package_catalog_cache.get(cache_key)

        if catalog is not None:
            for link in catalog.links:
                if (
                    link.requires_python
                    and not CandidateEvaluator.requires_python_matches(
                        link.requires_python,
                    )
                ):
                    self.last_rejected_requires_python[requirement.canonical_name] = (
                        link.requires_python
                    )
            return catalog.summaries

        future = (
            self.prefetcher.take(cache_key) if self.prefetcher is not None else None
        )

        if future is not None:
            summaries = future.result()
            with self.cache_lock:
                prefetched = self.package_catalog_cache.get(cache_key)
            if prefetched is not None:
                for link in prefetched.links:
                    if link.requires_python:
                        try:
                            compatible = CandidateEvaluator.requires_python_matches(
                                link.requires_python,
                            )
                        except ValueError:
                            compatible = False
                        if not compatible:
                            self.last_rejected_requires_python[
                                requirement.canonical_name
                            ] = link.requires_python
                            break
            return summaries

        return self.load_available_versions(requirement, cache_key)

    def load_available_versions(
        self,
        requirement: Requirement,
        cache_key: tuple[str, bool, bool] | None = None,
    ) -> tuple[CandidateSummary, ...]:
        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        if cache_key is None:
            cache_key = (
                requirement.canonical_name,
                allow_binary,
                allow_source,
            )

        versions: dict[tuple[str, bool], CandidateSummary] = {}

        links_by_version: dict[Version, list[Link]] = {}

        candidates_by_version: dict[Version, list[CandidateRecord]] = {}

        records_by_version: dict[Version, tuple[object, ...]] = {}

        cached_groups = self.catalog_groups(requirement)

        ordered_summaries: list[CandidateSummary] | None = (
            [] if cached_groups is not None and len(cached_groups) == 1 else None
        )

        catalog_links = (
            () if cached_groups is not None else self.catalog_links(requirement)
        )

        if cached_groups is not None:
            parsed_versions: dict[str, Version] = {}

            allowed_kind_mask = (WHEEL_RECORD if allow_binary else 0) | (
                SDIST_RECORD if allow_source else 0
            )

            unnamed_direct = is_unnamed_direct_requirement_internal(requirement)

            for groups, source_url, generation in cached_groups:
                for name, version_text, version_state, facts in groups:
                    if not unnamed_direct and name != requirement.canonical_name:
                        continue

                    version = (
                        None
                        if ordered_summaries is not None
                        else parsed_versions.get(version_text)
                    )

                    if version is None:
                        version = Version.from_cache_state(version_state)

                        if ordered_summaries is None:
                            parsed_versions[version_text] = version

                    has_eligible_artifact = False

                    ordered_has_unyanked = False

                    ordered_has_yanked = False

                    ordered_yanked_reason: str | None = None

                    for kind_mask, requires_python, yanked in facts:
                        if not kind_mask & allowed_kind_mask:
                            continue

                        if isinstance(requires_python, str):
                            try:
                                if not CandidateEvaluator.requires_python_matches(
                                    requires_python
                                ):
                                    self.last_rejected_requires_python[
                                        requirement.canonical_name
                                    ] = requires_python
                                    continue

                            except ValueError:
                                self.last_rejected_requires_python[
                                    requirement.canonical_name
                                ] = requires_python
                                continue

                        has_eligible_artifact = True

                        is_yanked = yanked is not None

                        yanked_reason = yanked if isinstance(yanked, str) else None

                        if ordered_summaries is None:
                            versions[(version_text, is_yanked)] = CandidateSummary(
                                version=version,
                                is_yanked=is_yanked,
                                yanked_reason=yanked_reason,
                            )

                        elif is_yanked:
                            ordered_has_yanked = True

                            ordered_yanked_reason = yanked_reason

                        else:
                            ordered_has_unyanked = True

                    if has_eligible_artifact:
                        records_by_version[version] = (
                            *records_by_version.get(version, ()),
                            (source_url, generation),
                        )

                        if ordered_summaries is not None:
                            if ordered_has_unyanked:
                                ordered_summaries.append(
                                    CandidateSummary(
                                        version=version,
                                        is_yanked=False,
                                        yanked_reason=None,
                                    ),
                                )

                            if ordered_has_yanked:
                                ordered_summaries.append(
                                    CandidateSummary(
                                        version=version,
                                        is_yanked=True,
                                        yanked_reason=ordered_yanked_reason,
                                    ),
                                )

        for link in catalog_links:
            if link.kind is ArtifactKind.WHEEL and not allow_binary:
                continue

            if link.kind in SOURCE_ARTIFACT_KINDS and not allow_source:
                continue

            if link.kind not in INSTALLABLE_ARTIFACT_KINDS:
                continue

            if link.requires_python:
                try:
                    if not CandidateEvaluator.requires_python_matches(
                        link.requires_python,
                    ):
                        self.last_rejected_requires_python[
                            requirement.canonical_name
                        ] = link.requires_python
                        continue

                except ValueError:
                    self.last_rejected_requires_python[requirement.canonical_name] = (
                        link.requires_python
                    )
                    continue

            with self.cache_lock:
                parsed = self.parsed_link_cache.get(link)

            if parsed is None:
                try:
                    parsed = InstallationCandidate.from_link(link, target=self.target)

                except ValueError:
                    continue

                with self.cache_lock:
                    self.parsed_link_cache[link] = parsed

            if not isinstance(parsed, InstallationCandidate):
                continue

            if not is_unnamed_direct_requirement_internal(requirement) and (
                parsed.canonical_name != requirement.canonical_name
            ):
                continue

            version_links = links_by_version.get(parsed.version)
            if version_links is None:
                version_links = []
                links_by_version[parsed.version] = version_links
            version_links.append(link)

            candidate = self.candidate_record_cache.get(link)

            if candidate is None:
                candidate = parsed.to_record()

                self.candidate_record_cache[link] = candidate

            version_candidates = candidates_by_version.get(parsed.version)
            if version_candidates is None:
                version_candidates = []
                candidates_by_version[parsed.version] = version_candidates
            version_candidates.append(candidate)

            key = (parsed.version.public, link.is_yanked)

            versions[key] = CandidateSummary(
                version=parsed.version,
                is_yanked=link.is_yanked,
                yanked_reason=link.yanked_reason,
            )

        result = (
            tuple(ordered_summaries)
            if ordered_summaries is not None
            else tuple(
                # Sort on the comparison key, not the Version: the order is
                # the same by definition (Version.__lt__ compares exactly
                # this), but tuples of ints compare in C where Versions pay
                # a Python-level __lt__ per comparison -- tens of thousands
                # of them for a package with a few thousand releases.
                sorted(
                    versions.values(),
                    key=lambda item: (item.version, item.is_yanked),
                ),
            )
        )

        catalog = PackageCatalog(
            links=tuple(link for links in links_by_version.values() for link in links),
            candidates_by_version=MappingProxyType(
                {
                    version: tuple(candidates)
                    for version, candidates in candidates_by_version.items()
                },
            ),
            summaries=result,
            summary_versions=tuple(summary.version for summary in result),
            links_by_version=MappingProxyType(
                {version: tuple(links) for version, links in links_by_version.items()},
            ),
            records_by_version=(
                None if cached_groups is None else MappingProxyType(records_by_version)
            ),
        )

        with self.cache_lock:
            self.package_catalog_cache[cache_key] = catalog

        return result

    def load_prefetched_versions(
        self,
        value: tuple[Requirement, tuple[str, bool, bool]],
    ) -> tuple[CandidateSummary, ...]:
        requirement, cache_key = value

        started = time.perf_counter()

        result = self.load_available_versions(requirement, cache_key)

        selection = self.evaluate_links(requirement)

        self.get_materializer_internal().prefetch_metadata(
            selection.accepted[:_CATALOG_METADATA_PREFETCH],
            requirement=requirement,
        )

        elapsed = time.perf_counter() - started

        self.prefetch_policy.observe(cache_key, elapsed, len(result))

        return result

    def prefetch_available_versions(
        self,
        requirements: tuple[Requirement, ...],
    ) -> None:
        """Fetch independent project catalogs in bounded background workers."""

        if (
            len(requirements) < 2
            or self.session is None
            or not self.prefetch_remote_sources
        ):
            return

        unique: dict[tuple[str, bool, bool], Requirement] = {}

        for requirement in requirements:
            if requirement.url is not None:
                continue

            allow_binary, allow_source = self.allowed_formats_internal(requirement)

            key = (requirement.canonical_name, allow_binary, allow_source)

            if key in self.prefetch_settled:
                continue

            with self.cache_lock:
                cached = key in self.package_catalog_cache

            warm = self.warm_catalog_cache.get(key)

            if warm is None:
                warm = bool(self.index_sources) and all(
                    source.has_fresh_cached_page(requirement)
                    for source in self.index_sources
                )

                self.warm_catalog_cache[key] = warm

            if cached or warm:
                self.prefetch_settled.add(key)

                continue

            unique[key] = requirement

        if not unique:
            return

        if self.prefetcher is None:
            self.prefetcher = Prefetcher(
                self.load_prefetched_versions,
                max_workers=_CATALOG_WORKERS,
            )

        pending = (
            tuple(unique.items())
            if len(unique) == 1
            else sorted(
                unique.items(),
                key=lambda item: self.prefetch_policy.priority(item[0]),
                reverse=True,
            )
        )

        for key, requirement in pending:
            if not self.prefetcher.pending(key):
                self.prefetcher.submit(key, (requirement, key))

    def close(self) -> None:
        if self.prefetcher is not None:
            self.prefetcher.close()

            self.prefetcher = None

        if self.index_executor is not None:
            self.index_executor.shutdown(wait=True, cancel_futures=True)

            self.index_executor = None

        if self.materializer_internal is not None:
            self.materializer_internal.close()

    def available_versions_for(
        self,
        requirement: Requirement,
        version: Version,
    ) -> tuple[CandidateSummary, ...]:
        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        catalog_key = (
            requirement.canonical_name,
            allow_binary,
            allow_source,
        )

        catalog = self.package_catalog_cache.get(catalog_key)

        if catalog is None:
            self.available_versions(requirement)

            catalog = self.package_catalog_cache[catalog_key]

        start = bisect_left(catalog.summary_versions, version)

        stop = bisect_right(catalog.summary_versions, version)

        return catalog.summaries[start:stop]

    def candidate_work_cost(self, requirement: Requirement) -> int:
        """Estimate metadata/build cost without initiating new I/O."""

        key = requirement.canonical_name

        cached = self.candidate_work_cost_cache.get(key)

        if cached is not None:
            return cached

        links = self.link_cache.get(key)

        if links is None:
            # Never turn a scheduling decision into a network request.

            return 1

        cost = 1

        for link in links:
            if link.kind in SOURCE_ARTIFACT_KINDS:
                cost = max(cost, 8)

            elif not link.is_file:
                cost = max(cost, 2)

        self.candidate_work_cost_cache[key] = cost

        return cost

    def matching_versions(
        self,
        requirement: Requirement,
        *,
        allow_prereleases: bool,
    ) -> tuple[CandidateSummary, ...]:
        allow_binary, allow_source = self.allowed_formats_internal(requirement)

        key = (
            requirement.canonical_name,
            allow_binary,
            allow_source,
            requirement.specifier.text,
            allow_prereleases,
        )

        cached = self.matching_versions_cache.get(key)

        if cached is not None:
            return cached

        available = self.available_versions(requirement)

        catalog = self.package_catalog_cache.get(
            (requirement.canonical_name, allow_binary, allow_source),
        )

        summary_versions = (
            catalog.summary_versions
            if catalog is not None
            else tuple(summary.version for summary in available)
        )

        lower, upper = requirement.specifier.bounds()

        start = 0

        stop = len(available)

        if lower is not None:
            start = (
                bisect_left(summary_versions, lower[0])
                if lower[1]
                else bisect_right(summary_versions, lower[0])
            )

        if upper is not None:
            stop = (
                bisect_right(summary_versions, upper[0])
                if upper[1]
                else bisect_left(summary_versions, upper[0])
            )

        result = tuple(
            summary
            for summary in available[start:stop]
            if requirement.is_satisfied_by(
                summary.version,
                allow_prereleases=allow_prereleases,
            )
        )

        self.matching_versions_cache[key] = result

        return result

    @staticmethod
    def exact_version_internal(requirement: Requirement) -> Version | None:
        for specifier in requirement.specifier.specifiers:
            if specifier.operator == "==" and not specifier.version.endswith(".*"):
                return specifier.parsed_version

        return None

    def allowed_formats_internal(self, requirement: Requirement) -> tuple[bool, bool]:
        if self.format_control is None:
            return True, True

        if requirement.url is not None or requirement.raw.startswith((".", "/", "~")):
            return True, True

        return self.format_control.allowed_formats(requirement.name)

    @staticmethod
    def deduplicate_candidates(
        accepted: list[CandidateRecord],
    ) -> list[CandidateRecord]:
        """Collapse equivalent artifacts while retaining hash alternatives."""

        seen: set[tuple[str, Version, str, tuple[tuple[str, str], ...]]] = set()

        result: list[CandidateRecord] = []

        for candidate in accepted:
            key = (
                candidate.canonical_name,
                candidate.version,
                str(candidate.link.filename),
                tuple(sorted((candidate.link.hashes or {}).items())),
            )

            if key in seen:
                continue

            seen.add(key)

            result.append(candidate)

        return result

    @staticmethod
    def best_accepted_candidates(
        accepted: tuple[CandidateRecord, ...],
    ) -> tuple[CandidateRecord, ...]:
        selected: list[CandidateRecord] = []

        seen_slots: set[tuple[str, bool]] = set()

        for candidate in accepted:
            slot = (
                "source" if candidate.link.kind in SOURCE_ARTIFACT_KINDS else "wheel",
                candidate.version.is_prerelease,
            )

            if slot in seen_slots:
                continue

            seen_slots.add(slot)

            selected.append(candidate)

        return tuple(selected)
