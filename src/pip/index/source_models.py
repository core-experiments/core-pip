from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Callable, Protocol

from pip.core.packaging import Requirement, Version

if TYPE_CHECKING:
    from pip.index.candidates import InstallationCandidate
    from pip.index.links import Link
    from pip.core.wheel import WheelFile


class ArtifactKind(Enum):
    WHEEL = "wheel"
    SDIST = "sdist"
    SOURCE_TREE = "source-tree"
    METADATA = "metadata"
    ATTESTATION = "attestation"
    UNKNOWN = "unknown"


SOURCE_ARTIFACT_KINDS = frozenset((ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE))
INSTALLABLE_ARTIFACT_KINDS = frozenset(
    (ArtifactKind.WHEEL, ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE)
)


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
    accepted: tuple[CandidateRecord, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True)
class CandidateSummary:
    version: Version
    is_yanked: bool
    yanked_reason: str | None


@dataclass(frozen=True)
class CandidateMetadata:
    """Metadata needed by dependency resolution, separate from artifact state."""

    name: str
    version: Version
    dependencies: tuple[Requirement, ...]
    provided_extras: frozenset[str]
    requires_python: str | None


@dataclass
class LazyCandidateMetadata:
    """A one-shot, memoized metadata computation for a candidate."""

    loader: Callable[[], CandidateMetadata]
    value: CandidateMetadata | None = None

    def load(self) -> CandidateMetadata:
        metadata = self.value
        if metadata is None:
            metadata = self.loader()
            self.value = metadata
        return metadata


@dataclass(frozen=True)
class CandidateRecord:
    """Immutable discovery result that does not imply artifact materialization."""

    name: str
    version: Version
    link: Link
    wheel: WheelFile | None = None
    tag_rank: int | None = None
    metadata_loader: LazyCandidateMetadata | None = field(
        default=None, compare=False, hash=False, repr=False
    )

    @property
    def canonical_name(self) -> str:
        from pip.core.packaging import canonicalize_name

        return canonicalize_name(self.name)

    def sort_key(self, *, prefer_binary: bool) -> tuple[object, object, object, int]:
        wheel_rank = 1 if self.link.kind is ArtifactKind.WHEEL else 0
        tag_rank = -(self.tag_rank if self.tag_rank is not None else 1_000_000)
        yanked_rank = 0 if self.link.is_yanked else 1
        if prefer_binary:
            return (yanked_rank, wheel_rank, self.version, tag_rank)
        return (yanked_rank, self.version, wheel_rank, tag_rank)

    def metadata(self) -> CandidateMetadata:
        if self.metadata_loader is None:
            raise RuntimeError("candidate metadata loader is not configured")
        return self.metadata_loader.load()


@dataclass(frozen=True)
class PackageCatalog:
    """Immutable package metadata shared by candidate and resolver queries."""

    links: tuple[Link, ...]
    summaries: tuple[CandidateSummary, ...]
    summaries_by_version: Mapping[Version, tuple[CandidateSummary, ...]]
    links_by_version: Mapping[Version, tuple[Link, ...]]


class PackageSource(Protocol):
    def collect_links(self, requirement: Requirement) -> list[Link]: ...


class LinkSource(Protocol):
    @property
    def link(self) -> Link | None: ...

    def page_candidates(self) -> Iterable[InstallationCandidate]: ...

    def file_links(self) -> Iterable[Link]: ...
