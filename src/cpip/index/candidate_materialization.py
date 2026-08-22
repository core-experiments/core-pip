"""Build, cache, and materialize resolved package candidates."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
from itertools import chain, islice
from threading import RLock
from typing import NamedTuple

from cpip.build.build import build_wheel_from_source, unpack_source_internal
from cpip.core.errors import BuildError, InstallationError, UnsupportedWheel
from cpip.core.packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
)
from cpip.core.versions import Version, ZERO_VERSION
from cpip.core.temp_dir import remove_temp_directory
from cpip.core.urls import path_to_url
from cpip.core.wheel import (
    LazyWheelLayout,
    WheelCandidate,
    validate_wheel_with_metadata,
    wheel_candidate,
    wheel_candidate_from_path,
    wheel_dist_info_dir,
)
from cpip.core.wheel_metadata import parse_metadata_headers
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
from cpip.resolution.archive import WheelArchive, WheelhouseUnavailable

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator
    from typing import Any

logger = logging.getLogger(__name__)


_EXTRA_MARKER_RE = re.compile(r"extra\s*(?:==|in)\s*['\"]([^'\"]+)['\"]")

_METADATA_WORKERS = 32


class _ArchiveMemberInfo(NamedTuple):
    compress_type: int

    CRC: int

    compress_size: int

    file_size: int

    header_offset: int

    external_attr: int


class _ResolverWheelArchive:
    """ZipFile-shaped adapter over :class:`WheelArchive`.

    A wrong-package backtracking resolve can open thousands of candidate
    wheels just to read their METADATA -- and ``zipfile.ZipFile.__init__``
    unconditionally builds a full ``ZipInfo`` for every member of each one,
    cost that's unrelated to the handful of headers resolution actually
    reads. ``WheelArchive`` (already relied on by the install-time raw
    archive reader) does the same central-directory scan without that
    per-member object construction, and only for the common case a wheel's
    zip always is -- non-zip64, non-encrypted, deflate/stored -- so this
    adapter is only ever handed to :func:`wheel_dist_info_dir`,
    :func:`wheel_archive_identity`, and :func:`wheel_candidate`, the same
    trio a real ``zipfile.ZipFile`` already serves here.
    """

    __slots__ = ("NameToInfo", "_archive")

    def __init__(self, archive: WheelArchive) -> None:
        self._archive = archive

        modes = archive.modes

        self.NameToInfo = {
            name: _ArchiveMemberInfo(*member, modes.get(name, 0))
            for name, member in archive.members.items()
        }

    def getinfo(self, name: str) -> _ArchiveMemberInfo:
        try:
            return self.NameToInfo[name]

        except KeyError:
            raise KeyError(f"There is no item named {name!r} in the archive") from None

    def read(self, name: str) -> bytes:
        if name not in self.NameToInfo:
            raise KeyError(f"There is no item named {name!r} in the archive")

        try:
            return self._archive.read(name)

        except WheelhouseUnavailable as exc:
            raise zipfile.BadZipFile(f"Bad archive member {name!r}: {exc}") from exc

    def namelist(self) -> list[str]:
        return list(self.NameToInfo)

    def open(self, name: str) -> io.BytesIO:
        return io.BytesIO(self.read(name))

    def close(self) -> None:
        self._archive.file.close()

    def __enter__(self) -> _ResolverWheelArchive:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _open_resolver_wheel_archive(
    path_text: str,
) -> _ResolverWheelArchive | zipfile.ZipFile:
    """Open a wheel for metadata-only reads, preferring the faster reader.

    Falls back to a real ``zipfile.ZipFile`` for anything ``WheelArchive``
    doesn't cover (zip64, encryption, an unusual compression method, or any
    other parsing surprise) -- so this only ever costs the speedup, never
    correctness.
    """

    try:
        # Unbuffered on purpose: WheelArchive's access pattern is one tail
        # read plus exact-size seek+read pairs, so a BufferedReader's
        # construction cost and readahead (discarded on every seek) are
        # pure per-open overhead. Its short-read semantics are already
        # what this reader treats as truncation.
        file = open(path_text, "rb", buffering=0)  # noqa: SIM115

        archive = WheelArchive(file)

        if any(member[0] not in {0, 8} for member in archive.members.values()):
            # A compression method the raw reader cannot decode (bzip2,
            # lzma): read() would fail on it later, so hand the whole
            # wheel to zipfile now instead of reporting it invalid.
            file.close()

            return zipfile.ZipFile(path_text)

    except (OSError, ValueError, WheelhouseUnavailable):
        try:
            file.close()

        except UnboundLocalError:
            pass

        # Path form (rather than a passed-in file object) so ZipFile owns
        # and auto-closes its own file handle on __exit__.
        return zipfile.ZipFile(path_text)

    return _ResolverWheelArchive(archive)


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
            # The same identity a directory scan attaches when it has the
            # stat in hand, remembered on the link so it is computed once.
            local_identity = (
                f"stat:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
            )
            candidate.link.local_identity_internal = local_identity
            return local_identity

    return candidate.link.url


def vcs_scheme(url: str) -> str | None:
    return parse_vcs_scheme(url)


class LazyWheelCandidate(WheelCandidate):
    """Resolver candidate whose metadata is cheap and whose wheel is deferred."""

    # WheelCandidate's own slots (name, version, path, ...) go unused here --
    # every one of them is overridden below as a property instead, backed by
    # these six attributes.  A subclass that doesn't declare its own
    # __slots__ gets a plain __dict__ regardless of what the parent
    # declared, silently paying for both: one per candidate streamed during
    # resolution, which is the hot path this class exists for.
    __slots__ = (
        "_record_internal",
        "_version_internal",
        "materialized_internal",
        "materializer_internal",
        "record_loader_internal",
        "requirement_internal",
    )

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
    def metadata_version(self) -> Version:
        """The release the candidate's own metadata declares.

        May differ from ``version`` (the catalog/filename-declared release)
        for a mislabeled or malformed artifact.
        """
        return self.record_internal.metadata().version

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

        self.artifact_fingerprint_cache: dict[str, str] = {}

        self.source_hash_cache: dict[str, dict[str, str] | None] = {}

        self.local_artifacts: dict[str, str] = {}

        self.vcs_revisions: dict[str, str] = {}

        self.metadata_prefetcher: Prefetcher[Any, str] | None = None

        self.metadata_prefetch_lock = RLock()

    def local_path_for(self, candidate: CandidateRecord) -> str | None:
        if not candidate.link.is_file:
            return None

        url = candidate.link.url

        cached = self.local_artifacts.get(url)

        if cached is None:
            cached = candidate.link.file_path

            self.local_artifacts[url] = cached

        return cached

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

                if self.release_is_invalid(candidate):
                    invalid_versions.add(identity)

                    continue

                yield LazyWheelCandidate(candidate, requirement, self)

        return CandidateStream(generate())

    def materialize_one(
        self,
        requirement: Requirement,
        record: CandidateRecord,
    ) -> WheelCandidate | None:
        """One record as the lazy candidate :meth:`materialize` would yield.

        For a caller that already holds the single record it wants -- the
        resolver's forward check reading one release -- without the stream,
        the prefetch decision and the generator a whole selection needs.
        ``None`` when the release is known to be invalid.
        """

        candidate = self.prepare_record(requirement, record)

        if self.release_is_invalid(candidate):
            return None

        return LazyWheelCandidate(candidate, requirement, self)

    def release_is_invalid(self, candidate: CandidateRecord) -> bool:
        """Whether the release was recorded as unusable by an earlier run."""

        cache = self.persistent_release_facts_cache

        if cache is None or cache.get(self.negative_fact_key(candidate)) is None:
            return False

        self.invalid_links.add(candidate.link.url)

        return True

    def negative_fact_key(self, candidate: CandidateRecord) -> tuple[str, str, str]:
        return (
            candidate.canonical_name,
            candidate.version.public,
            self.artifact_fingerprint(candidate),
        )

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
                self.metadata_prefetcher.submit(key, url)

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
            cached = self.metadata_cache.get(key)

            if cached is not None:
                return cached

            if self.persistent_candidate_metadata_cache is not None:
                cached = self.persistent_candidate_metadata_cache.get(persistent_key)

                if cached is not None:
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
                # Only source-tree/sdist candidates reach this; wheel
                # candidates (the common case) never need build_backend's
                # much heavier import chain.
                from cpip.build.build_backend import prepare_project_metadata

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

                        def remember_wheel_if_reusable(wheel_path: str) -> None:
                            # A backend without the optional
                            # prepare_metadata_for_build_wheel hook makes
                            # this metadata read build a full wheel and
                            # throw it away. If this candidate later wins
                            # the resolve, materialize() would otherwise
                            # build the exact same wheel again from
                            # scratch -- cache it here under the same key
                            # materialize()'s own cached_wheel_for_link()
                            # check already looks for, so a later build is
                            # skipped for free. Only for the same
                            # deterministic sources materialize() itself
                            # caches (a plain sdist, or an immutable VCS
                            # pin) -- a mutable ref could change by the
                            # time it's actually installed.
                            if candidate.link.kind is ArtifactKind.SDIST or (
                                candidate.link.kind is ArtifactKind.SOURCE_TREE
                                and is_immutable_vcs_link(candidate.link.url)
                            ):
                                store_cached_wheel(
                                    self.wheel_cache_dir,
                                    candidate,
                                    wheel_path,
                                )

                        try:
                            project = prepare_project_metadata(
                                path,
                                build_constraints=self.build_constraints,
                                build_isolation=self.build_isolation,
                                on_wheel_built=remember_wheel_if_reusable,
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
                with _open_resolver_wheel_archive(path_text) as archive:
                    try:
                        # Locating the .dist-info directory is all resolution
                        # needs.  Reading WHEEL for its ``Wheel-Version`` costs
                        # a second member decompression per candidate, and
                        # ``include_layout=False`` discards the text anyway;
                        # ``wheel_transaction`` re-validates before installing.
                        dist_info_dir = wheel_dist_info_dir(
                            archive,
                            os.path.basename(path_text)[:-4].split("-", 1)[0],
                        )

                    except UnsupportedWheel as exc:
                        raise InstallationError(str(exc)) from exc

                    built = wheel_candidate(
                        path_text,
                        requested_extras,
                        archive=archive,
                        filename_info=(candidate.name, candidate.version),
                        dist_info_dir=dist_info_dir,
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

    def candidate_from_loaded_metadata(
        self,
        candidate: CandidateRecord,
        path: str,
    ) -> WheelCandidate | None:
        """A local wheel's concrete candidate without reopening the wheel.

        The resolver loaded this record's metadata (for the same extras) to
        decide on it; the concrete candidate repeats it. What the install
        may still need from the file -- its layout -- is computed only when
        a consumer asks, which a warm install whose tree is in the archive
        cache never does.
        """

        loader = candidate.metadata_loader

        if loader is None:
            return None

        try:
            metadata = loader.load()

        except Exception:  # noqa: BLE001 - the full path reports the error
            return None

        return WheelCandidate(
            name=metadata.name,
            version=metadata.version,
            path=path,
            dependencies=metadata.dependencies,
            provided_extras=metadata.provided_extras,
            requires_python=metadata.requires_python,
            wheel_layout=LazyWheelLayout(lambda: self.wheel_layout_for(path)),
        )

    @staticmethod
    def wheel_layout_for(path: str) -> object | None:
        """Read a wheel's layout the way eager materialization does."""

        try:
            with _open_resolver_wheel_archive(path) as archive:
                dist_info_dir, wheel_metadata_text = validate_wheel_with_metadata(
                    archive,
                    os.path.basename(os.fspath(path))[:-4].split("-", 1)[0],
                )

                return wheel_candidate(
                    path,
                    archive=archive,
                    dist_info_dir=dist_info_dir,
                    wheel_metadata_text=wheel_metadata_text,
                ).wheel_layout

        except (OSError, UnsupportedWheel, InstallationError):
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

                    if built is None and candidate.link.is_file:
                        built = self.candidate_from_loaded_metadata(
                            candidate,
                            path,
                        )

                        if built is not None:
                            self.wheel_candidates[cache_key] = built

                    if built is None:
                        # The same fast reader the resolver used for this
                        # wheel's metadata: zipfile.ZipFile would build a
                        # ZipInfo per member just to hand back the layout
                        # that WheelArchive's central-directory scan already
                        # holds (modes included). Falls back to zipfile
                        # itself for anything WheelArchive declines.
                        with _open_resolver_wheel_archive(path) as archive:
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
                    built = wheel_candidate_from_path(path, requested_extras)

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

            if built.version != candidate.version and candidate.version != ZERO_VERSION:
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
                # The layout is what lets open_wheel_archive read this wheel
                # again without another central-directory scan; carried as
                # stored so a lazy one stays unread here.
                wheel_layout=built.stored_wheel_layout,
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

            _, upper_bound = req.specifier.bounds

            if upper_bound is not None and (
                upper_bound[0] < minimum
                or (upper_bound[0] == minimum and not upper_bound[1])
            ):
                raise BuildError(
                    f"Some build dependencies for {path_to_url(os.path.abspath(os.fspath(source)))} conflict with PEP 517/518 supported requirements: "
                    "setuptools==1.0 is incompatible with setuptools>=40.8.0,<82.",
                )
