"""Metadata preparation, direct URLs, editable sources, and install reports."""

from __future__ import annotations

import json
import os
import shutil
import sys

from cpip.build.metadata import InstalledMetadataDistribution, MetadataDistribution
from cpip.core.direct_url import ArchiveInfo, DirectUrl, DirInfo, VcsInfo
from cpip.core.errors import BuildError, CommandError, InstallationError
from cpip.core.hashes import file_hashes
from cpip.core.packaging import (
    SpecifierSet,
    canonicalize_name,
    canonicalize_requirement,
)
from cpip.core.temp_dir import remove_temp_directory
from cpip.core.urls import path_to_url, url_to_path
from cpip.core.utils import CURRENT_PYTHON_VERSION_FULL
from cpip.index.artifacts import ArtifactLocator
from cpip.index.links import Link
from cpip.resolution.input_requirements import install_req_from_editable

TYPE_CHECKING = False

if TYPE_CHECKING:
    from cpip.build.build_backend import ProjectMetadata
    from cpip.resolution.req_install import InstallRequirement

MetadataView = MetadataDistribution | InstalledMetadataDistribution


class MetadataInconsistent(InstallationError):
    def __init__(self, ireq: object, field: str, file_value: str, metadata_value: str):
        self.ireq = ireq
        self.field = field
        self.file_value = file_value
        self.metadata_value = metadata_value
        self.f_val = file_value
        self.m_val = metadata_value

    def __str__(self) -> str:
        return (
            f"Requested {self.ireq} has inconsistent {self.field}: "
            f"expected {self.file_value!r}, but metadata has {self.metadata_value!r}"
        )


class SidecarMetadataInconsistent(MetadataInconsistent):
    def __str__(self) -> str:
        return (
            f"Requested {self.ireq} has inconsistent {self.field} between "
            "its PEP 658 .metadata file and the wheel's METADATA: "
            f"sidecar has {self.file_value!r}, wheel has {self.metadata_value!r}"
        )


class MetadataInvalid(InstallationError):
    def __init__(self, ireq: object, error: str):
        self.ireq = ireq
        self.error = error

    def __str__(self) -> str:
        return f"Requested {self.ireq} has invalid metadata: {self.error}"


def canonical_requires(
    req: InstallRequirement,
    dist: MetadataView,
    source: str,
) -> frozenset[str]:
    canonical: set[str] = set()
    for raw in dist.iter_raw_dependencies():
        try:
            canonical.add(canonicalize_requirement(raw.strip()))
        except ValueError as e:
            raise MetadataInvalid(req, f"Requires-Dist in {source}: {e}")
    return frozenset(canonical)


def check_sidecar_matches_wheel(
    req: InstallRequirement,
    sidecar_dist: MetadataView,
    wheel_dist: MetadataView,
) -> None:
    """Ensure PEP 658 metadata matches the wheel's embedded metadata."""
    sidecar_name = canonicalize_name(sidecar_dist.raw_name)
    wheel_name = canonicalize_name(wheel_dist.raw_name)

    if sidecar_name != wheel_name:
        raise SidecarMetadataInconsistent(req, "Name", sidecar_name, wheel_name)

    if sidecar_dist.version != wheel_dist.version:
        raise SidecarMetadataInconsistent(
            req,
            "Version",
            str(sidecar_dist.version),
            str(wheel_dist.version),
        )

    sidecar_requires = canonical_requires(
        req,
        sidecar_dist,
        "the PEP 658 .metadata file",
    )
    wheel_requires = canonical_requires(req, wheel_dist, "the wheel's METADATA")

    if sidecar_requires != wheel_requires:
        raise SidecarMetadataInconsistent(
            req,
            "Requires-Dist",
            ", ".join(sorted(sidecar_requires - wheel_requires)),
            ", ".join(sorted(wheel_requires - sidecar_requires)),
        )

    if sidecar_dist.requires_python != wheel_dist.requires_python:
        raise SidecarMetadataInconsistent(
            req,
            "Requires-Python",
            str(sidecar_dist.requires_python),
            str(wheel_dist.requires_python),
        )

    sidecar_extras = frozenset(sidecar_dist.iter_provided_extras())
    wheel_extras = frozenset(wheel_dist.iter_provided_extras())

    if sidecar_extras != wheel_extras:
        raise SidecarMetadataInconsistent(
            req,
            "Provides-Extra",
            ", ".join(sorted(sidecar_extras - wheel_extras)),
            ", ".join(sorted(wheel_extras - sidecar_extras)),
        )


def direct_url_from_link(
    link: Link,
    *,
    source_dir: str | None = None,
    link_is_in_wheel_cache: bool = False,
) -> DirectUrl:
    if link.is_vcs:
        # Deferred: the VCS backends (and their subprocess support) only for
        # a VCS link.
        from cpip.vcs.versioncontrol import vcs

        vcs_backend = vcs.get_backend_for_scheme(link.scheme)
        assert vcs_backend
        url, requested_revision, _ = vcs_backend.get_url_rev_and_auth(
            link.url_without_fragment,
        )
        subdirectory = link.subdirectory_fragment
        commit_id = None
        if source_dir and os.path.exists(source_dir):
            source_backend = vcs.get_backend_for_dir(source_dir)
            assert source_backend
            commit_id = source_backend.get_revision(source_dir)
        elif link_is_in_wheel_cache and requested_revision is not None:
            commit_id = requested_revision
        else:
            commit_id = "HEAD"
        return DirectUrl(
            url=url,
            info_subdir=subdirectory,
            vcs_info=VcsInfo(
                vcs=vcs_backend.name,
                commit_id=commit_id,
                requested_revision=requested_revision,
            ),
        )
    if link.is_existing_dir:
        return DirectUrl(url=link.url, dir_info=DirInfo())
    return DirectUrl(url=link.url, archive_info=ArchiveInfo(hashes=link.hashes or None))


def prepare_editable_source(
    editable: str,
    *,
    build_isolation: bool = True,
    prepare_metadata: bool = True,
) -> tuple[str, DirectUrl | None, ProjectMetadata | None]:
    """Validate and prepare an editable source for the build service."""
    requirement = install_req_from_editable(editable)
    link = requirement.link
    if link is None or not (link.is_vcs or link.is_existing_dir or link.is_file):
        raise CommandError(f"{editable} is not a valid editable requirement")

    source_path = ArtifactLocator().ensure_local(link.url)
    if link.url.startswith("file:"):
        direct_url = DirectUrl(url=link.url, dir_info=DirInfo(editable=True))
    elif link.is_vcs:
        direct_url = direct_url_from_link(link)
    else:
        direct_url = None
    if link.is_vcs:
        checkout_name = canonicalize_name(
            link.egg_fragment or os.path.basename(source_path),
        )
        checkout_dir = os.path.join(sys.prefix, "src", checkout_name)
        try:
            shutil.rmtree(checkout_dir)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(checkout_dir), exist_ok=True)
        materialized_source = source_path
        shutil.copytree(materialized_source, checkout_dir, symlinks=True)
        remove_temp_directory(materialized_source)
        source_path = checkout_dir
        direct_url = DirectUrl(
            url=path_to_url(checkout_dir),
            dir_info=DirInfo(editable=True),
        )

    if link.subdirectory_fragment:
        source_path = os.path.join(source_path, link.subdirectory_fragment)

    project_files: set[str] = set()
    try:
        with os.scandir(source_path) as entries:
            for entry in entries:
                if entry.name in {"setup.py", "pyproject.toml"} and entry.is_file():
                    project_files.add(entry.name)
    except OSError:
        raise CommandError(f"{source_path} is not a valid editable requirement")
    if not project_files:
        raise CommandError(
            f"{source_path} does not appear to be a Python project: "
            "neither 'setup.py' nor 'pyproject.toml' found",
        )

    if prepare_metadata:
        try:
            from cpip.build.build_backend import BackendSpec, prepare_project_metadata

            metadata = prepare_project_metadata(
                source_path,
                editable=True,
                build_isolation=build_isolation,
            )
        except BuildError as exc:
            if "build_editable" in str(exc):
                from cpip.build.build_backend import (
                    BackendSpec,
                    prepare_project_metadata,
                )

                backend_spec = BackendSpec.from_project(source_path)
                if (
                    backend_spec is not None
                    and backend_spec.name.startswith("setuptools.build_meta")
                    and "setup.py" in project_files
                    and "pyproject.toml" in project_files
                ):
                    metadata = None
                else:
                    raise BuildError(
                        f"Build backend for {source_path} is missing the 'build_editable' hook",
                    ) from exc
            if not build_isolation and (
                "Cannot import 'setuptools.build_meta'" in str(exc)
                or "pyproject.toml" in project_files
            ):
                from cpip.build.build_backend import (
                    BackendSpec,
                    prepare_project_metadata,
                )

                metadata = prepare_project_metadata(
                    source_path,
                    editable=True,
                    build_isolation=True,
                )
            else:
                metadata = None
    else:
        metadata = None
    egg = link.egg_fragment
    if (
        metadata is not None
        and egg is not None
        and canonicalize_name(egg) != canonicalize_name(metadata.name)
    ):
        print(f"{editable} has inconsistent name: expected {egg}, got {metadata.name}")
        raise CommandError(
            "Generating metadata for package "
            f"{egg} produced metadata for project name {metadata.name}. "
            f"Fix your #egg={egg} fragments.",
        )
    if metadata is not None and metadata.requires_python is not None:
        python_version = CURRENT_PYTHON_VERSION_FULL
        if not SpecifierSet(metadata.requires_python).contains(python_version):
            raise CommandError(
                f"Package '{metadata.name}' requires a different Python: "
                f"{python_version} not in '{metadata.requires_python}'",
            )
    return source_path, direct_url, metadata


class ReportItem:
    __slots__ = (
        "candidate_name",
        "candidate_version",
        "editable",
        "is_direct",
        "requested",
        "requested_extras",
        "requires_dist",
        "source_hashes",
        "source_url",
        "yanked",
    )

    def __init__(
        self,
        candidate_name: str,
        candidate_version: str,
        requested: bool,
        source_url: str | None,
        source_hashes: dict[str, str] | None,
        yanked: bool,
        is_direct: bool = False,
        requested_extras: tuple[str, ...] = (),
        requires_dist: tuple[str, ...] = (),
        editable: bool = False,
    ) -> None:
        self.candidate_name = candidate_name
        self.candidate_version = candidate_version
        self.requested = requested
        self.source_url = source_url
        self.source_hashes = source_hashes
        self.yanked = yanked
        self.is_direct = is_direct
        self.requested_extras = requested_extras
        self.requires_dist = requires_dist
        self.editable = editable


def write_install_report(
    path: str,
    items: list[ReportItem],
    network_stats: dict[str, int] | None = None,
    resolution_metrics: dict[str, int | float] | None = None,
) -> None:
    install_entries: list[dict[str, object]] = []
    seen: set[tuple[str, str, bool]] = set()
    for item in sorted(items, key=lambda item: not item.requested):
        key = (
            canonicalize_name(item.candidate_name),
            item.candidate_version,
            item.requested,
        )
        if key in seen:
            continue
        seen.add(key)
        download_info: dict[str, object] = {
            "url": item.source_url or "",
        }
        if item.source_url and item.source_url.startswith("git+"):
            vcs_url, _, commit_id = item.source_url[4:].partition("@")
            download_info["url"] = vcs_url
            download_info["vcs_info"] = {
                "vcs": "git",
                "commit_id": commit_id,
            }
        if item.editable:
            download_info["dir_info"] = {"editable": True}
        hashes = dict(item.source_hashes or {})
        if not hashes and item.source_url and item.source_url.startswith("file://"):
            try:
                hashes = file_hashes(url_to_path(item.source_url))
            except OSError:
                hashes = {}
        if hashes:
            algorithm, digest = next(iter(sorted(hashes.items())))
            download_info["archive_info"] = {
                "hash": f"{algorithm}={digest}",
                "hashes": hashes,
            }
        metadata: dict[str, object] = {
            "name": item.candidate_name,
            "version": item.candidate_version,
        }
        if item.requires_dist:
            metadata["requires_dist"] = list(item.requires_dist)
        entry: dict[str, object] = {
            "metadata": metadata,
            "requested": item.requested,
            "is_direct": item.is_direct,
            "is_yanked": item.yanked,
            "download_info": download_info,
        }
        if item.requested_extras:
            entry["requested_extras"] = list(item.requested_extras)
        install_entries.append(entry)
    report_values: dict[str, object] = {"version": "1", "install": install_entries}
    if network_stats is not None:
        report_values["cpip_network"] = network_stats
    if resolution_metrics is not None:
        report_values["cpip_resolution"] = resolution_metrics
    report = json.dumps(report_values)
    if path == "-":
        print(report)
    else:
        with open(path, "w", encoding="utf-8") as file:
            file.write(report)
