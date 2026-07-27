"""Metadata preparation and consistency checks for installation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pip.build.metadata import InstalledMetadataDistribution, MetadataDistribution
from pip.core.errors import InstallationError
from pip.core.packaging import canonicalize_requirement
from pip.install.build_env.base import BuildEnvironmentInstaller, BuildIsolationMode
from pip.install.source import SourceMetadataPreparation

if TYPE_CHECKING:
    from pip.resolution.req_install import InstallRequirement


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


class DistributionPreparer:
    """Build or read metadata for requirements using one build policy."""

    def __init__(
        self,
        build_tracker: Any,
        build_env_installer: BuildEnvironmentInstaller,
        build_isolation: BuildIsolationMode,
        check_build_deps: bool,
    ) -> None:
        self.build_tracker = build_tracker
        self.build_env_installer = build_env_installer
        self.build_isolation = build_isolation
        self.check_build_deps = check_build_deps

    def prepare(self, req: InstallRequirement) -> MetadataView:
        """Build or read distribution metadata for an install requirement."""
        if req.editable or not req.is_wheel:
            link = req.link
            if link is None:
                raise AssertionError("source requirement is missing its link")
            with self.build_tracker.track(req, link.url_without_fragment):
                req.load_pyproject_toml()
                SourceMetadataPreparation(req).prepare(
                    self.build_env_installer,
                    self.build_isolation,
                    self.check_build_deps,
                )
            distribution = req.distribution_internal
            if distribution is None:
                directory = req.metadata_directory
                if directory is None:
                    raise AssertionError("source requirement has no prepared metadata")
                distribution = MetadataDistribution.from_directory(directory)
                req.set_dist(distribution)
            return cast(MetadataView, distribution)

        path = req.local_file_path
        name = req.name
        if not path or not name:
            raise AssertionError("wheel requirement is missing its local path or name")
        return MetadataDistribution.from_wheel(path, name)


def canonical_requires(
    req: InstallRequirement, dist: MetadataView, source: str
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
    from pip.core.packaging import canonicalize_name

    sidecar_name = canonicalize_name(sidecar_dist.raw_name)
    wheel_name = canonicalize_name(wheel_dist.raw_name)
    if sidecar_name != wheel_name:
        raise SidecarMetadataInconsistent(req, "Name", sidecar_name, wheel_name)

    if sidecar_dist.version != wheel_dist.version:
        raise SidecarMetadataInconsistent(
            req, "Version", str(sidecar_dist.version), str(wheel_dist.version)
        )

    sidecar_requires = canonical_requires(
        req, sidecar_dist, "the PEP 658 .metadata file"
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
