"""Candidate compatibility and requirement-hash validation."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from cpip.core.errors import (
    DirectoryUrlHashUnsupported,
    HashMismatch,
    HashMissing,
    HashUnpinned,
    VcsHashUnsupported,
)
from cpip.core.packaging import Requirement, SpecifierSet
from cpip.core.urls import url_to_path
from cpip.core.wheel import WheelCandidate
from cpip.resolution.engine.algorithms import (
    actual_hashes_for_candidate,
    allowed_hashes_internal,
    hash_sets,
    hashes_match,
    is_direct_requirement,
)

if TYPE_CHECKING:
    from cpip.resolution.engine.context import ConfigurationContext
    from cpip.resolution.engine.input.models import RequirementInput


class ValidationOperations:
    """Candidate compatibility and requirement-hash operations."""

    def candidate_matches_python(
        self: ConfigurationContext,
        candidate: WheelCandidate,
    ) -> bool:
        if not candidate.requires_python:
            return True
        try:
            return SpecifierSet(candidate.requires_python).contains(self.python_version)
        except ValueError:
            return True

    def allow_prereleases_internal(
        self: ConfigurationContext,
        requirement: Requirement,
    ) -> bool:
        key = (
            requirement.canonical_name,
            requirement.specifier.text_internal,
            requirement.url,
            requirement.raw,
        )
        cached = self.allow_prereleases_cache.get(key)
        if cached is not None:
            return cached
        controlled = self.provider.release_control
        if controlled is not None:
            value = controlled.allows_prereleases(requirement.name)
            if value is not None:
                self.allow_prereleases_cache[key] = value
                return value
        mentions_prerelease = any(
            spec.operator != "==="
            and not spec.version.endswith(".*")
            and spec.parsed_version.is_prerelease
            for spec in requirement.specifier.specifiers
        )
        result = (
            self.allow_prereleases
            or is_direct_requirement(requirement)
            or mentions_prerelease
        )
        self.allow_prereleases_cache[key] = result
        return result

    @staticmethod
    def candidate_cache_key(
        requirement: Requirement,
    ) -> tuple[str, str, tuple[str, ...], str | None, str]:
        return (
            requirement.canonical_name,
            requirement.specifier.text_internal,
            tuple(sorted(requirement.extras)),
            requirement.url,
            requirement.raw,
        )

    def preflight_hash_requirement(
        self: ConfigurationContext,
        requirement: Requirement,
        *,
        source_requirements: dict[str, RequirementInput],
        source_requirements_by_url: dict[str, RequirementInput],
    ) -> None:
        if not self.require_hashes:
            return
        source_req = source_requirements.get(requirement.canonical_name)
        if source_req is None and requirement.url is not None:
            source_req = source_requirements_by_url.get(requirement.url)
        if source_req is None or source_req.link is None:
            return
        link_url = source_req.link.url
        if link_url.startswith("git+"):
            raise VcsHashUnsupported(
                "Can't verify hashes for these requirements because we don't "
                "have a way to hash version control repositories",
            )
        if link_url.startswith("file://"):
            local_path = url_to_path(link_url)
            if os.path.isdir(local_path):
                raise DirectoryUrlHashUnsupported(
                    "Can't verify hashes for these file:// requirements because "
                    "they point to directories",
                )

    def validate_candidate_hashes(
        self: ConfigurationContext,
        requirement: Requirement,
        candidate: WheelCandidate,
        *,
        source_requirements: dict[str, RequirementInput],
        source_requirements_by_url: dict[str, RequirementInput],
    ) -> None:
        source_req = source_requirements.get(requirement.canonical_name)
        if source_req is None and candidate.source_url is not None:
            source_req = source_requirements_by_url.get(candidate.source_url)
        provider_hashes = self.provider.hashes_by_name.get(requirement.canonical_name)
        if not self.require_hashes:
            self.validate_link_hashes(requirement, candidate, source_req)
            return
        if source_req is None:
            if provider_hashes is not None and provider_hashes.allowed_internal:
                allowed = hash_sets(provider_hashes.allowed_internal)
                actual = actual_hashes_for_candidate(candidate)
                if hashes_match(allowed, actual):
                    return
                raise HashMismatch(
                    "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.",
                )
            if requirement.url is not None:
                raise HashMissing(
                    "Hashes are required in --require-hashes mode, but they are missing "
                    f"from some requirements. Missing hash for:\n    {requirement.name}=={candidate.version}",
                )
            specifier = str(requirement.specifier)
            if not (
                specifier.startswith("==")
                and "*" not in specifier
                and "," not in specifier
            ):
                raise HashUnpinned(
                    "In --require-hashes mode, all requirements must have their "
                    f"versions pinned with ==. Unpinned requirement:\n    {requirement.name}",
                )
            raise HashMissing(
                "Hashes are required in --require-hashes mode, but they are missing "
                f"from some requirements. Missing hash for:\n    {requirement.name}=={candidate.version}",
            )
        if source_req.link is not None:
            link_url = source_req.link.url
            if link_url.startswith("git+"):
                raise VcsHashUnsupported(
                    "Can't verify hashes for these requirements because we don't "
                    "have a way to hash version control repositories",
                )
            if link_url.startswith("file://"):
                local_path = url_to_path(link_url)
                if os.path.isdir(local_path):
                    raise DirectoryUrlHashUnsupported(
                        "Can't verify hashes for these file:// requirements because "
                        "they point to directories",
                    )
        allowed_hashes = allowed_hashes_internal(source_req)
        if (
            not allowed_hashes
            and provider_hashes is not None
            and provider_hashes.allowed_internal
        ):
            allowed_hashes = hash_sets(provider_hashes.allowed_internal)
        if (
            not allowed_hashes
            and source_req.link is not None
            and source_req.link.hashes
        ):
            allowed_hashes = hash_sets(source_req.link.hashes)
        if (
            not allowed_hashes
            and source_req.user_supplied
            and source_req.link is not None
            and source_req.link.url == candidate.source_url
            and candidate.source_hashes
        ):
            allowed_hashes = hash_sets(candidate.source_hashes)
        actual_hashes = actual_hashes_for_candidate(candidate)
        if not allowed_hashes:
            suggestion = ""
            sha256 = actual_hashes.get("sha256")
            if sha256:
                suggestion = f" --hash=sha256:{sha256}"
            raise HashMissing(
                "Hashes are required in --require-hashes mode, but they are missing "
                f"from some requirements. Missing hash for:\n    {source_req}{suggestion}",
            )
        if hashes_match(allowed_hashes, actual_hashes):
            return
        if candidate.from_cache:
            print(
                "WARNING: The hashes of the source archive found in cache entry "
                "don't match, ignoring cached built wheel and re-downloading source.",
                file=sys.stderr,
            )
        expected_algorithm, expected_digests = next(
            iter(sorted(allowed_hashes.items())),
        )
        expected_digest = min(expected_digests)
        actual_digest = actual_hashes.get(expected_algorithm) or "<missing>"
        label = source_req.link.url if source_req.link is not None else str(source_req)
        raise HashMismatch(
            "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
            f"    {label}:\n"
            f"        Expected {expected_algorithm} {expected_digest}\n"
            f"             Got        {actual_digest}",
        )

    def validate_link_hashes(
        self: ConfigurationContext,
        requirement: Requirement,
        candidate: WheelCandidate,
        source_req: RequirementInput | None,
    ) -> None:
        if not candidate.source_hashes:
            return
        if source_req is not None and allowed_hashes_internal(source_req):
            return
        if not is_direct_requirement(requirement):
            return
        actual_hashes = actual_hashes_for_candidate(candidate)
        if not actual_hashes or hashes_match(
            hash_sets(candidate.source_hashes),
            actual_hashes,
        ):
            return
        expected_algorithm, expected_digests = next(
            iter(sorted(hash_sets(candidate.source_hashes).items())),
        )
        expected_digest = min(expected_digests)
        actual_digest = actual_hashes.get(expected_algorithm) or "<missing>"
        label = candidate.source_url or str(candidate.path)
        raise HashMismatch(
            "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
            f"    {label}:\n"
            f"        Expected {expected_algorithm} {expected_digest}\n"
            f"             Got        {actual_digest}",
        )
