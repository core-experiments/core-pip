"""Installation orchestration for prepared requirements."""

from __future__ import annotations

import logging

from cpip.build.metadata import (
    InstalledDistributionStore,
    InstalledMetadataDistribution,
)
from cpip.core.packaging import canonicalize_name
from cpip.install.target import InstallTarget
from cpip.install.uninstall import DistributionUninstaller
from cpip.install.wheel_transaction import (
    WheelInstaller,
)
from cpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


class RequirementInstaller:
    """Install and remove prepared requirements in one target configuration."""

    def __init__(
        self,
        *,
        root: str | None = None,
        home: str | None = None,
        prefix: str | None = None,
        use_user_site: bool = False,
        pycompile: bool = True,
        script_executable: str | None = None,
    ) -> None:
        self.root = root
        self.home = home
        self.prefix = prefix
        self.use_user_site = use_user_site
        self.pycompile = pycompile
        self.script_executable = script_executable
        self.uninstaller_internal = DistributionUninstaller()

    def check_if_exists(self, requirement: InstallRequirement) -> None:
        """Attach installed-distribution state before installing."""
        req = requirement.req
        if req is None:
            return
        existing = InstalledDistributionStore().find(req.name)
        if existing is None:
            return
        compatible = req.specifier.contains(existing.version, allow_prereleases=True)
        if not compatible or requirement.editable:
            requirement.satisfied_by = None
            requirement.should_reinstall = True
        else:
            requirement.satisfied_by = existing

    def uninstall(self, name: str, *, paths: list[str] | None = None) -> bool:
        """Uninstall an installed distribution using its recorded files."""
        uninstaller = (
            self.uninstaller_internal if paths is None else DistributionUninstaller(paths)
        )
        return uninstaller.uninstall(name)

    def install(self, requirement: InstallRequirement) -> None:
        req = requirement.req
        local_file_path = requirement.local_file_path
        if req is None or not requirement.is_wheel or local_file_path is None:
            raise RuntimeError(f"Cannot install unprepared requirement {requirement}")
        target = InstallTarget.from_options(
            canonicalize_name(req.name),
            target=None,
            user=self.use_user_site,
            home=self.home,
            root=self.root,
            prefix=self.prefix,
            isolated=requirement.isolated,
        )
        WheelInstaller(
            target,
            pycompile=self.pycompile,
            force=requirement.should_reinstall,
            script_executable=self.script_executable,
        ).install(
            local_file_path,
            requested=requirement.user_supplied,
            direct_url=requirement.download_info if requirement.is_direct else None,
        )
        requirement.install_succeeded = True

    def install_all(self, requirements: list[InstallRequirement]) -> list[str]:
        """Install prepared requirements and report the installed names."""
        to_install = {
            requirement.name: requirement for requirement in requirements if requirement.name
        }
        if to_install:
            logger.info(
                "Installing collected packages: %s",
                ", ".join(to_install),
            )

        installed: list[str] = []
        for requirement in to_install.values():
            name = requirement.name
            assert name is not None
            self.install(requirement)
            installed.append(name)
        return installed


def installed_packages_summary(
    installed: list[str],
    env: list[InstalledMetadataDistribution],
) -> str:
    """Return the concise summary shown after installing packages."""
    installed.sort()
    installed_versions = {
        distribution.canonical_name: distribution.version for distribution in env
    }
    summary = []
    for package in installed:
        version = installed_versions.get(canonicalize_name(package))
        summary.append(f"{package}-{version}" if version else package)
    return f"Successfully installed {' '.join(summary)}" if summary else ""
