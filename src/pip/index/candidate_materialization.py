"""Build, cache, and materialize resolved package candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, overload

from pip.core.errors import BuildError, UnsupportedWheel
from pip.core.packaging import (
    Requirement,
    Version,
    canonicalize_name,
    parse_requirement,
)
from pip.core.wheel import WheelCandidate, parse_wheel, wheel_candidate
from pip.index.artifacts import ArtifactLocator
from pip.index.cache import origin_hashes, wheel_cache_path
from pip.index.candidates import InstallationCandidate
from pip.index.links import Link
from pip.index.source_models import ArtifactKind
from pip.index.vcs import is_immutable_vcs_link, vcs_reference, vcs_scheme

logger = logging.getLogger(__name__)


class CandidateStream(Sequence[WheelCandidate]):
    """A replayable sequence that materializes candidates on demand."""

    def __init__(self, source: Iterator[WheelCandidate]) -> None:
        self._source = source
        self._items: list[WheelCandidate] = []
        self._exhausted = False
        self._error: Exception | None = None

    def _advance(self) -> bool:
        if self._error is not None:
            raise self._error
        if self._exhausted:
            return False
        try:
            item = next(self._source)
        except StopIteration:
            self._exhausted = True
            return False
        except Exception as exc:
            self._error = exc
            raise
        self._items.append(item)
        return True

    def __iter__(self) -> Iterator[WheelCandidate]:
        index = 0
        while True:
            if index < len(self._items):
                yield self._items[index]
                index += 1
                continue
            if not self._advance():
                return

    def __bool__(self) -> bool:
        return bool(self._items) or self._advance()

    def __len__(self) -> int:
        while self._advance():
            pass
        return len(self._items)

    @overload
    def __getitem__(self, index: int) -> WheelCandidate: ...

    @overload
    def __getitem__(self, index: slice) -> list[WheelCandidate]: ...

    def __getitem__(
        self, index: int | slice
    ) -> WheelCandidate | list[WheelCandidate]:
        if isinstance(index, slice):
            if (
                index.stop is None
                or (index.start is not None and index.start < 0)
                or index.stop < 0
                or (index.step is not None and index.step < 0)
            ):
                len(self)
            else:
                while len(self._items) < index.stop and self._advance():
                    pass
            return self._items[index]
        if index < 0:
            len(self)
        else:
            while len(self._items) <= index and self._advance():
                pass
        return self._items[index]

    def prefer(
        self,
        keep: Callable[[WheelCandidate], bool],
        *,
        decisive: Callable[[WheelCandidate], bool] | None = None,
    ) -> CandidateStream:
        """Prefer matching candidates, falling back to the full stream if none do."""

        decisive = decisive or keep

        def generate() -> Iterator[WheelCandidate]:
            buffered: list[WheelCandidate] = []
            preference_found = False
            for candidate in self:
                if preference_found:
                    if keep(candidate):
                        yield candidate
                    continue
                buffered.append(candidate)
                if decisive(candidate):
                    preference_found = True
                    yield from (item for item in buffered if keep(item))
                    buffered.clear()
            if not preference_found:
                yield from buffered

        return CandidateStream(generate())


def emit_build_message(message: str) -> None:
    if not os.environ.get("PIP_QUIET"):
        print(message)


def source_hashes_for_link(link: Link) -> dict[str, str]:
    hashes = dict(link.hashes)
    if hashes:
        return hashes
    local = ArtifactLocator().local_path(link.url)
    if local is not None and local.is_file():
        try:
            return {"sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
        except OSError:
            return {}
    return {}


def _cache_identity(url: str) -> str:
    """Return a stable cache identity for an artifact URL.

    VCS URLs may carry package-name and subdirectory fragments, and callers
    may spell the VCS scheme with or without the ``+`` prefix.  Those details
    do not change the source at an immutable revision, so they must not create
    separate built-wheel cache entries.
    """
    if is_immutable_vcs_link(url):
        reference = vcs_reference(url)
        return f"{reference.vcs}+{reference.repo_url}@{reference.requested_revision}"
    return url


class CandidateMaterializer:
    def __init__(
        self,
        *,
        build_options: dict[str, dict[str, object]] | None = None,
        build_constraints: list[str] | None = None,
        wheel_cache_dir: Path | None = None,
        build_isolation: bool = True,
        session: Any = None,
    ) -> None:
        self.build_options = build_options
        self.build_constraints = build_constraints
        self.wheel_cache_dir = wheel_cache_dir
        self.build_isolation = build_isolation
        self.artifacts = ArtifactLocator(session)
        self.invalid_links: set[str] = set()

    def materialize(
        self,
        requirement: Requirement,
        accepted: tuple[InstallationCandidate, ...],
    ) -> CandidateStream:
        return CandidateStream(self._iter_materialize(requirement, accepted))

    def _iter_materialize(
        self,
        requirement: Requirement,
        accepted: tuple[InstallationCandidate, ...],
    ) -> Iterator[WheelCandidate]:
        candidates: list[WheelCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in accepted:
            from_cache = False
            cache_hashes: dict[str, str] | None = None
            path = self.artifacts.ensure_local(candidate.link.url)
            source_hashes = dict(candidate.link.hashes)
            if (
                not source_hashes
                and candidate.link.is_file
                and Path(candidate.link.file_path).is_file()
            ):
                source_hashes["sha256"] = hashlib.sha256(
                    Path(candidate.link.file_path).read_bytes()
                ).hexdigest()
            materialized_vcs_path = (
                path if vcs_scheme(candidate.link.url) is not None else None
            )
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
            if candidate.link.kind in {ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE}:
                display_name = (
                    requirement.name
                    if canonicalize_name(requirement.name) == candidate.canonical_name
                    else candidate.name
                )
                if requirement.name is None:
                    display_name = candidate.name
                cached = _cached_wheel_for_link(
                    self.wheel_cache_dir, candidate.link.url
                )
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
                    _validate_build_requirements(path)
                    key = requirement.raw
                    from pip.build.build import build_wheel_from_source

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
                        _cache_built_wheel(self.wheel_cache_dir, candidate, path)
            if candidate.link.kind is ArtifactKind.WHEEL:
                try:
                    with zipfile.ZipFile(path) as archive:
                        parse_wheel(archive, Path(path).name[:-4].split("-", 1)[0])
                except UnsupportedWheel as exc:
                    if ".dist-info directory" not in str(exc):
                        self.invalid_links.add(candidate.link.url)
                        logger.warning("%s", exc)
                        continue
                    raise
            try:
                built = wheel_candidate(path, set(requirement.extras))
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
                source_vcs=vcs_scheme(candidate.link.url),
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
                    shutil.rmtree(materialized_vcs_path, ignore_errors=True)
                continue
            seen.add(key)
            candidates.append(wheel)
            if materialized_vcs_path is not None:
                shutil.rmtree(materialized_vcs_path, ignore_errors=True)
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


def _validate_build_requirements(source: Path) -> None:
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        return
    import tomllib

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
            if not req.is_satisfied_by(minimum):
                raise BuildError(
                    f"Some build dependencies for {source.as_uri()} conflict with PEP 517/518 supported requirements: "
                    "setuptools==1.0 is incompatible with setuptools>=40.8.0."
                )


def _cached_wheel_for_link(
    wheel_cache_dir: Path | None, url: str
) -> tuple[Path, dict[str, str] | None] | None:
    if wheel_cache_dir is None:
        return None
    entry_dir = wheel_cache_path(wheel_cache_dir, _cache_identity(url))
    if not entry_dir.is_dir():
        return None
    wheels = sorted(entry_dir.glob("*.whl"))
    if not wheels:
        return None
    return wheels[0], origin_hashes(entry_dir / "origin.json")


def _cache_built_wheel(
    wheel_cache_dir: Path | None, candidate: InstallationCandidate, wheel: Path
) -> None:
    if wheel_cache_dir is None:
        return
    entry_dir = wheel_cache_path(wheel_cache_dir, _cache_identity(candidate.link.url))
    entry_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel, entry_dir / wheel.name)
    origin = {
        "archive_info": {
            "hashes": source_hashes_for_link(candidate.link),
        }
    }
    (entry_dir / "origin.json").write_text(json.dumps(origin), encoding="utf-8")
