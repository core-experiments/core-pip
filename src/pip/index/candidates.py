from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pip.core.errors import BuildError
from pip.core.packaging import Version, canonicalize_name
from pip.index.source_models import CandidateRecord

if TYPE_CHECKING:
    from pip.core.wheel import TargetContext, WheelFile
    from pip.index.links import Link
    from pip.index.source_models import CandidateRecord, RejectedCandidate


def remove_temp_directory_internal(path: str | Path) -> None:
    from pip.core.temp_dir import remove_temp_directory

    remove_temp_directory(path)


def prepare_project_metadata(
    source_dir: Path,
    *,
    editable: bool = False,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
):
    from pip.build.build_backend import prepare_project_metadata as prepare

    return prepare(
        source_dir,
        editable=editable,
        build_constraints=build_constraints,
        build_isolation=build_isolation,
    )


class InstallationCandidate(CandidateRecord):
    __slots__ = ()

    def __init__(
        self,
        name: str,
        version: str | Version,
        link: Link,
        wheel: WheelFile | None = None,
        tag_rank: int | None = None,
    ) -> None:
        super().__init__(
            name,
            version if isinstance(version, Version) else Version(version),
            link,
            wheel,
            tag_rank,
        )

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    def to_record(self) -> CandidateRecord:
        from pip.index.source_models import CandidateRecord

        return CandidateRecord(
            name=self.name,
            version=self.version,
            link=self.link,
            wheel=self.wheel,
            tag_rank=self.tag_rank,
        )

    def __hash__(self) -> int:
        return hash((self.name, str(self.version), self.link))

    @classmethod
    def from_link(
        cls,
        link: Link,
        *,
        target: TargetContext | None = None,
    ) -> "InstallationCandidate | RejectedCandidate":
        from pip.core.wheel import (
            parse_wheel_file,
            supported_wheel_tags,
            wheel_tag_rank,
        )
        from pip.index.source_models import (
            ArtifactKind,
            RejectedCandidate,
            RejectionReason,
        )

        if link.kind is ArtifactKind.WHEEL:
            wheel = parse_wheel_file(link.filename)
            if wheel is None:
                return RejectedCandidate(
                    link, RejectionReason.INVALID_WHEEL, "invalid wheel filename"
                )
            return cls(
                name=wheel.name,
                version=wheel.version,
                link=link,
                wheel=wheel,
                tag_rank=wheel_tag_rank(wheel.tags, supported_wheel_tags(target)),
            )
        if link.kind is ArtifactKind.SOURCE_TREE:
            return cls.from_vcs(link) if link.is_vcs else cls.from_source_tree(link)
        if link.kind is not ArtifactKind.SDIST:
            return RejectedCandidate(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                f"{link.kind.value} candidates are not installable yet",
            )
        from pip.index.directory_index import project_version_from_filename

        parsed = project_version_from_filename(link.filename)
        if parsed is None:
            return RejectedCandidate(
                link,
                RejectionReason.INVALID_VERSION,
                "could not parse project and version",
            )
        name, version = parsed
        return cls(name=name, version=version, link=link)

    @classmethod
    def from_source_tree(
        cls, link: Link
    ) -> "InstallationCandidate | RejectedCandidate":
        from pip.index.source_models import RejectedCandidate, RejectionReason

        local = Path(link.file_path)
        if not local.exists():
            return RejectedCandidate(
                link, RejectionReason.MISSING_ARTIFACT, "source tree is not local"
            )
        try:
            metadata = prepare_project_metadata(local)
            version = Version(metadata.version)
        except ValueError:
            return RejectedCandidate(
                link, RejectionReason.INVALID_VERSION, "invalid project version"
            )
        except BuildError:
            if link.source_url is None and not (
                (local / "pyproject.toml").exists() or (local / "setup.py").exists()
            ):
                return cls(name=local.name or "source", version=Version("0"), link=link)
            if (local / "pyproject.toml").exists():
                try:
                    if "version" in (local / "pyproject.toml").read_text(
                        encoding="utf-8"
                    ):
                        return RejectedCandidate(
                            link,
                            RejectionReason.INVALID_VERSION,
                            "invalid project version",
                        )
                except OSError:
                    pass
            return cls(name=local.name or "source", version=Version("0"), link=link)
        except OSError:
            return RejectedCandidate(
                link, RejectionReason.MISSING_ARTIFACT, "source tree is unreadable"
            )
        return cls(name=metadata.name, version=version, link=link)

    @classmethod
    def from_vcs(cls, link: Link) -> "InstallationCandidate | RejectedCandidate":
        from pip.index.source_models import RejectedCandidate, RejectionReason
        from pip.index.vcs import materialize_vcs

        local = None
        try:
            local = materialize_vcs(link.url, emit_resolution=False)
            metadata = prepare_project_metadata(local)
            version = Version(metadata.version)
        except (BuildError, ValueError):
            return RejectedCandidate(
                link, RejectionReason.INVALID_VERSION, "invalid project version"
            )
        except OSError as exc:
            return RejectedCandidate(link, RejectionReason.MISSING_ARTIFACT, str(exc))
        finally:
            if local is not None:
                remove_temp_directory_internal(local)
        return cls(name=metadata.name, version=version, link=link)

    def sort_key(self, *, prefer_binary: bool) -> tuple[object, object, object, int]:
        from pip.index.source_models import ArtifactKind

        wheel_rank = 1 if self.link.kind is ArtifactKind.WHEEL else 0
        tag_rank = -(self.tag_rank if self.tag_rank is not None else 1_000_000)
        yanked_rank = 0 if self.link.is_yanked else 1
        if prefer_binary:
            return (yanked_rank, wheel_rank, self.version, tag_rank)
        return (yanked_rank, self.version, wheel_rank, tag_rank)

    def __str__(self) -> str:
        return f"{self.name!r} candidate (version {self.version} at {self.link})"


class BestCandidateResult:
    __slots__ = ("all_candidates", "applicable_candidates", "best_candidate")

    def __init__(
        self,
        all_candidates: list[InstallationCandidate],
        applicable_candidates: list[InstallationCandidate],
        best_candidate: InstallationCandidate | None,
    ) -> None:
        self.all_candidates = all_candidates
        self.applicable_candidates = applicable_candidates
        self.best_candidate = best_candidate

        assert set(self.applicable_candidates) <= set(self.all_candidates)
        if self.best_candidate is None:
            assert not self.applicable_candidates
        else:
            assert self.best_candidate in self.applicable_candidates
