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
import urllib.request
import zipfile
from collections.abc import Generator, Iterator
from typing import Any

from cpip.core.errors import BuildError, InstallationError, UnsupportedWheel
from cpip.core.packaging import (
    Requirement,
    Version,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.urls import path_to_url
from cpip.core.wheel import (
    WheelCandidate,
    validate_wheel_with_metadata,
    wheel_candidate,
)
from cpip.index.candidate_cache import (
    cache_built_wheel as store_cached_wheel,
)
from cpip.index.candidate_cache import (
    cached_wheel_for_link,
    emit_build_message,
)
from cpip.index.candidate_metadata_cache import get_candidate_metadata_cache
from cpip.index.candidate_stream import CandidateStream
from cpip.index.metadata_cache import get_wheel_metadata_cache
from cpip.index.prefetch import Prefetcher
from cpip.index.source_models import (
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateMetadata,
    CandidateRecord,
    LazyCandidateMetadata,
)

logger = logging.getLogger(__name__)

_EXTRA_MARKER_RE = re.compile(r"extra\s*(?:==|in)\s*['\"]([^'\"]+)['\"]")


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
    return tuple(
        requirement
        for value in values
        if (requirement := parse_requirement(value)) is not None
        if marker_applies(requirement.marker, extras=requested_extras)
    )


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
    from cpip.index.vcs import vcs_scheme as parse_vcs_scheme

    return parse_vcs_scheme(url)


def remove_temp_directory_internal(path: str | os.PathLike[str]) -> None:
    from cpip.core.temp_dir import remove_temp_directory

    remove_temp_directory(path)


class LazyWheelCandidate(WheelCandidate):
    """Resolver candidate whose metadata is cheap and whose wheel is deferred."""

    def __init__(
        self,
        record: CandidateRecord,
        requirement: Requirement,
        materializer: CandidateMaterializer,
    ) -> None:
        self.record_internal = record
        self.requirement_internal = requirement
        self.materializer_internal = materializer
        self.materialized_internal: WheelCandidate | None = None

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
        return self.record_internal.version

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
        self.artifact_fingerprint_cache: dict[str, str] = {}
        self.source_hash_cache: dict[str, dict[str, str] | None] = {}
        self.local_artifacts: dict[str, str] = {}
        self.vcs_revisions: dict[str, str] = {}
        self.metadata_prefetcher: Prefetcher[Any, str] | None = None

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
            from cpip.index.artifacts import ArtifactLocator

            self.artifacts = ArtifactLocator(self.session)
        path = self.artifacts.ensure_local_text(
            candidate.link.url,
            is_vcs=candidate.link.is_vcs,
            local_path=local_path,
        )
        if candidate.link.is_vcs:
            from cpip.index.vcs import git_revision

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
                remove_temp_directory_internal(local)
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

    def materialize(
        self,
        requirement: Requirement,
        accepted: tuple[CandidateRecord, ...],
    ) -> CandidateStream:
        records = tuple(
            candidate.copy_with(
                metadata_loader=self.metadata_loader(candidate, requirement),
            )
            for candidate in accepted
        )
        # Keep the speculative window bounded, but widen it for large remote
        # candidate sets.  The resolver usually consumes candidates in order;
        # overlapping metadata-only requests for the first few candidates
        # avoids serial latency without downloading artifacts or building
        # sdists.  Small sets retain the old two-request footprint.
        prefetch_count = min(8, max(2, len(records) // 8))
        self.prefetch_metadata(records[:prefetch_count])

        def generate() -> Iterator[WheelCandidate]:
            invalid_versions: set[tuple[str, Version]] = set()
            for candidate in records:
                identity = (candidate.canonical_name, candidate.version)
                if identity in invalid_versions:
                    continue
                if candidate.link.kind is ArtifactKind.WHEEL:
                    try:
                        metadata = candidate.metadata()
                    except UnsupportedWheel as exc:
                        if ".dist-info directory" in str(exc):
                            yield LazyWheelCandidate(candidate, requirement, self)
                            continue
                        self.invalid_links.add(candidate.link.url)
                        invalid_versions.add(identity)
                        continue
                    except (OSError, ValueError):
                        self.invalid_links.add(candidate.link.url)
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
                        invalid_versions.add(identity)
                        continue
                yield LazyWheelCandidate(candidate, requirement, self)

        return CandidateStream(generate())

    def prefetch_metadata(self, records: tuple[CandidateRecord, ...]) -> None:
        if not self.dry_run or self.session is None:
            return
        for candidate in records:
            if candidate.link.kind is not ArtifactKind.WHEEL:
                continue
            metadata_link = candidate.link.metadata_link()
            if metadata_link is None:
                continue
            if self.metadata_prefetcher is None:
                self.metadata_prefetcher = Prefetcher(self.session.get, max_workers=8)
            self.metadata_prefetcher.submit(metadata_link.url, metadata_link.url)

    def take_prefetched_metadata(self, url: str) -> Any:
        if self.metadata_prefetcher is None:
            return None
        future = self.metadata_prefetcher.take(url)
        return future.result() if future is not None else None

    def close(self) -> None:
        if self.metadata_prefetcher is not None:
            self.metadata_prefetcher.close()
            self.metadata_prefetcher = None

    def metadata_loader(
        self,
        candidate: CandidateRecord,
        requirement: Requirement,
    ) -> LazyCandidateMetadata:
        requested_extras = frozenset(requirement.extras)
        fingerprint = self.artifact_fingerprint(candidate)
        key = (
            candidate.link.url,
            fingerprint,
            str(candidate.version),
            requested_extras,
        )

        def load() -> CandidateMetadata:
            cached = self.metadata_cache.get(key)
            if cached is not None:
                return cached
            persistent_key = (
                candidate.link.url,
                str(candidate.version),
                tuple(sorted(requested_extras)),
                fingerprint,
            )
            if self.persistent_candidate_metadata_cache is not None:
                cached = self.persistent_candidate_metadata_cache.get(persistent_key)
                if cached is not None:
                    self.metadata_cache[key] = cached
                    return cached
            if candidate.link.kind in SOURCE_ARTIFACT_KINDS and self.dry_run:
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
            if candidate.link.kind is ArtifactKind.WHEEL and self.dry_run:
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
                if (
                    candidate.link.kind is ArtifactKind.SOURCE_TREE
                    and candidate.link.subdirectory_fragment
                ):
                    path = os.path.join(path, candidate.link.subdirectory_fragment)
                with tempfile.TemporaryDirectory(prefix="cpip-metadata-") as temp_dir:
                    if candidate.link.kind is ArtifactKind.SDIST:
                        from cpip.build.build import unpack_source_internal

                        path = unpack_source_internal(path, temp_dir)
                    validate_build_requirements(path)
                    from cpip.index.candidates import prepare_project_metadata

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
                if vcs_path is not None:
                    remove_temp_directory_internal(vcs_path)
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
            from cpip.core.wheel_metadata import parse_metadata_headers

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
        release_key = (candidate.canonical_name, str(candidate.version))
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
            f"{urllib.parse.quote(str(candidate.version))}/json"
        )
        try:
            if self.session is not None:
                response = self.session.get(url)
                response.raise_for_status()
                data = json.loads(response.text)
            else:
                with urllib.request.urlopen(url) as response:
                    data = json.loads(response.read())
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
        from cpip.index.vcs import is_immutable_vcs_link

        candidates: list[WheelCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        requested_extras = frozenset(requirement.extras)
        for candidate in accepted:
            from_cache = False
            cache_hashes: dict[str, str] | None = None
            local_path = self.local_path_for(candidate)
            path = self.ensure_local_text(candidate, local_path=local_path)
            source_hashes = dict(candidate.link.hashes)
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
                    from cpip.build.build import build_wheel_from_source

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
                    remove_temp_directory_internal(materialized_vcs_path)
                continue
            seen.add(key)
            candidates.append(wheel)
            if materialized_vcs_path is not None:
                remove_temp_directory_internal(materialized_vcs_path)
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
