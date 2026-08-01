from __future__ import annotations

import datetime
import os
import stat
import time
import urllib.parse
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from cpip.core.errors import InstallationError
from cpip.core.hashes import Hashes
from cpip.core.packaging import Requirement, Version
from cpip.core.release_control import ReleaseControl
from cpip.index.candidates import InstallationCandidate
from cpip.index.config import DEFAULT_INDEX_URL
from cpip.index.links import Link
from cpip.index.prefetch import Prefetcher, PrefetchPolicy
from cpip.index.source_locations import (
    FindLinksSource,
    SimpleIndexSource,
    looks_like_path_requirement,
    is_remote_source_location,
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

if TYPE_CHECKING:
    from cpip.core.format_control import FormatControl
    from cpip.core.wheel import TargetContext
    from cpip.index.candidate_materialization import CandidateStream

PYPI_HOSTS = frozenset(("pypi.org", "pypi.python.org"))


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
        self.link_cache = {}
        self.find_links_cache = None
        self.find_links_by_name_cache = None
        self.parsed_link_cache = {}
        self.candidate_selection_cache = {}
        self.matching_versions_cache = {}
        self.package_catalog_cache = {}
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
                return [Link.from_url(requirement.url, source_url=None)]
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
                    grouped.setdefault(parsed.canonical_name, []).append(link)
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

    def evaluate_links(self, requirement: Requirement) -> CandidateSelection:
        accepted: list[CandidateRecord] = []
        rejected: list[RejectedCandidate] = []
        allow_binary, allow_source = self.allowed_formats_internal(requirement)
        selection_key = (
            requirement.canonical_name,
            requirement.specifier.text_internal,
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.marker,
            requirement.raw,
            allow_binary,
            allow_source,
            self.allow_yanked,
            self.prefer_binary,
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
        exact_version = self.exact_version_internal(requirement)
        catalog = self.package_catalog_cache.get(catalog_key)
        if (
            requirement.url is None
            and exact_version is not None
            and catalog is not None
        ):
            links = catalog.links_by_version.get(exact_version, ())
        elif requirement.url is None and catalog is not None:
            matching_versions = self.matching_versions(
                requirement,
                allow_prereleases=True,
            )
            matching_links = tuple(
                link
                for summary in matching_versions
                for link in catalog.links_by_version.get(summary.version, ())
            )
            if matching_links:
                links = tuple(dict.fromkeys(matching_links))
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
            from cpip.index.candidate_evaluators import CandidateEvaluator

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
                result = CandidateEvaluator.evaluate_parsed_link(
                    link,
                    parsed,
                    requirement,
                    allow_yanked=self.allow_yanked,
                    allow_binary=allow_binary,
                    allow_source=allow_source,
                )
                if isinstance(result, InstallationCandidate):
                    accepted.append(result.to_record())
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
        from cpip.index.candidate_evaluators import CandidateEvaluator

        for link in links:
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
            cache_parsed = not CandidateEvaluator.is_unnamed_direct_requirement(
                requirement,
            )
            parsed = self.parsed_link_cache.get(link) if cache_parsed else None
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
                if cache_parsed:
                    self.parsed_link_cache[link] = parsed
            result = CandidateEvaluator.evaluate_parsed_link(
                link,
                parsed,
                requirement,
                allow_yanked=self.allow_yanked,
                allow_binary=allow_binary,
                allow_source=allow_source,
            )
            if isinstance(result, InstallationCandidate):
                accepted.append(result.to_record())
            else:
                rejected.append(result)
        accepted.sort(
            key=lambda candidate: candidate.sort_key(prefer_binary=self.prefer_binary),
            reverse=True,
        )
        selection = CandidateSelection(tuple(accepted), tuple(rejected))
        self.candidate_selection_cache[selection_key] = selection
        return selection

    def find_candidates(
        self,
        requirement: Requirement,
        *,
        allowed_versions: frozenset[Version] | None = None,
    ) -> CandidateStream:
        from cpip.index.candidate_materialization import CandidateMaterializer

        selection = self.evaluate_links(requirement)
        accepted = selection.accepted
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
        if allowed_versions is not None:
            accepted = tuple(
                candidate
                for candidate in accepted
                if candidate.version in allowed_versions
            )
        if requirement.url is None and any(
            candidate.version.is_prerelease for candidate in accepted
        ):
            from cpip.index.candidate_evaluators import CandidateEvaluator

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
        hashes = self.hashes_by_name.get(requirement.canonical_name)
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
        if self.materializer_internal is None:
            self.materializer_internal = CandidateMaterializer(
                build_options=self.build_options,
                build_constraints=self.build_constraints,
                wheel_cache_dir=self.wheel_cache_dir,
                build_isolation=self.build_isolation,
                dry_run=self.dry_run,
                compute_source_hashes=self.compute_source_hashes,
                session=self.session,
            )
        return self.materializer_internal.materialize(requirement, ordered)

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
            return catalog.summaries
        future = (
            self.prefetcher.take(cache_key) if self.prefetcher is not None else None
        )
        if future is not None:
            return future.result()
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
        for link in self.catalog_links(requirement):
            if link.kind is ArtifactKind.WHEEL and not allow_binary:
                continue
            if link.kind in SOURCE_ARTIFACT_KINDS and not allow_source:
                continue
            if link.kind not in INSTALLABLE_ARTIFACT_KINDS:
                continue
            if link.requires_python:
                try:
                    from cpip.index.candidate_evaluators import CandidateEvaluator

                    if not CandidateEvaluator.requires_python_matches(
                        link.requires_python,
                    ):
                        continue
                except ValueError:
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
            links_by_version.setdefault(parsed.version, []).append(link)
            key = (str(parsed.version), link.is_yanked)
            versions[key] = CandidateSummary(
                version=parsed.version,
                is_yanked=link.is_yanked,
                yanked_reason=link.yanked_reason,
            )
        result = tuple(
            sorted(versions.values(), key=lambda item: (item.version, item.is_yanked)),
        )
        summaries_by_version: dict[Version, list[CandidateSummary]] = {}
        for summary in result:
            summaries_by_version.setdefault(summary.version, []).append(summary)
        catalog = PackageCatalog(
            links=tuple(link for links in links_by_version.values() for link in links),
            summaries=result,
            summary_versions=tuple(summary.version for summary in result),
            summaries_by_version=MappingProxyType(
                {
                    version: tuple(summaries)
                    for version, summaries in summaries_by_version.items()
                },
            ),
            links_by_version=MappingProxyType(
                {version: tuple(links) for version, links in links_by_version.items()},
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
            with self.cache_lock:
                cached = key in self.package_catalog_cache
            if not cached:
                unique[key] = requirement
        if not unique:
            return
        if self.prefetcher is None:
            self.prefetcher = Prefetcher(self.load_prefetched_versions, max_workers=8)
        for key, requirement in sorted(
            unique.items(),
            key=lambda item: self.prefetch_policy.priority(item[0]),
            reverse=True,
        ):
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
        return catalog.summaries_by_version.get(version, ())

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
            str(requirement.specifier),
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
