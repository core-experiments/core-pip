"""Index-backed candidate source."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cpip.core.packaging import Requirement
    from cpip.index.provider import CandidateProvider


class IndexCandidateSource:
    """Small source boundary around the existing index provider."""

    def __init__(self, provider: CandidateProvider) -> None:
        self.provider = provider

    def find_candidates(self, requirement: Requirement) -> Any:
        return self.provider.find_candidates(requirement)

    def available_versions(self, requirement: Requirement) -> Any:
        return self.provider.available_versions(requirement)

    def close(self) -> None:
        self.provider.close()
