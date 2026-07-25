from __future__ import annotations

import datetime
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pip.core.format_control import FormatControl
from pip.core.errors import InstallationError
from pip.core.hashes import Hashes
from pip.core.packaging import Requirement
from pip.core.release_control import ReleaseControl
from pip.core.wheel import TargetContext
from pip.index.candidate_evaluators import CandidateEvaluator
from pip.index.candidate_materialization import CandidateMaterializer, CandidateStream
from pip.index.candidates import InstallationCandidate
from pip.index.config import DEFAULT_INDEX_URL
from pip.index.links import Link
from pip.index.source_locations import (
    FindLinksSource,
    SimpleIndexSource,
    looks_like_path_requirement,
)
from pip.index.source_models import (
    ArtifactKind,
    CandidateSelection,
    CandidateSummary,
    PackageSource,
    RejectedCandidate,
    RejectionReason,
)


@dataclass
class CandidateProvider:
    sources: tuple[PackageSource, ...]
    find_links: list[str]
    index_urls: list[str]
    no_index: bool = False
    allow_yanked: bool = False
    release_control: ReleaseControl | None = None
    format_control: FormatControl | None = None
    prefer_binary: bool = False
    target: TargetContext | None = None
    build_options: dict[str, dict[str, object]] | None = None
    build_constraints: list[str] | None = None
    wheel_cache_dir: Path | None = None
    trusted_hosts: tuple[str, ...] = ()
    build_isolation: bool = True
    locked_links: dict[str, Link] = field(default_factory=dict)
    session: Any = None
    uploaded_prior_to: datetime.datetime | None = None
    hashes_by_name: dict[str, Hashes] = field(default_factory=dict)
    _link_cache: dict[str, tuple[Link, ...]] = field(
        default_factory=dict, init=False, repr=False
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
        wheel_cache_dir: str | Path | None = None,
        trusted_hosts: list[str] | tuple[str, ...] = (),
        build_isolation: bool = True,
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
                    tuple(normalized_find_links), tuple(trusted_hosts), session
                )
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
            wheel_cache_dir=Path(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None,
            trusted_hosts=tuple(trusted_hosts),
            build_isolation=build_isolation,
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
            path = Path(requirement.raw)
            return [Link.from_path(path, source_url=None)] if path.exists() else []
        links: list[Link] = []
        cache_key = requirement.canonical_name
        cached = self._link_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        seen: set[str] = set()
        sources: list[PackageSource] = []
        if self.find_links:
            sources.append(
                FindLinksSource(
                    tuple(self.find_links), self.trusted_hosts, self.session
                )
            )
        sources.extend(
            SimpleIndexSource(url, self.trusted_hosts, self.session)
            for url in self.index_urls
        )
        for source in sources:
            for link in source.collect_links(requirement):
                if link.url in seen:
                    continue
                seen.add(link.url)
                links.append(link)
        self._link_cache[cache_key] = tuple(links)
        return links

    def evaluate_links(self, requirement: Requirement) -> CandidateSelection:
        accepted: list[InstallationCandidate] = []
        rejected: list[RejectedCandidate] = []
        allow_binary, allow_source = self._allowed_formats(requirement)
        for link in self.collect_links(requirement):
            if self.uploaded_prior_to is not None:
                # Upload timestamps describe index-hosted artifacts. Local
                # files, directories, and VCS checkouts are already under the
                # user's control and must not be rejected by this filter.
                if link.is_file or link.is_existing_dir or link.is_vcs:
                    pass
                else:
                    if link.upload_time is None or (
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
                            and host in {"pypi.org", "pypi.python.org"}
                            and cutoff > datetime.datetime.now(datetime.timezone.utc)
                        ):
                            continue
                        rejected.append(
                            RejectedCandidate(
                                link,
                                RejectionReason.MISSING_ARTIFACT,
                                "does not provide upload-time metadata before the cutoff",
                            )
                        )
                        continue
            result = CandidateEvaluator.evaluate_link(
                link,
                requirement,
                allow_yanked=self.allow_yanked,
                allow_binary=allow_binary,
                allow_source=allow_source,
                target=self.target,
            )
            if isinstance(result, InstallationCandidate):
                accepted.append(result)
            else:
                rejected.append(result)
        accepted.sort(
            key=lambda candidate: candidate.sort_key(prefer_binary=self.prefer_binary),
            reverse=True,
        )
        return CandidateSelection(tuple(accepted), tuple(rejected))

    def find_candidates(self, requirement: Requirement) -> CandidateStream:
        selection = self.evaluate_links(requirement)
        accepted = selection.accepted
        if not accepted and requirement.url is not None:
            if selection.rejected and selection.rejected[0].link.is_vcs:
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
                    upload_rejection.link.source_url or ""
                ).hostname
                if host not in {"pypi.org", "pypi.python.org"}:
                    raise InstallationError(upload_rejection.detail)
        if requirement.url is None:
            accepted = tuple(
                CandidateEvaluator.create(
                    requirement.name,
                    release_control=self.release_control,
                    prefer_binary=self.prefer_binary,
                    specifier=requirement.specifier,
                    target=self.target,
                    hashes=None,
                ).get_applicable_candidates(list(accepted))
            )
        hashes = self.hashes_by_name.get(requirement.canonical_name)
        if hashes is not None and hashes._allowed:
            allowed = {
                digest.lower()
                for digests in hashes._allowed.values()
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
        preferred = self._best_accepted_candidates(requirement, accepted)
        preferred_set = set(preferred)
        ordered = preferred + tuple(
            candidate for candidate in accepted if candidate not in preferred_set
        )
        materializer = CandidateMaterializer(
            build_options=self.build_options,
            build_constraints=self.build_constraints,
            wheel_cache_dir=self.wheel_cache_dir,
            build_isolation=self.build_isolation,
            session=self.session,
        )
        return materializer.materialize(requirement, ordered)

    def available_versions(
        self, requirement: Requirement
    ) -> tuple[CandidateSummary, ...]:
        versions: dict[tuple[str, bool], CandidateSummary] = {}
        allow_binary, allow_source = self._allowed_formats(requirement)
        for link in self.collect_links(requirement):
            if link.kind is ArtifactKind.WHEEL and not allow_binary:
                continue
            if (
                link.kind in {ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE}
                and not allow_source
            ):
                continue
            if link.kind not in {
                ArtifactKind.WHEEL,
                ArtifactKind.SDIST,
                ArtifactKind.SOURCE_TREE,
            }:
                continue
            if link.requires_python:
                try:
                    if not CandidateEvaluator.requires_python_matches(
                        link.requires_python
                    ):
                        continue
                except ValueError:
                    continue
            parsed = InstallationCandidate.from_link(link, target=self.target)
            if not isinstance(parsed, InstallationCandidate):
                continue
            if not CandidateEvaluator.is_unnamed_direct_requirement(requirement) and (
                parsed.canonical_name != requirement.canonical_name
            ):
                continue
            key = (str(parsed.version), link.is_yanked)
            versions[key] = CandidateSummary(
                version=parsed.version,
                is_yanked=link.is_yanked,
                yanked_reason=link.yanked_reason,
            )
        return tuple(
            sorted(versions.values(), key=lambda item: (item.version, item.is_yanked))
        )

    def _allowed_formats(self, requirement: Requirement) -> tuple[bool, bool]:
        if self.format_control is None:
            return True, True
        if requirement.url is not None or requirement.raw.startswith((".", "/", "~")):
            return True, True
        return self.format_control.allowed_formats(requirement.name)

    @staticmethod
    def _best_accepted_candidates(
        requirement: Requirement,
        accepted: tuple[InstallationCandidate, ...],
    ) -> tuple[InstallationCandidate, ...]:
        selected: list[InstallationCandidate] = []
        seen_slots: set[tuple[str, bool]] = set()
        for candidate in accepted:
            if not requirement.is_satisfied_by(
                candidate.version, allow_prereleases=True
            ):
                continue
            slot = (
                "source"
                if candidate.link.kind in {ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE}
                else "wheel",
                candidate.version.is_prerelease,
            )
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
            selected.append(candidate)
        return tuple(selected)
