"""Build, cache, and materialize resolved package candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
from collections.abc import Callable, Generator, Iterable, Iterator
from itertools import chain, islice
from threading import RLock
from typing import Any

from cpip.core.errors import BuildError, InstallationError, UnsupportedWheel
from cpip.core.packaging import (
    Requirement,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.temp_dir import remove_temp_directory
from cpip.core.urls import path_to_url
from cpip.core.wheel import (
    WheelCandidate,
    validate_wheel_with_metadata,
    wheel_candidate,
)
from cpip.core.wheel_metadata import parse_metadata_headers
from cpip.build.build import build_wheel_from_source, unpack_source_internal
from cpip.index.artifacts import ArtifactLocator
from cpip.index.candidate_cache import (
    cache_built_wheel as store_cached_wheel,
)
from cpip.index.candidate_cache import (
    cached_wheel_for_link,
    emit_build_message,
)
from cpip.index.candidate_metadata_cache import get_candidate_metadata_cache
from cpip.index.candidate_stream import CandidateStream
from cpip.index.candidates import prepare_project_metadata
from cpip.index.metadata_cache import get_wheel_metadata_cache
from cpip.index.prefetch import Prefetcher
from cpip.index.release_facts_cache import get_release_facts_cache
from cpip.index.source_models import (
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateMetadata,
    CandidateRecord,
    LazyCandidateMetadata,
)
from cpip.index.vcs import git_revision, is_immutable_vcs_link
from cpip.index.vcs import vcs_scheme as parse_vcs_scheme

logger = logging.getLogger(__name__)


_EXTRA_MARKER_RE = re.compile(r"extra\s*(?:==|in)\s*['\"]([^'\"]+)['\"]")

_METADATA_WORKERS = 32


def project_provided_extras(project: object) -> frozenset[str]:
    optional_dependencies = getattr(project, "optional_dependencies", {})

    extras = set(optional_dependencies)

    extras.update(getattr(project, "provided_extras", ()))

    for dependency in getattr(project, "dependencies", ()):
        marker = getattr(parse_requirement(dependency), "marker", None)

        if marker is not None:
            extras.update(_EXTRA_MARKER_RE.findall(str(marker)))

    return frozenset(extras)


def project_dependencies(
    project: object,
    requested_extras: frozenset[str],
) -> tuple[Requirement, ...]:
    values = list(getattr(project, "dependencies", ()))

    optional_dependencies = getattr(project, "optional_dependencies", {})

    for extra in requested_extras:
        values.extend(optional_dependencies.get(extra, ()))

    dependencies = []
    for value in values:
        requirement = parse_requirement(value)
        if not marker_applies(requirement.marker, extras=requested_extras):
            continue
        if requirement.name.startswith(("file://", "http://", "https://")):
            path = urllib.parse.unquote(urllib.parse.urlsplit(requirement.name).path)
            name = path.rstrip("/").rsplit("/", 1)[-1]
            if name:
                requirement = Requirement(
                    name=name,
                    specifier=requirement.specifier,
                    extras=requirement.extras,
                    url=requirement.url or requirement.name,
                    marker=requirement.marker,
                    raw=requirement.raw,
                )
        dependencies.append(requirement)
    return tuple(dependencies)


def candidate_metadata_fingerprint(candidate: CandidateRecord) -> str:
    """Return a cheap identity for persistent candidate metadata."""

    sha256 = candidate.link.hashes.get("sha256")

    if sha256 is not None:
        return f"sha256:{sha256}"

    local_identity = candidate.link.local_identity_internal

    if local_identity is not None:
        return local_identity

    if candidate.link.is_file:
        try:
            stat = os.stat(candidate.link.file_path)

        except OSError:
            pass

        else:
            return (
                f"stat:{os.path.abspath(candidate.link.file_path)}:"
                f"{stat.st_size}:{stat.st_mtime_ns}"
            )

    return candidate.link.url


def vcs_scheme(url: str) -> str | None:
    return parse_vcs_scheme(url)


class LazyWheelCandidate(WheelCandidate):
    """Resolver candidate whose metadata is cheap and whose wheel is deferred."""

    def __init__(
        self,
        record: CandidateRecord | None,
        requirement: Requirement,
        materializer: CandidateMaterializer,
        record_loader: Callable[[], CandidateRecord] | None = None,
        version: Version | None = None,
    ) -> None:
        self._record_internal = record

        self._version_internal = (
            version
            if version is not None
            else (record.version if record is not None else None)
        )

        self.requirement_internal = requirement

        self.materializer_internal = materializer

        self.record_loader_internal = record_loader

        self.materialized_internal: WheelCandidate | None = None

    @property
    def record_internal(self) -> CandidateRecord:
        record = self._record_internal

        if record is None:
            loader = self.record_loader_internal

            if loader is None:
                raise RuntimeError("lazy candidate has no record loader")

            record = loader()

            self._record_internal = record

        return record

    def build_candidate(self) -> WheelCandidate:
        candidate = self.materialized_internal

        if candidate is None:
            candidates = list(
                self.materializer_internal.iter_materialize(
                    self.requirement_internal,
                    (self.record_internal,),
                ),
            )

            if not candidates:
                raise BuildError(
                    f"Unable to materialize candidate {self.record_internal.name}",
                )

            candidate = candidates[0]

            self.materialized_internal = candidate

        return candidate

    def materialize(self) -> WheelCandidate:
        """Return the concrete wheel candidate at an explicit build boundary."""

        return self.build_candidate()

    @property
    def name(self) -> str:
        return self.record_internal.name

    @property
    def version(self) -> Version:
        version = self._version_internal

        if version is None:
            version = self.record_internal.version

            self._version_internal = version

        return version

    @property
    def path(self) -> str:
        if (
            self.materializer_internal.dry_run
            and self.record_internal.link.kind in SOURCE_ARTIFACT_KINDS
        ):
            if not self.record_internal.link.is_file:
                return str(self.record_internal.link.filename)

            local_path = self.materializer_internal.local_path_for(
                self.record_internal,
            )

            assert local_path is not None

            return self.materializer_internal.ensure_local_text(
                self.record_internal,
                local_path=local_path,
            )

        return self.materialize().path

    @property
    def dependencies(self) -> tuple[Requirement, ...]:
        return self.record_internal.metadata().dependencies

    @property
    def provided_extras(self) -> frozenset[str]:
        return self.record_internal.metadata().provided_extras

    @property
    def requires_python(self) -> str | None:
        requires_python = self.record_internal.link.requires_python

        if requires_python is not None:
            return requires_python

        return self.record_internal.metadata().requires_python

    @property
    def source_url(self) -> str:
        return self.record_internal.link.url

    @property
    def source_hashes(self) -> dict[str, str] | None:
        return self.materializer_internal.source_hashes_for(self.record_internal)

    @property
    def source_kind(self) -> str:
        return self.record_internal.link.kind.value

    @property
    def source_vcs(self) -> str | None:
        if not self.record_internal.link.is_vcs:
            return None

        return vcs_scheme(self.record_internal.link.url)

    @property
    def source_vcs_revision(self) -> str | None:
        if not self.record_internal.link.is_vcs:
            return None

        return self.materializer_internal.vcs_revision(self.record_internal.link.url)

    @property
    def from_cache(self) -> bool:
        candidate = self.materialized_internal

        return candidate.from_cache if candidate is not None else False

    @property
    def yanked_reason(self) -> str | None:
        return self.record_internal.link.yanked_reason

    @property
    def wheel_layout(self) -> object | None:
        return self.materialize().wheel_layout


class CandidateMaterializer:
    def __init__(
        self,
        *,
        build_options: dict[str, dict[str, object]] | None = None,
        build_constraints: list[str] | None = None,
        wheel_cache_dir: str | os.PathLike[str] | None = None,
        build_isolation: bool = True,
        dry_run: bool = False,
        compute_source_hashes: bool = False,
        session: Any = None,
    ) -> None:
        self.build_options = build_options

        self.build_constraints = build_constraints

        self.wheel_cache_dir = wheel_cache_dir

        self.build_isolation = build_isolation

        self.dry_run = dry_run

        self.compute_source_hashes = compute_source_hashes

        self.session = session

        self.persistent_metadata_cache = (
            get_wheel_metadata_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.persistent_candidate_metadata_cache = (
            get_candidate_metadata_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.persistent_release_facts_cache = (
            get_release_facts_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.artifacts = None

        self.invalid_links: set[str] = set()

        self.wheel_candidates: dict[
            tuple[str, str, frozenset[str]],
            WheelCandidate,
        ] = {}

        self.metadata_cache: dict[
            tuple[str, str, str, frozenset[str]],
            CandidateMetadata,
        ] = {}

        self.release_metadata_cache: dict[
            tuple[str, str],
            tuple[
                str,
                Version,
                tuple[Requirement, ...],
                frozenset[str],
                str | None,
            ]
            | None,
        ] = {}

        self.prepared_record_cache: dict[
            tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]],
            tuple[CandidateRecord, ...],
        ] = {}

        self.artifact_fingerprint_cache: dict[str, str] = {}

        self.source_hash_cache: dict[str, dict[str, str] | None] = {}

        self.local_artifacts: dict[str, str] = {}

        self.vcs_revisions: dict[str, str] = {}

        self.metadata_prefetcher: Prefetcher[Any, str] | None = None

        self.metadata_prefetch_lock = RLock()

        self.metadata_loads = 0

        self.metadata_cache_hits = 0

        self.metadata_prefetches = 0

        self.artifact_materializations = 0

    def local_path_for(self, candidate: CandidateRecord) -> str | None:
        if not candidate.link.is_file:
            return None

        url = candidate.link.url

        cached = self.local_artifacts.get(url)

        if cached is None:
            cached = candidate.link.file_path

            self.local_artifacts[url] = cached

        return cached

    def ensure_local(
        self,
        candidate: CandidateRecord,
        *,
        local_path: str | None = None,
    ) -> str:
        return self.ensure_local_text(candidate, local_path=local_path)

    def ensure_local_text(
        self,
        candidate: CandidateRecord,
        *,
        local_path: str | None = None,
    ) -> str:
        if not candidate.link.is_vcs:
            cached = self.local_artifacts.get(candidate.link.url)

            if cached is not None:
                return cached

        if candidate.link.is_file:
            path = (
                os.fspath(local_path)
                if local_path is not None
                else self.local_path_for(candidate)
            )

            assert path is not None

            self.local_artifacts[candidate.link.url] = path

            return path

        if self.artifacts is None:
            self.artifacts = ArtifactLocator(
                self.session,
                cache_dir=self.wheel_cache_dir,
            )

        path = self.artifacts.ensure_local_text(
            candidate.link.url,
            is_vcs=candidate.link.is_vcs,
            local_path=local_path,
            hashes=(candidate.link.hashes if not candidate.link.is_vcs else None),
        )

        if candidate.link.is_vcs:
            self.vcs_revisions.setdefault(candidate.link.url, git_revision(path))

        path_text = path

        if not candidate.link.is_vcs:
            self.local_artifacts[candidate.link.url] = path_text

        return path_text

    def vcs_revision(self, url: str) -> str | None:
        """Return the revision observed while materializing a VCS candidate."""

        return self.vcs_revisions.get(url)

    def artifact_fingerprint(self, candidate: CandidateRecord) -> str:
        key = candidate.link.url

        fingerprint = self.artifact_fingerprint_cache.get(key)

        if fingerprint is None:
            fingerprint = candidate_metadata_fingerprint(candidate)

            self.artifact_fingerprint_cache[key] = fingerprint

        return fingerprint

    def source_hashes_for(self, candidate: CandidateRecord) -> dict[str, str] | None:
        hashes = candidate.link.hashes

        if hashes:
            return dict(hashes)

        if candidate.link.kind not in SOURCE_ARTIFACT_KINDS:
            return None

        if candidate.link.is_vcs:
            url = candidate.link.url

            if self.vcs_revision(url) is None:
                local = self.ensure_local_text(candidate)
                remove_temp_directory(local)

            return None

        if self.dry_run and not candidate.link.is_file:
            return None

        fingerprint = self.artifact_fingerprint(candidate)

        if fingerprint in self.source_hash_cache:
            cached = self.source_hash_cache[fingerprint]

            return None if cached is None else dict(cached)

        local = self.ensure_local_text(
            candidate,
            local_path=self.local_path_for(candidate),
        )

        try:
            with open(local, "rb") as file:
                result = {"sha256": hashlib.sha256(file.read()).hexdigest()}

        except OSError:
            self.source_hash_cache[fingerprint] = None

            return None

        self.source_hash_cache[fingerprint] = result

        return dict(result)

    def prepare_records(
        self,
        requirement: Requirement,
        accepted: tuple[CandidateRecord, ...],
    ) -> tuple[CandidateRecord, ...]:
        """Attach lazy metadata loaders without loading candidate metadata."""

        record_key = (
            requirement.canonical_name,
            tuple(sorted(requirement.extras)),
            tuple(
                (candidate.link.url, candidate.version.public) for candidate in accepted
            ),
        )

        records = self.prepared_record_cache.get(record_key)

        if records is None:
            if len(self.prepared_record_cache) >= 4096:
                self.prepared_record_cache.pop(next(iter(self.prepared_record_cache)))

            records = tuple(
                self.prepare_record(requirement, candidate) for candidate in accepted
            )

            self.prepared_record_cache[record_key] = records

        return records

    def prepare_record(
        self,
        requirement: Requirement,
        candidate: CandidateRecord,
    ) -> CandidateRecord:
        """Attach metadata only when a candidate reaches a consumption boundary."""

        if candidate.metadata_loader is not None:
            return candidate

        return candidate.copy_with(
            metadata_loader=self.metadata_loader(candidate, requirement),
        )

    def materialize(
        self,
        requirement: Requirement,
        accepted: Iterable[CandidateRecord],
    ) -> CandidateStream:
        requested_extras = frozenset(requirement.extras)

        accepted_iterator = iter(accepted)

        first = next(accepted_iterator, None)

        if first is None:
            return CandidateStream(iter(()))

        # A cached first choice is the common warm path. Do not speculate on a

        # second release in that case; cold misses retain a two-request window

        # so network latency can overlap when the resolver must backtrack.

        prefetch_count = 0 if self.has_cached_metadata(first, requested_extras) else 2

        initial_records = [first]

        if prefetch_count > 1:
            initial_records.extend(islice(accepted_iterator, prefetch_count - 1))

        prefetched_records = tuple(
            self.prepare_record(requirement, candidate)
            for candidate in initial_records[:prefetch_count]
        )

        self.prefetch_metadata(prefetched_records, requirement=requirement)

        accepted_records = chain(initial_records, accepted_iterator)

        def generate() -> Iterator[WheelCandidate]:
            invalid_versions: set[tuple[str, Version]] = set()

            for index, candidate in enumerate(accepted_records):
                candidate = (
                    prefetched_records[index]
                    if index < len(prefetched_records)
                    else self.prepare_record(requirement, candidate)
                )

                identity = (candidate.canonical_name, candidate.version)

                if identity in invalid_versions:
                    continue

                negative_key = self.negative_fact_key(candidate)

                if (
                    self.persistent_release_facts_cache is not None
                    and self.persistent_release_facts_cache.get(negative_key)
                    is not None
                ):
                    self.invalid_links.add(candidate.link.url)

                    invalid_versions.add(identity)

                    continue

                if candidate.link.kind is ArtifactKind.WHEEL:
                    try:
                        metadata = candidate.metadata()

                    except UnsupportedWheel as exc:
                        if ".dist-info directory" in str(exc):
                            yield LazyWheelCandidate(candidate, requirement, self)

                            continue

                        self.invalid_links.add(candidate.link.url)

                        self.remember_negative_fact(negative_key, str(exc))

                        invalid_versions.add(identity)

                        continue

                    except (OSError, ValueError):
                        self.invalid_links.add(candidate.link.url)

                        self.remember_negative_fact(
                            negative_key,
                            "invalid wheel metadata",
                        )

                        invalid_versions.add(identity)

                        print(
                            f"WARNING: Ignoring version {candidate.version} of "
                            f"{candidate.name} since it has invalid metadata",
                            file=sys.stderr,
                        )

                        continue

                    if metadata.version != candidate.version:
                        print(
                            f"WARNING: {candidate.name} has an inconsistent version: "
                            f"expected '{candidate.version}', but metadata has "
                            f"'{metadata.version}'",
                        )

                        if requirement.extras:
                            print(
                                f"Requested {requirement.raw or requirement.name}, "
                                f"but installing version {metadata.version}",
                            )

                        self.invalid_links.add(candidate.link.url)

                        self.remember_negative_fact(
                            negative_key,
                            "inconsistent wheel version metadata",
                        )

                        invalid_versions.add(identity)

                        continue

                yield LazyWheelCandidate(candidate, requirement, self)

        return CandidateStream(generate())

    def negative_fact_key(self, candidate: CandidateRecord) -> tuple[str, str, str]:
        return (
            candidate.canonical_name,
            candidate.version.public,
            self.artifact_fingerprint(candidate),
        )

    def remember_negative_fact(self, key: tuple[str, str, str], reason: str) -> None:
        if self.persistent_release_facts_cache is not None:
            self.persistent_release_facts_cache.put(key, reason)

    def metadata_cache_keys(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> tuple[
        tuple[str, str, str, frozenset[str]],
        tuple[str, str, tuple[str, ...], str],
    ]:
        fingerprint = self.artifact_fingerprint(candidate)

        return (
            (
                candidate.link.url,
                fingerprint,
                candidate.version.public,
                requested_extras,
            ),
            (
                candidate.link.url,
                candidate.version.public,
                tuple(sorted(requested_extras)),
                fingerprint,
            ),
        )

    def has_cached_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> bool:
        memory_key, persistent_key = self.metadata_cache_keys(
            candidate,
            requested_extras,
        )

        return memory_key in self.metadata_cache or (
            self.persistent_candidate_metadata_cache is not None
            and self.persistent_candidate_metadata_cache.contains(persistent_key)
        )

    def prefetch_metadata(
        self,
        records: tuple[CandidateRecord, ...],
        *,
        requirement: Requirement | None = None,
    ) -> None:
        if self.session is None:
            return

        requested_extras = frozenset(requirement.extras if requirement else ())

        pending: list[tuple[str, str]] = []

        for candidate in records:
            if candidate.link.kind is not ArtifactKind.WHEEL:
                continue

            metadata_link = candidate.link.metadata_link()

            if metadata_link is None:
                continue

            if self.has_cached_metadata(candidate, requested_extras):
                continue

            pending.append((metadata_link.url, metadata_link.url))

        if not pending:
            return

        with self.metadata_prefetch_lock:
            if self.metadata_prefetcher is None:
                self.metadata_prefetcher = Prefetcher(
                    self.session.get,
                    max_workers=_METADATA_WORKERS,
                )

            for key, url in pending:
                if self.metadata_prefetcher.submit(key, url):
                    self.metadata_prefetches += 1

    def take_prefetched_metadata(self, url: str) -> Any:
        with self.metadata_prefetch_lock:
            prefetcher = self.metadata_prefetcher

            future = None if prefetcher is None else prefetcher.take(url)

        return future.result() if future is not None else None

    def close(self) -> None:
        with self.metadata_prefetch_lock:
            prefetcher = self.metadata_prefetcher

            self.metadata_prefetcher = None

        if prefetcher is not None:
            prefetcher.close()

    def metadata_loader(
        self,
        candidate: CandidateRecord,
        requirement: Requirement,
    ) -> LazyCandidateMetadata:
        requested_extras = frozenset(requirement.extras)

        key, persistent_key = self.metadata_cache_keys(
            candidate,
            requested_extras,
        )

        def load() -> CandidateMetadata:
            self.metadata_loads += 1

            cached = self.metadata_cache.get(key)

            if cached is not None:
                self.metadata_cache_hits += 1

                return cached

            if self.persistent_candidate_metadata_cache is not None:
                cached = self.persistent_candidate_metadata_cache.get(persistent_key)

                if cached is not None:
                    self.metadata_cache_hits += 1

                    self.metadata_cache[key] = cached

                    return cached

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                metadata = self.pypi_metadata(candidate, requested_extras)

                if (
                    metadata is not None
                    and requested_extras <= metadata.provided_extras
                ):
                    self.metadata_cache[key] = metadata

                    if self.persistent_candidate_metadata_cache is not None:
                        self.persistent_candidate_metadata_cache.put(
                            persistent_key,
                            metadata,
                        )

                    return metadata

            if candidate.link.kind is ArtifactKind.WHEEL:
                metadata_link = candidate.link.metadata_link()

                metadata = self.remote_wheel_metadata(
                    candidate,
                    requested_extras,
                    response=(
                        self.take_prefetched_metadata(metadata_link.url)
                        if metadata_link is not None
                        else None
                    ),
                )

                if metadata is not None:
                    self.metadata_cache[key] = metadata

                    if self.persistent_candidate_metadata_cache is not None:
                        self.persistent_candidate_metadata_cache.put(
                            persistent_key,
                            metadata,
                        )

                    return metadata

            local_path = self.local_path_for(candidate)

            path_text = self.ensure_local_text(candidate, local_path=local_path)

            vcs_path = path_text if candidate.link.is_vcs else None

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                path = path_text

                try:
                    if (
                        candidate.link.kind is ArtifactKind.SOURCE_TREE
                        and candidate.link.subdirectory_fragment
                    ):
                        path = os.path.join(path, candidate.link.subdirectory_fragment)

                    with tempfile.TemporaryDirectory(
                        prefix="cpip-metadata-"
                    ) as temp_dir:
                        if candidate.link.kind is ArtifactKind.SDIST:
                            path = unpack_source_internal(path, temp_dir)

                        validate_build_requirements(path)

                        try:
                            project = prepare_project_metadata(
                                path,
                                build_constraints=self.build_constraints,
                                build_isolation=self.build_isolation,
                            )

                        except BuildError as exc:
                            metadata = self.pypi_metadata(candidate, requested_extras)

                            if metadata is None:
                                raise BuildError(
                                    f"Failed to build '{candidate.name}': {exc}",
                                ) from exc

                        else:
                            metadata = CandidateMetadata(
                                name=project.name,
                                version=Version(project.version),
                                dependencies=project_dependencies(
                                    project,
                                    requested_extras,
                                ),
                                provided_extras=project_provided_extras(project),
                                requires_python=project.requires_python,
                            )
                finally:
                    if vcs_path is not None:
                        remove_temp_directory(vcs_path)

            else:
                with (
                    open(path_text, "rb", buffering=32768) as stream,
                    zipfile.ZipFile(stream) as archive,
                ):
                    try:
                        dist_info_dir, wheel_metadata_text = (
                            validate_wheel_with_metadata(
                                archive,
                                os.path.basename(path_text)[:-4].split("-", 1)[0],
                            )
                        )

                    except UnsupportedWheel as exc:
                        raise InstallationError(str(exc)) from exc

                    built = wheel_candidate(
                        path_text,
                        requested_extras,
                        archive=archive,
                        filename_info=(candidate.name, candidate.version),
                        dist_info_dir=dist_info_dir,
                        wheel_metadata_text=wheel_metadata_text,
                        include_layout=False,
                        metadata_cache=self.persistent_metadata_cache,
                    )

                metadata = CandidateMetadata(
                    name=built.name,
                    version=built.version,
                    dependencies=built.dependencies,
                    provided_extras=built.provided_extras,
                    requires_python=built.requires_python,
                )

            self.metadata_cache[key] = metadata

            if self.persistent_candidate_metadata_cache is not None:
                self.persistent_candidate_metadata_cache.put(persistent_key, metadata)

            return metadata

        return LazyCandidateMetadata(load)

    def remote_wheel_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
        response: Any = None,
    ) -> CandidateMetadata | None:
        if self.session is None:
            return None

        metadata_link = candidate.link.metadata_link()

        if metadata_link is None:
            return None

        try:
            if response is None:
                response = self.session.get(metadata_link.url)

            response.raise_for_status()

            headers = parse_metadata_headers(response.text)

            name = headers.get("name", (None,))[0]

            version = headers.get("version", (None,))[0]

            if name is None or version is None:
                return None

            dependencies = tuple(
                requirement
                for value in headers.get("requires-dist", ())
                if (requirement := parse_requirement(value)) is not None
                if marker_applies(requirement.marker, extras=requested_extras)
            )

            return CandidateMetadata(
                name=name,
                version=Version(version),
                dependencies=dependencies,
                provided_extras=frozenset(headers.get("provides-extra", ())),
                requires_python=(headers.get("requires-python") or [None])[0],
            )

        except (KeyError, OSError, TypeError, ValueError):
            return None

    def pypi_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> CandidateMetadata | None:
        """Read release metadata when a PyPI sdist backend cannot run."""

        source_url = candidate.link.source_url or candidate.link.url

        host = urllib.parse.urlparse(source_url).hostname

        if host not in {"pypi.org", "pypi.python.org"}:
            return None

        release_key = (candidate.canonical_name, candidate.version.public)

        if release_key in self.release_metadata_cache:
            release = self.release_metadata_cache[release_key]

            if release is None:
                return None

            name, version, all_dependencies, extras, requires_python = release

            return CandidateMetadata(
                name=name,
                version=version,
                dependencies=tuple(
                    requirement
                    for requirement in all_dependencies
                    if marker_applies(requirement.marker, extras=requested_extras)
                ),
                provided_extras=extras,
                requires_python=requires_python,
            )

        url = (
            "https://pypi.org/pypi/"
            f"{urllib.parse.quote(candidate.canonical_name)}/"
            f"{urllib.parse.quote(candidate.version.public)}/json"
        )

        try:
            if self.session is None:
                from cpip._vendor import requests

                self.session = requests.Session()

            response = self.session.get(url)

            if getattr(response, "status_code", None) == 404:
                # The versioned JSON API is only a metadata optimization.

                # Legacy or removed releases can remain downloadable from

                # the Simple API after this endpoint disappears, so fall

                # through to artifact metadata instead of failing resolution.

                self.release_metadata_cache[release_key] = None

                return None

            response.raise_for_status()

            data = json.loads(response.text)

            info = data["info"]

            dependencies = tuple(
                requirement
                for value in tuple(info.get("requires_dist") or ())
                if (requirement := parse_requirement(value)) is not None
            )

            extras = frozenset(info.get("provides_extra") or ())

            release = (
                str(info["name"]),
                Version(str(info["version"])),
                dependencies,
                extras,
                info.get("requires_python"),
            )

            self.release_metadata_cache[release_key] = release

            name, version, all_dependencies, extras, requires_python = release

            return CandidateMetadata(
                name=name,
                version=version,
                dependencies=tuple(
                    requirement
                    for requirement in all_dependencies
                    if marker_applies(requirement.marker, extras=requested_extras)
                ),
                provided_extras=extras,
                requires_python=requires_python,
            )

        except (KeyError, OSError, TypeError, ValueError):
            self.release_metadata_cache[release_key] = None

            return None

    def iter_materialize(
        self,
        requirement: Requirement,
        accepted: tuple[CandidateRecord, ...],
    ) -> Generator[WheelCandidate, None, list[WheelCandidate]]:
        candidates: list[WheelCandidate] = []

        seen: set[tuple[str, str, str]] = set()

        requested_extras = frozenset(requirement.extras)

        for candidate in accepted:
            self.artifact_materializations += 1

            from_cache = False

            cache_hashes: dict[str, str] | None = None

            local_path = self.local_path_for(candidate)

            path = self.ensure_local_text(candidate, local_path=local_path)

            source_hashes = dict(candidate.link.hashes)

            if not source_hashes and self.artifacts is not None:
                cached_hashes = self.artifacts.hashes_for(candidate.link.url)

                if cached_hashes is not None:
                    source_hashes.update(cached_hashes)

            if (
                self.compute_source_hashes
                and not source_hashes
                and local_path is not None
            ):
                try:
                    with open(local_path, "rb") as file:
                        source_hashes["sha256"] = hashlib.sha256(
                            file.read(),
                        ).hexdigest()

                except OSError:
                    pass

            materialized_vcs_path = path if candidate.link.is_vcs else None

            if (
                candidate.link.kind is ArtifactKind.SOURCE_TREE
                and candidate.link.subdirectory_fragment
            ):
                path = os.path.join(path, candidate.link.subdirectory_fragment)

            cache_built_wheel = (
                candidate.link.kind is ArtifactKind.SDIST and not candidates
            ) or (
                candidate.link.kind is ArtifactKind.SOURCE_TREE
                and is_immutable_vcs_link(candidate.link.url)
                and not candidates
            )

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                display_name = (
                    requirement.name
                    if canonicalize_name(requirement.name) == candidate.canonical_name
                    else candidate.name
                )

                if requirement.name is None:
                    display_name = candidate.name

                cached = cached_wheel_for_link(self.wheel_cache_dir, candidate.link.url)

                if cached is not None:
                    path, cache_hashes = cached

                    from_cache = True

                    cached_name = os.path.basename(os.fspath(path)).split("-", 1)[0]

                    logger.debug(
                        "use cached built wheel for %s from %s",
                        display_name,
                        candidate.link.url,
                    )

                    emit_build_message(f"Using cached {cached_name}")

                else:
                    emit_build_message("Preparing build dependencies")

                    validate_build_requirements(path)

                    key = requirement.raw

                    logger.debug(
                        "build source candidate %s from %s",
                        display_name,
                        candidate.link.url,
                    )

                    emit_build_message(f"Building wheel for {display_name}")

                    try:
                        path = build_wheel_from_source(
                            path,
                            config_settings=(self.build_options or {}).get(key),
                            build_constraints=self.build_constraints,
                            build_isolation=self.build_isolation,
                        )

                    except BuildError as exc:
                        emit_build_message(f"Failed to build '{display_name}'")

                        raise BuildError(
                            f"Failed to build '{display_name}': {exc}",
                        ) from exc

                    emit_build_message(f"Created wheel for {display_name}")

                    emit_build_message(f"Successfully built {display_name}")

                    if cache_built_wheel:
                        logger.debug(
                            "store cached built wheel for %s from %s",
                            display_name,
                            candidate.link.url,
                        )

                        store_cached_wheel(self.wheel_cache_dir, candidate, path)

            try:
                if candidate.link.kind is ArtifactKind.WHEEL:
                    cache_key = (
                        candidate.link.url,
                        self.artifact_fingerprint(candidate),
                        requested_extras,
                    )

                    built = self.wheel_candidates.get(cache_key)

                    if built is None:
                        with (
                            open(path, "rb", buffering=32768) as stream,
                            zipfile.ZipFile(stream) as archive,
                        ):
                            dist_info_dir, wheel_metadata_text = (
                                validate_wheel_with_metadata(
                                    archive,
                                    os.path.basename(os.fspath(path))[:-4].split(
                                        "-",
                                        1,
                                    )[0],
                                )
                            )

                            built = wheel_candidate(
                                path,
                                requested_extras,
                                archive=archive,
                                filename_info=(candidate.name, candidate.version),
                                dist_info_dir=dist_info_dir,
                                wheel_metadata_text=wheel_metadata_text,
                            )

                        self.wheel_candidates[cache_key] = built

                else:
                    built = wheel_candidate(path, requested_extras)

            except UnsupportedWheel as exc:
                if ".dist-info directory" not in str(exc):
                    self.invalid_links.add(candidate.link.url)

                    logger.warning("%s", exc)

                    continue

                raise

            except ValueError:
                self.invalid_links.add(candidate.link.url)

                print(
                    f"WARNING: Ignoring version {candidate.version} of "
                    f"{candidate.name} since it has invalid metadata",
                    file=sys.stderr,
                )

                continue

            if built.version != candidate.version and candidate.version != Version("0"):
                print(
                    f"WARNING: {candidate.name} has an inconsistent version: "
                    f"expected '{candidate.version}', but metadata has "
                    f"'{built.version}'",
                )

                if requirement.extras:
                    print(
                        f"Requested {requirement.raw or requirement.name}, "
                        f"but installing version {built.version}",
                    )

                self.invalid_links.add(candidate.link.url)

                continue

            wheel = WheelCandidate(
                name=built.name,
                version=built.version,
                path=built.path,
                dependencies=built.dependencies,
                provided_extras=built.provided_extras,
                requires_python=built.requires_python or candidate.link.requires_python,
                source_url=candidate.link.url,
                source_hashes=cache_hashes
                if cache_hashes is not None
                else source_hashes,
                source_kind=candidate.link.kind.value,
                source_vcs=vcs_scheme(candidate.link.url)
                if candidate.link.is_vcs
                else None,
                from_cache=from_cache,
                yanked_reason=candidate.link.yanked_reason,
            )

            key = (wheel.canonical_name, str(wheel.version), str(wheel.path))

            if key in seen:
                logger.debug(
                    "dedupe candidate %s==%s path=%s",
                    wheel.name,
                    wheel.version,
                    os.path.basename(wheel.path),
                )

                if materialized_vcs_path is not None:
                    remove_temp_directory(materialized_vcs_path)

                continue

            seen.add(key)

            candidates.append(wheel)

            if materialized_vcs_path is not None:
                remove_temp_directory(materialized_vcs_path)

            logger.debug(
                "candidate ready %s==%s kind=%s",
                candidate.name,
                candidate.version,
                candidate.link.kind.value,
            )

            yield wheel

        logger.debug(
            "materialization completed requirement=%s produced=%d",
            requirement.raw or requirement.name,
            len(candidates),
        )

        return candidates


def validate_build_requirements(source: str | os.PathLike[str]) -> None:
    pyproject = os.path.join(os.fspath(source), "pyproject.toml")

    try:
        with open(pyproject, encoding="utf-8") as file:
            contents = file.read()

    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return

    try:
        import tomllib

    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        from cpip._vendor import tomli as tomllib

    try:
        data = tomllib.loads(contents)

    except tomllib.TOMLDecodeError as exc:
        raise BuildError(
            f"Invalid PEP 518 build requirements in {pyproject}: {exc}",
        ) from exc

    if "build-system" not in data:
        return

    build_system = data["build-system"]

    if not isinstance(build_system, dict):
        raise BuildError(
            f"Invalid PEP 518 [build-system] table in {pyproject}: mandatory `requires` key is missing",
        )

    if "requires" not in build_system:
        raise BuildError(
            f"Invalid PEP 518 [build-system] table in {pyproject}: mandatory `requires` key is missing",
        )

    requires = build_system.get("requires")

    if not isinstance(requires, list) or not all(
        isinstance(item, str) for item in requires
    ):
        raise BuildError(
            f"Invalid PEP 518 build requirements in {pyproject}: build-system.requires is not a list of strings",
        )

    for item in requires:
        try:
            req = parse_requirement(item)

        except ValueError:
            continue

        if canonicalize_name(req.name) == "setuptools":
            minimum = Version("40.8.0")

            _, upper_bound = req.specifier.bounds()

            if upper_bound is not None and (
                upper_bound[0] < minimum
                or (upper_bound[0] == minimum and not upper_bound[1])
            ):
                raise BuildError(
                    f"Some build dependencies for {path_to_url(os.path.abspath(os.fspath(source)))} conflict with PEP 517/518 supported requirements: "
                    "setuptools==1.0 is incompatible with setuptools>=40.8.0,<82.",
                )
