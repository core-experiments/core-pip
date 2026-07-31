from __future__ import annotations

from enum import Enum
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Callable, Protocol

from cpip.core.packaging import Requirement, Version

if TYPE_CHECKING:
    from cpip.index.candidates import InstallationCandidate
    from cpip.index.links import Link
    from cpip.core.wheel import WheelFile


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


class MetadataFile:
    __slots__ = ("hashes",)

    def __init__(self, hashes: dict[str, str] | None) -> None:
        self.hashes = hashes

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MetadataFile) and self.hashes == other.hashes


class VcsReference:
    __slots__ = ("vcs", "repo_url", "requested_revision")

    def __init__(self, vcs: str, repo_url: str, requested_revision: str | None) -> None:
        self.vcs = vcs
        self.repo_url = repo_url
        self.requested_revision = requested_revision


class RejectedCandidate:
    __slots__ = ("link", "reason", "detail")

    def __init__(self, link: Link, reason: RejectionReason, detail: str) -> None:
        self.link = link
        self.reason = reason
        self.detail = detail


class CandidateSelection:
    __slots__ = ("accepted", "rejected")

    def __init__(
        self,
        accepted: tuple[CandidateRecord, ...],
        rejected: tuple[RejectedCandidate, ...],
    ) -> None:
        self.accepted = accepted
        self.rejected = rejected


class CandidateSummary:
    __slots__ = ("version", "is_yanked", "yanked_reason")

    def __init__(
        self, version: Version, is_yanked: bool, yanked_reason: str | None
    ) -> None:
        self.version = version
        self.is_yanked = is_yanked
        self.yanked_reason = yanked_reason


class CandidateMetadata:
    """Metadata needed by dependency resolution, separate from artifact state."""

    __slots__ = (
        "name",
        "version",
        "dependencies",
        "provided_extras",
        "requires_python",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        dependencies: tuple[Requirement, ...],
        provided_extras: frozenset[str],
        requires_python: str | None,
    ) -> None:
        self.name = name
        self.version = version
        self.dependencies = dependencies
        self.provided_extras = provided_extras
        self.requires_python = requires_python


class LazyCandidateMetadata:
    """A one-shot, memoized metadata computation for a candidate."""

    __slots__ = ("loader", "value")

    def __init__(self, loader: Callable[[], CandidateMetadata]) -> None:
        self.loader = loader
        self.value: CandidateMetadata | None = None

    def load(self) -> CandidateMetadata:
        metadata = self.value
        if metadata is None:
            metadata = self.loader()
            self.value = metadata
        return metadata


class CandidateRecord:
    """Immutable discovery result that does not imply artifact materialization."""

    __slots__ = ("name", "version", "link", "wheel", "tag_rank", "metadata_loader")

    def __init__(
        self,
        name: str,
        version: Version,
        link: Link,
        wheel: WheelFile | None = None,
        tag_rank: int | None = None,
        metadata_loader: LazyCandidateMetadata | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.link = link
        self.wheel = wheel
        self.tag_rank = tag_rank
        self.metadata_loader = metadata_loader

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CandidateRecord) and (
            self.name,
            self.version,
            self.link,
            self.wheel,
            self.tag_rank,
        ) == (other.name, other.version, other.link, other.wheel, other.tag_rank)

    def __hash__(self) -> int:
        return hash((self.name, self.version, self.link, self.wheel, self.tag_rank))

    def copy_with(self, **changes: object) -> CandidateRecord:
        values = {
            "name": self.name,
            "version": self.version,
            "link": self.link,
            "wheel": self.wheel,
            "tag_rank": self.tag_rank,
            "metadata_loader": self.metadata_loader,
        }
        values.update(changes)
        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        from cpip.core.packaging import canonicalize_name

        return canonicalize_name(self.name)

    def sort_key(self, *, prefer_binary: bool) -> tuple[object, object, object, int]:
        wheel_rank = 1 if self.link.kind is ArtifactKind.WHEEL else 0
        tag_rank = -(self.tag_rank if self.tag_rank is not None else 1_000_000)
        yanked_rank = 0 if self.link.is_yanked else 1
        if prefer_binary:
            return (yanked_rank, wheel_rank, self.version, tag_rank)
        return (yanked_rank, self.version, wheel_rank, tag_rank)

    def metadata(self) -> CandidateMetadata:
        loader = self.metadata_loader
        if loader is None:
            raise RuntimeError("candidate metadata loader is not configured")
        metadata = loader.value
        if metadata is None:
            metadata = loader.load()
        return metadata


class PackageCatalog:
    """Immutable package metadata shared by candidate and resolver queries."""

    __slots__ = (
        "links",
        "summaries",
        "summary_versions",
        "summaries_by_version",
        "links_by_version",
    )

    def __init__(
        self,
        links: tuple[Link, ...],
        summaries: tuple[CandidateSummary, ...],
        summary_versions: tuple[Version, ...],
        summaries_by_version: Mapping[Version, tuple[CandidateSummary, ...]],
        links_by_version: Mapping[Version, tuple[Link, ...]],
    ) -> None:
        self.links = links
        self.summaries = summaries
        self.summary_versions = summary_versions
        self.summaries_by_version = summaries_by_version
        self.links_by_version = links_by_version


class PackageSource(Protocol):
    def collect_links(self, requirement: Requirement) -> list[Link]: ...


class LinkSource(Protocol):
    @property
    def link(self) -> Link | None: ...

    def page_candidates(self) -> Iterable[InstallationCandidate]: ...

    def file_links(self) -> Iterable[Link]: ...
