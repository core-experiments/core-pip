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
from pathlib import Path
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
from cpip.core.wheel import WheelCandidate, validate_wheel, wheel_candidate
from cpip.index.candidate_cache import (
    cached_wheel_for_link,
    emit_build_message,
    cache_built_wheel as store_cached_wheel,
)
from cpip.index.candidate_stream import CandidateStream
from cpip.index.source_models import (
    ArtifactKind,
    CandidateMetadata,
    CandidateRecord,
    LazyCandidateMetadata,
    SOURCE_ARTIFACT_KINDS,
)
from cpip.index.metadata_cache import get_wheel_metadata_cache

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
    project: object, requested_extras: frozenset[str]
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
                    self.requirement_internal, (self.record_internal,)
                )
            )
            if not candidates:
                raise BuildError(
                    f"Unable to materialize candidate {self.record_internal.name}"
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
    def path(self) -> Path:
        if (
            self.materializer_internal.dry_run
            and self.record_internal.link.kind in SOURCE_ARTIFACT_KINDS
        ):
            return self.materializer_internal.ensure_local(
                self.record_internal,
                local_path=(
                    Path(self.record_internal.link.file_path)
                    if self.record_internal.link.is_file
                    else None
                ),
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
        hashes = self.record_internal.link.hashes
        if hashes:
            return dict(hashes)
        if self.record_internal.link.kind in SOURCE_ARTIFACT_KINDS:
            local = self.materializer_internal.ensure_local(
                self.record_internal,
                local_path=(
                    Path(self.record_internal.link.file_path)
                    if self.record_internal.link.is_file
                    else None
                ),
            )
            if self.record_internal.link.is_vcs:
                remove_temp_directory_internal(local)
                return None
            if local.is_file():
                return {"sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
            return None
        return None

    @property
    def source_kind(self) -> str:
        return self.record_internal.link.kind.value

    @property
    def source_vcs(self) -> str | None:
        if not self.record_internal.link.is_vcs:
            return None
        return vcs_scheme(self.record_internal.link.url)

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
        wheel_cache_dir: Path | None = None,
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
        self.artifacts = None
        self.invalid_links: set[str] = set()
        self.wheel_candidates: dict[
            tuple[str, int, int, frozenset[str]], WheelCandidate
        ] = {}
        self.metadata_cache: dict[tuple[str, frozenset[str]], CandidateMetadata] = {}
        self.local_artifacts: dict[str, Path] = {}

    def ensure_local(
        self,
        candidate: CandidateRecord,
        *,
        local_path: Path | None = None,
    ) -> Path:
        if not candidate.link.is_vcs:
            cached = self.local_artifacts.get(candidate.link.url)
            if cached is not None:
                return cached
        if candidate.link.is_file:
            path = local_path or Path(candidate.link.file_path)
            self.local_artifacts[candidate.link.url] = path
            return path
        if self.artifacts is None:
            from cpip.index.artifacts import ArtifactLocator

            self.artifacts = ArtifactLocator(self.session)
        path = self.artifacts.ensure_local(
            candidate.link.url,
            is_vcs=candidate.link.is_vcs,
            local_path=local_path,
        )
        if not candidate.link.is_vcs:
            self.local_artifacts[candidate.link.url] = path
        return path

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
                            f"'{metadata.version}'"
                        )
                        if requirement.extras:
                            print(
                                f"Requested {requirement.raw or requirement.name}, "
                                f"but installing version {metadata.version}"
                            )
                        self.invalid_links.add(candidate.link.url)
                        invalid_versions.add(identity)
                        continue
                yield LazyWheelCandidate(candidate, requirement, self)

        return CandidateStream(generate())

    def metadata_loader(
        self, candidate: CandidateRecord, requirement: Requirement
    ) -> LazyCandidateMetadata:
        requested_extras = frozenset(requirement.extras)
        key = (candidate.link.url, requested_extras)

        def load() -> CandidateMetadata:
            cached = self.metadata_cache.get(key)
            if cached is not None:
                return cached
            local_path = (
                Path(candidate.link.file_path) if candidate.link.is_file else None
            )
            path = self.ensure_local(candidate, local_path=local_path)
            vcs_path = path if candidate.link.is_vcs else None
            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                if (
                    candidate.link.kind is ArtifactKind.SOURCE_TREE
                    and candidate.link.subdirectory_fragment
                ):
                    path = path / candidate.link.subdirectory_fragment
                with tempfile.TemporaryDirectory(prefix="cpip-metadata-") as temp_dir:
                    if path.is_file():
                        from cpip.build.build import unpack_source

                        path = unpack_source(path, Path(temp_dir))
                    validate_build_requirements(path)
                    from cpip.index.candidates import prepare_project_metadata

                    try:
                        project = prepare_project_metadata(
                            path,
                            build_constraints=self.build_constraints,
                            build_isolation=self.build_isolation,
                        )
                    except BuildError as exc:
                        metadata = self.pypi_metadata(
                            candidate, requested_extras
                        )
                        if metadata is None:
                            raise BuildError(
                                f"Failed to build '{candidate.name}': {exc}"
                            ) from exc
                    else:
                        metadata = CandidateMetadata(
                            name=project.name,
                            version=Version(project.version),
                            dependencies=project_dependencies(
                                project, requested_extras
                            ),
                            provided_extras=project_provided_extras(project),
                            requires_python=project.requires_python,
                        )
                if vcs_path is not None:
                    remove_temp_directory_internal(vcs_path)
            else:
                with (
                    path.open("rb", buffering=32768) as stream,
                    zipfile.ZipFile(stream) as archive,
                ):
                    try:
                        dist_info_dir = validate_wheel(
                            archive, Path(path).name[:-4].split("-", 1)[0]
                        )
                    except UnsupportedWheel as exc:
                        raise InstallationError(str(exc)) from exc
                    built = wheel_candidate(
                        path,
                        set(requested_extras),
                        archive=archive,
                        filename_info=(candidate.name, candidate.version),
                        dist_info_dir=dist_info_dir,
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
            return metadata

        return LazyCandidateMetadata(load)

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
                if marker_applies(requirement.marker, extras=requested_extras)
            )
            extras = frozenset(info.get("provides_extra") or ())
            return CandidateMetadata(
                name=str(info["name"]),
                version=Version(str(info["version"])),
                dependencies=dependencies,
                provided_extras=extras,
                requires_python=info.get("requires_python"),
            )
        except (KeyError, OSError, TypeError, ValueError):
            return None

    def iter_materialize(
        self,
        requirement: Requirement,
        accepted: tuple[CandidateRecord, ...],
    ) -> Generator[WheelCandidate, None, list[WheelCandidate]]:
        from cpip.index.vcs import is_immutable_vcs_link

        candidates: list[WheelCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in accepted:
            requested_extras = frozenset(requirement.extras)
            from_cache = False
            cache_hashes: dict[str, str] | None = None
            local_path = (
                Path(candidate.link.file_path) if candidate.link.is_file else None
            )
            path = self.ensure_local(candidate, local_path=local_path)
            source_hashes = dict(candidate.link.hashes)
            if (
                self.compute_source_hashes
                and not source_hashes
                and local_path is not None
                and local_path.is_file()
            ):
                source_hashes["sha256"] = hashlib.sha256(
                    local_path.read_bytes()
                ).hexdigest()
            materialized_vcs_path = path if candidate.link.is_vcs else None
            if (
                candidate.link.kind is ArtifactKind.SOURCE_TREE
                and candidate.link.subdirectory_fragment
            ):
                path = path / candidate.link.subdirectory_fragment
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
                    cached_name = Path(path).name.split("-", 1)[0]
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
                            f"Failed to build '{display_name}': {exc}"
                        ) from exc
                    emit_build_message(f"Created wheel for {display_name}")
                    emit_build_message(f"Successfully built {display_name}")
                    if cache_built_wheel:
                        logger.debug(
                            "store cached built wheel for %s from %s",
                            display_name,
                            candidate.link.url,
                        )
                        store_cached_wheel(
                            self.wheel_cache_dir, candidate, path
                        )
            try:
                if candidate.link.kind is ArtifactKind.WHEEL:
                    try:
                        stat = path.stat()
                    except OSError:
                        stat = None
                    cache_key = (
                        os.fspath(path),
                        stat.st_size if stat is not None else -1,
                        stat.st_mtime_ns if stat is not None else -1,
                        requested_extras,
                    )
                    built = self.wheel_candidates.get(cache_key)
                    if built is None:
                        with (
                            path.open("rb", buffering=32768) as stream,
                            zipfile.ZipFile(stream) as archive,
                        ):
                            dist_info_dir = validate_wheel(
                                archive, Path(path).name[:-4].split("-", 1)[0]
                            )
                            built = wheel_candidate(
                                path,
                                set(requested_extras),
                                archive=archive,
                                filename_info=(candidate.name, candidate.version),
                                dist_info_dir=dist_info_dir,
                            )
                        if stat is not None:
                            self.wheel_candidates[cache_key] = built
                else:
                    built = wheel_candidate(path, set(requested_extras))
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
                    f"'{built.version}'"
                )
                if requirement.extras:
                    print(
                        f"Requested {requirement.raw or requirement.name}, "
                        f"but installing version {built.version}"
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
                    wheel.path.name,
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


def validate_build_requirements(source: Path) -> None:
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        return
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        from cpip._vendor import tomli as tomllib

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(
            f"Invalid PEP 518 build requirements in {pyproject}: {exc}"
        ) from exc
    if "build-system" not in data:
        return
    build_system = data["build-system"]
    if not isinstance(build_system, dict):
        raise BuildError(
            f"Invalid PEP 518 [build-system] table in {pyproject}: mandatory `requires` key is missing"
        )
    if "requires" not in build_system:
        raise BuildError(
            f"Invalid PEP 518 [build-system] table in {pyproject}: mandatory `requires` key is missing"
        )
    requires = build_system.get("requires")
    if not isinstance(requires, list) or not all(
        isinstance(item, str) for item in requires
    ):
        raise BuildError(
            f"Invalid PEP 518 build requirements in {pyproject}: build-system.requires is not a list of strings"
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
                    f"Some build dependencies for {source.as_uri()} conflict with PEP 517/518 supported requirements: "
                    "setuptools==1.0 is incompatible with setuptools>=40.8.0,<82."
                )
