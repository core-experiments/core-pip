from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pip.install.build_env.base import BuildEnvironment

if TYPE_CHECKING:
    from pip.resolution.req_install import InstallRequirement


class NoOpBuildEnvironment(BuildEnvironment):
    """A no-op drop-in replacement for BuildEnvironment"""

    def __init__(self) -> None:
        self.python_executable = sys.executable
        self.lib_dirs = None
        self._save_env = {}

    def __enter__(self) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def install_requirements(
        self,
        requirements: Iterable[str],
        prefix_as_string: str,
        *,
        kind: str,
        for_req: InstallRequirement | None = None,
    ) -> None:
        raise NotImplementedError()
