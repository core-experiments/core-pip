from __future__ import annotations

import logging
import os
import zipfile
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from cpip.core.errors import InstallationError
from cpip.core.filesystem import display_path
from cpip.core.subprocess import call_subprocess
from cpip.install.build_env.base import BuildIsolationMode
from cpip.install.build_env.noop import NoOpBuildEnvironment
from cpip.install.build_env.venv import VenvBuildEnvironment
from cpip.resolution.req_install import InstallRequirement
from cpip.vcs.support import hide_url
from cpip.vcs.versioncontrol import vcs

if TYPE_CHECKING:
    from cpip.install.build_env.base import BuildEnvironmentInstaller

logger = logging.getLogger(__name__)


class SourceManager:
    """Manage the source checkout and archive for one requirement."""

    def __init__(self, requirement: InstallRequirement) -> None:
        self.requirement = requirement

    def update_editable(self) -> None:
        link = self.requirement.link
        source_dir = self.requirement.source_dir
        if link is None or source_dir is None or link.scheme == "file":
            return
        backend = vcs.get_backend_for_scheme(link.scheme)
        if backend is None:
            raise InstallationError(f"Unsupported VCS URL {link.url}")
        backend.obtain(source_dir, url=hide_url(link.url), verbosity=0)

    def archive(self, build_dir: str | None) -> None:
        source_dir = self.requirement.source_dir
        name = self.requirement.name
        if source_dir is None or build_dir is None or not name:
            return
        version = self.requirement.metadata.get("version", "unknown")
        archive = os.path.join(build_dir, f"{name}-{version}.zip")
        if os.path.exists(archive):
            os.remove(archive)
        root = os.path.normcase(os.path.abspath(source_dir))
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for directory, _, filenames in os.walk(root):
                for filename in filenames:
                    path = os.path.join(directory, filename)
                    output.write(path, os.path.join(name, os.path.relpath(path, root)))
        logger.info("Saved %s", display_path(archive))


class SourceMetadataPreparation:
    """Represents a source distribution.

    The preparation step for these needs metadata for the packages to be
    generated.
    """

    def __init__(self, req: InstallRequirement) -> None:
        self.req = req

    def prepare(
        self,
        build_env_installer: BuildEnvironmentInstaller,
        build_isolation: BuildIsolationMode,
        check_build_deps: bool,
    ) -> None:
        # Set up the backend environment after the requirement loads its project data.
        self.prepare_build_env(build_isolation, build_env_installer)

        # Set up the build isolation, if this requirement should be isolated
        if build_isolation != "off":
            # Setup an isolated environment and install the build backend static
            # requirements in it.
            self.prepare_build_backend()
            # Check that the build backend supports PEP 660. This cannot be done
            # earlier because we need to setup the build backend to verify it
            # supports build_editable, nor can it be done later, because we want
            # to avoid installing build requirements needlessly.
            self.req.editable_sanity_check()
            # Install the dynamic build requirements.
            self.install_build_reqs(build_env_installer)
        else:
            # When not using build isolation, we still need to check that
            # the build backend supports PEP 660.
            self.req.editable_sanity_check()
        # Check if the current environment provides build dependencies
        if check_build_deps:
            pyproject_requires = self.req.pyproject_requires
            assert pyproject_requires is not None
            conflicting, missing = self.req.build_env.check_requirements(
                pyproject_requires,
            )
            if conflicting:
                self.raise_conflicts(
                    "the backend dependencies",
                    cast("set[tuple[str, str]]", conflicting),
                )
            if missing:
                self.raise_missing_reqs(missing)
        self.req.prepare_metadata()

    def prepare_build_env(
        self,
        build_isolation: BuildIsolationMode,
        build_env_installer: BuildEnvironmentInstaller,
    ) -> None:
        if build_isolation == "venv":
            self.req.build_env = VenvBuildEnvironment(build_env_installer)

        self.req.configure_backend(self.req.build_env.python_executable)

    def prepare_build_backend(self) -> None:
        # Install the pyproject.toml declared build-time requirements.
        pyproject_requires = self.req.pyproject_requires
        assert pyproject_requires is not None
        assert not isinstance(self.req.build_env, NoOpBuildEnvironment)

        with self.req.build_env:
            self.req.build_env.install_requirements(
                pyproject_requires,
                "overlay",
                kind="build dependencies",
                for_req=self.req,
            )
        conflicting, missing = self.req.build_env.check_requirements(
            self.req.requirements_to_check,
        )
        if conflicting:
            self.raise_conflicts(
                "PEP 517/518 supported requirements",
                cast("set[tuple[str, str]]", conflicting),
            )
        if missing:
            logger.warning(
                "Missing build requirements in pyproject.toml for %s.",
                self.req,
            )
            logger.warning(
                "The project does not specify a build backend, and "
                "cpip cannot fall back to setuptools without %s.",
                " and ".join(map(repr, sorted(missing))),
            )

    def get_build_requires(self, editable: bool) -> Iterable[str]:
        with self.req.build_env:
            backend = self.req.pep517_backend
            assert backend is not None
            with backend.subprocess_runner(call_subprocess):
                if editable:
                    return backend.get_requires_for_build_editable()
                return backend.get_requires_for_build_wheel()

    def install_build_reqs(
        self,
        build_env_installer: BuildEnvironmentInstaller,
    ) -> None:
        # Install any extra build dependencies that the backend requests.
        # This must be done in a second pass, as the pyproject.toml
        # dependencies must be installed before we can call the backend.
        if (
            self.req.editable
            and self.req.permit_editable_wheels
            and self.req.supports_pyproject_editable
        ):
            build_reqs = self.get_build_requires(editable=True)
        else:
            build_reqs = self.get_build_requires(editable=False)
        conflicting, missing = self.req.build_env.check_requirements(build_reqs)
        if conflicting:
            self.raise_conflicts(
                "the backend dependencies",
                cast("set[tuple[str, str]]", conflicting),
            )
        with self.req.build_env:
            self.req.build_env.install_requirements(
                missing,
                "normal",
                kind="backend dependencies",
                for_req=self.req,
            )

    def raise_conflicts(
        self,
        conflicting_with: str,
        conflicting_reqs: set[tuple[str, str]],
    ) -> None:
        format_string = (
            "Some build dependencies for {requirement} "
            "conflict with {conflicting_with}: {description}."
        )
        error_message = format_string.format(
            requirement=self.req,
            conflicting_with=conflicting_with,
            description=", ".join(
                f"{installed} is incompatible with {wanted}"
                for installed, wanted in sorted(conflicting_reqs)
            ),
        )
        raise InstallationError(error_message)

    def raise_missing_reqs(self, missing: set[str]) -> None:
        format_string = (
            "Some build dependencies for {requirement} are missing: {missing}."
        )
        error_message = format_string.format(
            requirement=self.req,
            missing=", ".join(map(repr, sorted(missing))),
        )
        raise InstallationError(error_message)
