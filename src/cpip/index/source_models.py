from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import TYPE_CHECKING, Callable, Protocol

from cpip.core.packaging import Requirement, Version, canonicalize_name

if TYPE_CHECKING:
    from cpip.core.wheel import WheelFile
    from cpip.index.links import Link


class ArtifactKind(Enum):
    WHEEL = "wheel"

    SDIST = "sdist"

    SOURCE_TREE = "source-tree"

    METADATA = "metadata"

    ATTESTATION = "attestation"

    UNKNOWN = "unknown"


SOURCE_ARTIFACT_KINDS = frozenset((ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE))

INSTALLABLE_ARTIFACT_KINDS = frozenset(
    (ArtifactKind.WHEEL, ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE),
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
    __slots__ = ("repo_url", "requested_revision", "vcs")

    def __init__(self, vcs: str, repo_url: str, requested_revision: str | None) -> None:
        self.vcs = vcs

        self.repo_url = repo_url

        self.requested_revision = requested_revision


class RejectedCandidate:
    __slots__ = ("detail", "link", "reason")

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
    __slots__ = ("is_yanked", "version", "yanked_reason")

    def __init__(
        self,
        version: Version,
        is_yanked: bool,
        yanked_reason: str | None,
    ) -> None:
        self.version = version

        self.is_yanked = is_yanked

        self.yanked_reason = yanked_reason


class CandidateMetadata:
    """Metadata needed by dependency resolution, separate from artifact state."""

    __slots__ = (
        "dependencies",
        "name",
        "provided_extras",
        "requires_python",
        "version",
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

    __slots__ = (
        "_canonical_name",
        "link",
        "metadata_loader",
        "name",
        "tag_rank",
        "version",
        "wheel",
    )

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

        self._canonical_name: str | None = None

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
        cached = self._canonical_name

        if cached is not None:
            return cached

        cached = canonicalize_name(self.name)

        self._canonical_name = cached

        return cached

    def sort_key(self, *, prefer_binary: bool) -> tuple[object, object, object, int]:
        wheel_rank = 1 if self.link.kind is ArtifactKind.WHEEL else 0

        tag_rank = -(self.tag_rank if self.tag_rank is not None else 1_000_000)

        yanked_rank = 0 if self.link.is_yanked else 1

        # The version's comparison key orders exactly as the Version does
        # (Version.__lt__ compares nothing else) and, being a tuple of
        # ints, compares in C instead of through a Python-level __lt__ per
        # comparison of the sort.
        version_key = self.version.comparison_key

        if prefer_binary:
            return (yanked_rank, wheel_rank, version_key, tag_rank)

        return (yanked_rank, version_key, wheel_rank, tag_rank)

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
        "candidates_by_version",
        "links",
        "links_by_version",
        "records_by_version",
        "summaries",
        "summary_versions",
    )

    def __init__(
        self,
        links: tuple[Link, ...],
        candidates_by_version: Mapping[Version, tuple[CandidateRecord, ...]],
        summaries: tuple[CandidateSummary, ...],
        summary_versions: tuple[Version, ...],
        links_by_version: Mapping[Version, tuple[Link, ...]],
        records_by_version: Mapping[Version, tuple[object, ...]] | None = None,
    ) -> None:
        self.links = links

        self.candidates_by_version = candidates_by_version

        self.summaries = summaries

        self.summary_versions = summary_versions

        self.links_by_version = links_by_version

        self.records_by_version = records_by_version


class PackageSource(Protocol):
    def collect_links(self, requirement: Requirement) -> list[Link]: ...
