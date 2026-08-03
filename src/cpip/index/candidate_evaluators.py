"""Candidate filtering and ranking for the package finder."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import TypeVar

from cpip.core.errors import InvalidWheelFilename
from cpip.core.hashes import Hashes
from cpip.core.packaging import Requirement, SpecifierSet, Version, is_windows_path
from cpip.core.release_control import ReleaseControl
from cpip.core.target_python import TargetPython, get_supported
from cpip.core.wheel import TargetContext, Wheel, WheelTag, legacy_build_tag
from cpip.index.candidate_filters import (
    allowed_hashes,
    filter_unallowed_hashes,
    supported_tag_ranks,
)
from cpip.index.candidates import BestCandidateResult, InstallationCandidate
from cpip.index.links import Link
from cpip.index.source_models import (
    INSTALLABLE_ARTIFACT_KINDS,
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateRecord,
    RejectedCandidate,
    RejectionReason,
)

CandidateT = TypeVar("CandidateT", bound=CandidateRecord)


class CandidateEvaluator:
    __slots__ = (
        "allowed_hashes_internal",
        "hashes_internal",
        "prefer_binary_internal",
        "project_name_internal",
        "release_control_internal",
        "specifier_internal",
        "supported_tag_ranks",
        "supported_tags_internal",
    )

    def __init__(
        self,
        project_name: str,
        *,
        supported_tags: Sequence[WheelTag],
        specifier: SpecifierSet,
        release_control: ReleaseControl | None = None,
        prefer_binary: bool = False,
        hashes: Hashes | None = None,
    ) -> None:
        self.project_name_internal = project_name
        self.supported_tags_internal = tuple(supported_tags)
        self.supported_tag_ranks = supported_tag_ranks(self.supported_tags_internal)
        self.specifier_internal = specifier
        self.release_control_internal = release_control
        self.prefer_binary_internal = prefer_binary
        self.hashes_internal = hashes
        self.allowed_hashes_internal = allowed_hashes(hashes)

    @classmethod
    def create(
        cls,
        project_name: str,
        *,
        target_python: TargetPython | None = None,
        target: TargetContext | None = None,
        release_control: ReleaseControl | None = None,
        prefer_binary: bool = False,
        specifier: SpecifierSet | None = None,
        hashes: Hashes | None = None,
    ) -> CandidateEvaluator:
        if target_python is None and target is None:
            supported_tags = get_supported()
        elif target is not None:
            supported_tags = get_supported(
                version=target.python_version,
                platforms=list(target.platforms),
                impl=target.implementation,
                abis=list(target.abis),
            )
        else:
            assert target_python is not None
            supported_tags = target_python.get_sorted_tags()
        return cls(
            project_name,
            supported_tags=supported_tags,
            specifier=specifier if specifier is not None else SpecifierSet(),
            release_control=release_control,
            prefer_binary=prefer_binary,
            hashes=hashes,
        )

    def get_applicable_candidates(
        self,
        candidates: list[CandidateT],
    ) -> list[CandidateT]:
        allow_prereleases = self.allow_prereleases_internal()
        if allow_prereleases is None:
            specifier_allows_prereleases = any(
                spec.operator != "==="
                and not spec.version.endswith(".*")
                and spec.parsed_version.is_prerelease
                for spec in self.specifier_internal.specifiers
            )
            if specifier_allows_prereleases:
                applicable = [
                    candidate
                    for candidate in candidates
                    if self.specifier_internal.contains(
                        candidate.version,
                        allow_prereleases=True,
                    )
                ]
            else:
                stable = [
                    candidate
                    for candidate in candidates
                    if not candidate.version.is_prerelease
                    and self.specifier_internal.contains(candidate.version)
                ]
                if stable:
                    applicable = stable
                else:
                    applicable = [
                        candidate
                        for candidate in candidates
                        if self.specifier_internal.contains(
                            candidate.version,
                            allow_prereleases=True,
                        )
                    ]
        else:
            applicable = [
                candidate
                for candidate in candidates
                if not (candidate.version.is_prerelease and not allow_prereleases)
                and self.specifier_internal.contains(
                    candidate.version,
                    allow_prereleases=allow_prereleases,
                )
            ]
        return filter_unallowed_hashes(
            applicable,
            hashes=self.hashes_internal,
            project_name=self.project_name_internal,
        )

    def allow_prereleases_internal(self) -> bool | None:
        if self.release_control_internal is None:
            return None
        return self.release_control_internal.allows_prereleases(
            self.project_name_internal,
        )

    @staticmethod
    def evaluate_link(
        link: Link,
        requirement: Requirement,
        *,
        allow_yanked: bool,
        allow_binary: bool,
        allow_source: bool,
        target: TargetContext | None,
    ) -> InstallationCandidate | RejectedCandidate:
        parsed = InstallationCandidate.from_link(link, target=target)
        return CandidateEvaluator.evaluate_parsed_link(
            link,
            parsed,
            requirement,
            allow_yanked=allow_yanked,
            allow_binary=allow_binary,
            allow_source=allow_source,
        )

    @staticmethod
    def evaluate_parsed_link(
        link: Link,
        parsed: CandidateRecord | RejectedCandidate,
        requirement: Requirement,
        *,
        allow_yanked: bool,
        allow_binary: bool,
        allow_source: bool,
    ) -> CandidateRecord | RejectedCandidate:
        """Apply requirement-specific policy to an already parsed link."""
        if link.kind is ArtifactKind.WHEEL and not allow_binary:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "binary distributions are disabled",
            )
        if link.kind in SOURCE_ARTIFACT_KINDS and not allow_source:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "source distributions are disabled",
            )
        if isinstance(parsed, RejectedCandidate):
            # Direct archive URLs often have non-distribution filenames (for
            # example GitHub's ``master.zip``).  Keep those installable by
            # deferring identity/version discovery to the build step, but do
            # not mask invalid source-tree metadata this way.
            if (
                CandidateEvaluator.is_unnamed_direct_requirement(requirement)
                and link.kind is ArtifactKind.SDIST
                and parsed.reason is RejectionReason.INVALID_VERSION
            ):
                parsed = CandidateRecord(
                    name=requirement.name,
                    version=Version("0"),
                    link=link,
                )
            else:
                return parsed
        if not CandidateEvaluator.is_unnamed_direct_requirement(requirement) and (
            parsed.canonical_name != requirement.canonical_name
        ):
            return CandidateEvaluator.reject(
                link,
                RejectionReason.DIFFERENT_PROJECT,
                f"wrong project name: {parsed.name}",
            )
        # A direct source URL with a non-distribution filename is represented
        # by the placeholder version 0 until its build metadata is available.
        # Applying an exact constraint to that placeholder rejects the source
        # before it can be built and inspected.
        unknown_direct_source_version = (
            CandidateEvaluator.is_unnamed_direct_requirement(requirement)
            and link.kind in SOURCE_ARTIFACT_KINDS
            and parsed.version == Version("0")
        )
        if not unknown_direct_source_version and not requirement.is_satisfied_by(
            parsed.version,
        ):
            return CandidateEvaluator.reject(
                link,
                RejectionReason.VERSION_MISMATCH,
                f"{parsed.version} does not satisfy {requirement.specifier}",
            )
        if link.requires_python:
            try:
                if not CandidateEvaluator.requires_python_matches(link.requires_python):
                    return CandidateEvaluator.reject(
                        link,
                        RejectionReason.REQUIRES_PYTHON,
                        f"requires Python {link.requires_python}",
                    )
            except ValueError:
                return CandidateEvaluator.reject(
                    link,
                    RejectionReason.REQUIRES_PYTHON,
                    f"invalid Requires-Python: {link.requires_python}",
                )
        if link.is_yanked and not (
            allow_yanked or CandidateEvaluator.is_exact_pin(requirement)
        ):
            return CandidateEvaluator.reject(
                link,
                RejectionReason.YANKED,
                link.yanked_reason or "yanked",
            )
        if link.kind is ArtifactKind.WHEEL and parsed.tag_rank is None:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_WHEEL,
                "wheel tags are not supported by this interpreter",
            )
        if link.kind not in INSTALLABLE_ARTIFACT_KINDS:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                f"{link.kind.value} candidates are not installable yet",
            )
        return parsed

    @staticmethod
    def is_exact_pin(requirement: Requirement) -> bool:
        return any(
            spec.operator in {"==", "==="} and not spec.version.endswith(".*")
            for spec in requirement.specifier.specifiers
        )

    @staticmethod
    @lru_cache(maxsize=128)
    def requires_python_matches(requires_python: str) -> bool:
        import platform

        return SpecifierSet(requires_python).contains(platform.python_version())

    @staticmethod
    def is_unnamed_direct_requirement(requirement: Requirement) -> bool:
        if requirement.name == "editable-placeholder" and requirement.url is not None:
            return True
        if requirement.url is not None:
            return True
        if requirement.raw.startswith("file:"):
            return True
        return requirement.raw.startswith((".", "/", "~")) or is_windows_path(
            requirement.raw,
        )

    @staticmethod
    def reject(link: Link, reason: RejectionReason, detail: str) -> RejectedCandidate:
        return RejectedCandidate(link=link, reason=reason, detail=detail)

    def compute_best_candidate(
        self,
        candidates: list[InstallationCandidate],
    ) -> BestCandidateResult:
        applicable = self.get_applicable_candidates(candidates)
        best = self.sort_best_candidate(applicable)
        return BestCandidateResult(candidates, applicable, best)

    def sort_best_candidate(
        self,
        candidates: list[InstallationCandidate],
    ) -> InstallationCandidate | None:
        if not candidates:
            return None
        return max(candidates, key=self.sort_key_internal)

    def sort_key_internal(
        self,
        candidate: InstallationCandidate,
    ) -> tuple[int, int, Version, int, int, int, int, tuple[int, str] | tuple[()]]:
        digest = None
        if candidate.link.hashes is not None:
            digest = candidate.link.hashes.get("sha256")
        allowed = self.allowed_hashes_internal
        hash_rank = int(bool(allowed and digest in allowed))
        yanked_rank = -1 if candidate.link.is_yanked else 0
        wheel_rank = 0
        egg_fragment_rank = 1
        tag_rank = -1_000_000
        build_tag: tuple[int, str] | tuple[()] = ()
        if candidate.wheel is not None:
            wheel_rank = 1
            supported_matches = (
                rank
                for file_tag in candidate.wheel.tags
                if (rank := self.supported_tag_ranks.get(str(file_tag).lower()))
                is not None
            )
            best_rank = min(supported_matches, default=None)
            if best_rank is not None:
                tag_rank = -best_rank
            build_tag = legacy_build_tag(candidate.wheel.build_tag)
        elif (
            candidate.link.kind is ArtifactKind.WHEEL
            or candidate.link.filename.endswith(".whl")
        ):
            try:
                wheel = Wheel(candidate.link.filename)
                wheel_rank = 1
                supported_matches = (
                    rank
                    for file_tag in wheel.file_tags
                    if (rank := self.supported_tag_ranks.get(str(file_tag).lower()))
                    is not None
                )
                best_rank = min(supported_matches, default=None)
                if best_rank is not None:
                    tag_rank = -best_rank
                build_tag = wheel.build_tag
            except InvalidWheelFilename:
                pass
        if candidate.link.egg_fragment is not None:
            egg_fragment_rank = 0
        binary_preference = wheel_rank if self.prefer_binary_internal else 0
        return (
            hash_rank,
            yanked_rank,
            candidate.version,
            binary_preference,
            wheel_rank,
            egg_fragment_rank,
            tag_rank,
            build_tag,
        )
