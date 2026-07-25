"""Candidate filtering and ranking for the package finder."""

from __future__ import annotations

import logging
import urllib.parse

from pip.core.errors import InvalidWheelFilename
from pip.core.hashes import Hashes
from pip.core.packaging import Requirement, SpecifierSet, Version
from pip.core.release_control import ReleaseControl
from pip.core.target_python import TargetPython, get_supported
from pip.core.wheel import TargetContext, Wheel, WheelTag
from pip.index.candidates import BestCandidateResult, InstallationCandidate
from pip.index.links import Link
from pip.index.source_models import ArtifactKind, RejectedCandidate, RejectionReason

logger = logging.getLogger(__name__)


def _log_hash_check(message: str, *args: object) -> None:
    logger.debug(message, *args)


class CandidateEvaluator:
    def __init__(
        self,
        project_name: str,
        *,
        supported_tags: list[WheelTag],
        specifier: SpecifierSet,
        release_control: ReleaseControl | None = None,
        prefer_binary: bool = False,
        hashes: Hashes | None = None,
    ) -> None:
        self._project_name = project_name
        self._supported_tag_ranks: dict[str, int] = {}
        for index, tag in enumerate(supported_tags):
            self._supported_tag_ranks.setdefault(str(tag).lower(), index)
        self._specifier = specifier
        self._release_control = release_control
        self._prefer_binary = prefer_binary
        self._hashes = hashes

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
        allow_prereleases = self._allow_prereleases()
        if allow_prereleases is None:
            allow_prereleases = not any(
                not candidate.version.is_prerelease
                and self._specifier.contains(candidate.version)
                for candidate in candidates
            )
            allow_prereleases = allow_prereleases or any(
                spec.operator != "==="
                and not spec.version.endswith(".*")
                and Version(spec.version).is_prerelease
                for spec in self._specifier.specifiers
            )
        applicable = [
            candidate
            for candidate in candidates
            if not (candidate.version.is_prerelease and allow_prereleases is False)
            if self._specifier.contains(
                candidate.version, allow_prereleases=allow_prereleases
            )
        ]
        return filter_unallowed_hashes(
            applicable,
            hashes=self._hashes,
            project_name=self._project_name,
        )

    def _allow_prereleases(self) -> bool | None:
        if self._release_control is None:
            return None
        return self._release_control.allows_prereleases(self._project_name)

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
        parsed = InstallationCandidate.from_link(link, target=target)
        if isinstance(parsed, RejectedCandidate):
            if CandidateEvaluator.is_unnamed_direct_requirement(
                requirement
            ) and link.kind in {ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE}:
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
        return max(candidates, key=self._sort_key)

    def _sort_key(
        self, candidate: InstallationCandidate
    ) -> tuple[int, int, Version, int, int, int, int, tuple[int, str] | tuple[()]]:
        digest = None
        if candidate.link.hashes is not None:
            digest = candidate.link.hashes.get("sha256")
        allowed = _allowed_hashes(self._hashes)
        hash_rank = int(bool(allowed and digest in allowed))
        yanked_rank = -1 if candidate.link.is_yanked else 0
        wheel_rank = 0
        egg_fragment_rank = 1
        tag_rank = -1_000_000
        build_tag: tuple[int, str] | tuple[()] = ()
        if candidate.link.filename.endswith(".whl"):
            try:
                wheel = Wheel(candidate.link.filename)
                wheel_rank = 1
                supported_matches = (
                    rank
                    for file_tag in wheel.file_tags
                    if (
                        rank := self._supported_tag_ranks.get(
                            str(file_tag).lower()
                        )
                    )
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
        binary_preference = wheel_rank if self._prefer_binary else 0
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
    allowed = _allowed_hashes(hashes)
    if hashes is None:
        return list(candidates)
    if not allowed:
        _log_hash_check(
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
        _log_hash_check(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
        return list(candidates)
    if discarded:
        _log_hash_check(
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
        _log_hash_check(
            "Checked %d links for project %r against %d hashes (%d matches, %d no digest): discarding no candidates",
            len(candidates),
            project_name,
            len(allowed),
            matches,
            no_digest,
        )
    return result


def _allowed_hashes(hashes: Hashes | None) -> frozenset[str]:
    return hashes.allowed_digests if hashes is not None else frozenset()
