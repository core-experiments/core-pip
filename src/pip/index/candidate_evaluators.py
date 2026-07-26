"""Candidate filtering and ranking for the package finder."""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Sequence
from functools import lru_cache

from pip.core.errors import InvalidWheelFilename
from pip.core.hashes import Hashes
from pip.core.packaging import Requirement, SpecifierSet, Version
from pip.core.release_control import ReleaseControl
from pip.core.target_python import TargetPython, get_supported
from pip.core.wheel import TargetContext, Wheel, WheelTag, legacy_build_tag
from pip.index.candidates import BestCandidateResult, InstallationCandidate
from pip.index.links import Link
from pip.index.source_models import ArtifactKind, RejectedCandidate, RejectionReason

logger = logging.getLogger(__name__)


@lru_cache(maxsize=64)
def supported_tag_ranks(tags: tuple[WheelTag, ...]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for index, tag in enumerate(tags):
        ranks.setdefault(str(tag).lower(), index)
    return ranks


def log_hash_check(message: str, *args: object) -> None:
    logger.debug(message, *args)


class CandidateEvaluator:
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
        self, candidates: list[InstallationCandidate]
    ) -> list[InstallationCandidate]:
        allow_prereleases = self.allow_prereleases_internal()
        if allow_prereleases is None:
            allow_prereleases = not any(
                not candidate.version.is_prerelease
                and self.specifier_internal.contains(candidate.version)
                for candidate in candidates
            )
            allow_prereleases = allow_prereleases or any(
                spec.operator != "==="
                and not spec.version.endswith(".*")
                and Version(spec.version).is_prerelease
                for spec in self.specifier_internal.specifiers
            )
        applicable = [
            candidate
            for candidate in candidates
            if not (candidate.version.is_prerelease and allow_prereleases is False)
            if self.specifier_internal.contains(
                candidate.version, allow_prereleases=allow_prereleases
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
            self.project_name_internal
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
        parsed: InstallationCandidate | RejectedCandidate,
        requirement: Requirement,
        *,
        allow_yanked: bool,
        allow_binary: bool,
        allow_source: bool,
    ) -> InstallationCandidate | RejectedCandidate:
        """Apply requirement-specific policy to an already parsed link."""
        if link.kind is ArtifactKind.WHEEL and not allow_binary:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "binary distributions are disabled",
            )
        if (
            link.kind in {ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE}
            and not allow_source
        ):
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
                parsed = InstallationCandidate(
                    name=requirement.name,
                    version="0",
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
        if not requirement.is_satisfied_by(parsed.version):
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
                link, RejectionReason.YANKED, link.yanked_reason or "yanked"
            )
        if link.kind is ArtifactKind.WHEEL and parsed.tag_rank is None:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_WHEEL,
                "wheel tags are not supported by this interpreter",
            )
        if link.kind not in {
            ArtifactKind.WHEEL,
            ArtifactKind.SDIST,
            ArtifactKind.SOURCE_TREE,
        }:
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
        return requirement.raw.startswith((".", "/", "~"))

    @staticmethod
    def reject(link: Link, reason: RejectionReason, detail: str) -> RejectedCandidate:
        return RejectedCandidate(link=link, reason=reason, detail=detail)

    def compute_best_candidate(
        self, candidates: list[InstallationCandidate]
    ) -> BestCandidateResult:
        applicable = self.get_applicable_candidates(candidates)
        best = self.sort_best_candidate(applicable)
        return BestCandidateResult(candidates, applicable, best)

    def sort_best_candidate(
        self, candidates: list[InstallationCandidate]
    ) -> InstallationCandidate | None:
        if not candidates:
            return None
        return max(candidates, key=self.sort_key_internal)

    def sort_key_internal(
        self, candidate: InstallationCandidate
    ) -> tuple[int, int, Version, int, int, int, int, tuple[int, str] | tuple[()]]:
        digest = None
        if candidate.link.hashes is not None:
            digest = candidate.link.hashes.get("sha256")
        allowed = allowed_hashes_internal(self.hashes_internal)
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
        elif candidate.link.filename.endswith(".whl"):
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
        if urllib.parse.urlparse(candidate.link.url).fragment.startswith("egg="):
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


def filter_unallowed_hashes(
    candidates: list[InstallationCandidate],
    *,
    hashes: Hashes | None,
    project_name: str,
) -> list[InstallationCandidate]:
    allowed = allowed_hashes_internal(hashes)
    if hashes is None:
        return list(candidates)
    if not allowed:
        log_hash_check(
            "Given no hashes to check %d links for project %r: discarding no candidates",
            len(candidates),
            project_name,
        )
        return list(candidates)
    matches = 0
    no_digest = 0
    discarded: list[str] = []
    result: list[InstallationCandidate] = []
    for candidate in candidates:
        candidate_hashes = candidate.link.hashes or {}
        digest = candidate_hashes.get("sha256")
        if digest is None:
            no_digest += 1
            result.append(candidate)
        elif digest in allowed:
            matches += 1
            result.append(candidate)
        else:
            discarded.append(candidate.link.url)
    if matches == 0:
        log_hash_check(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
        return list(candidates)
    if discarded:
        log_hash_check(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding %d non-matches:\n  %s",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
            len(discarded),
            "\n  ".join(discarded),
        )
    else:
        log_hash_check(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
    return result


def allowed_hashes_internal(hashes: Hashes | None) -> frozenset[str]:
    return hashes.allowed_digests if hashes is not None else frozenset()
