from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from pip.core.packaging import Requirement, Version

if TYPE_CHECKING:
    from pip.index.candidates import InstallationCandidate
    from pip.index.links import Link


class ArtifactKind(Enum):
    WHEEL = "wheel"
    SDIST = "sdist"
    SOURCE_TREE = "source-tree"
    METADATA = "metadata"
    ATTESTATION = "attestation"
    UNKNOWN = "unknown"


class RejectionReason(Enum):
    DIFFERENT_PROJECT = "different-project"
    INVALID_VERSION = "invalid-version"
    VERSION_MISMATCH = "version-mismatch"
    REQUIRES_PYTHON = "requires-python"
    YANKED = "yanked"
    UNSUPPORTED_WHEEL = "unsupported-wheel"
    UNSUPPORTED_ARTIFACT = "unsupported-artifact"
    INVALID_WHEEL = "invalid-wheel"
    MISSING_ARTIFACT = "missing-artifact"


@dataclass(frozen=True)
class MetadataFile:
    hashes: dict[str, str] | None


@dataclass(frozen=True)
class VcsReference:
    vcs: str
    repo_url: str
    requested_revision: str | None


@dataclass(frozen=True)
class RejectedCandidate:
    link: Link
    reason: RejectionReason
    detail: str


@dataclass(frozen=True)
class CandidateSelection:
    accepted: tuple[InstallationCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True)
class CandidateSummary:
    version: Version
    is_yanked: bool
    yanked_reason: str | None


class PackageSource(Protocol):
    def collect_links(self, requirement: Requirement) -> list[Link]: ...


class LinkSource(Protocol):
    @property
    def link(self) -> Link | None: ...

    def page_candidates(self) -> Iterable[InstallationCandidate]: ...

    def file_links(self) -> Iterable[Link]: ...
