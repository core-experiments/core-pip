from __future__ import annotations

import abc
import os
from collections.abc import Iterable
from contextlib import AbstractContextManager as ContextManager
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Protocol

from cpip.build.metadata import InstalledDistributionStore

from cpip.core.packaging import Version, marker_applies, parse_requirement
from cpip.platform.locations.sysconfig import get_scheme

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement


BuildIsolationMode = Literal["off", "venv"]


class Prefix:
    """Filesystem locations used by an isolated build environment."""

    def __init__(self, path: str, *, python_executable: str | None = None) -> None:
        self.path = path
        scheme = get_scheme("", prefix=path)
        self.lib_dirs = list(dict.fromkeys((scheme.purelib, scheme.platlib)))
        self.python_executable = python_executable


class BuildEnvironmentInstaller(Protocol):
    """
    Interface for installing build dependencies into an isolated build
    environment.
    """

    def install(
        self,
        requirements: Iterable[str],
        prefix: Prefix,
        *,
        kind: str,
        for_req: InstallRequirement | None,
    ) -> None: ...


class BuildEnvironment(ContextManager[None], metaclass=abc.ABCMeta):
    """Creates and manages an isolated environment to install build deps"""

    lib_dirs: list[str] | None
    python_executable: str
    save_env: dict[str, str | None]

    @abc.abstractmethod
    def __init__(self, installer: BuildEnvironmentInstaller): ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for varname, old_value in self.save_env.items():
            if old_value is None:
                os.environ.pop(varname, None)
            else:
                os.environ[varname] = old_value

    def check_requirements(
        self, reqs: Iterable[str]
    ) -> tuple[set[tuple[str, str]], set[str]]:
        """Return 2 sets:
        - conflicting requirements: set of (installed, wanted) reqs tuples
        - missing requirements: set of reqs
        """
        missing = set()
        conflicting = set()
        if reqs:
            for req_str in reqs:
                req = parse_requirement(req_str)
                # We're explicitly evaluating with an empty extra value, since build
                # environments are not provided any mechanism to select specific extras.
                if req.marker is not None and not marker_applies(req.marker):
                    continue
                dist = InstalledDistributionStore(paths=self.lib_dirs).find(req.name)
                if not dist:
                    missing.add(req_str)
                    continue
                if isinstance(dist.version, Version):
                    installed_req_str = f"{req.name}=={dist.version}"
                else:
                    installed_req_str = f"{req.name}==={dist.version}"
                if not req.specifier.contains(dist.version, allow_prereleases=True):
                    conflicting.add((installed_req_str, req_str))
                # FIXME: Consider direct URL?
        return conflicting, missing

    @abc.abstractmethod
    def install_requirements(
        self,
        requirements: Iterable[str],
        prefix_as_string: str,
        *,
        kind: str,
        for_req: InstallRequirement | None = None,
    ) -> None: ...
