from __future__ import annotations

import logging
import os

from pyproject_hooks import BuildBackendHookCaller, HookMissing
from typing import Any

from pip.core.subprocess import runner_with_message

logger = logging.getLogger(__name__)


def build_wheel_pep517(
    name: str,
    backend: Any,
    metadata_directory: str,
    wheel_directory: str,
    *,
    editable: bool = False,
) -> str | None:
    """Build one InstallRequirement using the PEP 517/660 build process.

    Returns path to wheel if successfully built. Otherwise, returns None.
    """
    assert metadata_directory is not None
    artifact = "editable" if editable else "wheel"
    try:
        logger.debug("Destination directory: %s", wheel_directory)

        runner = runner_with_message(f"Building {artifact} for {name} (pyproject.toml)")
        with backend.subprocess_runner(runner):
            if editable:
                try:
                    wheel_name = backend.build_editable(
                        wheel_directory=wheel_directory,
                        metadata_directory=metadata_directory,
                    )
                except HookMissing as exc:
                    logger.error(
                        "Cannot build editable %s because the build backend "
                        "does not have the %s hook",
                        name,
                        exc,
                    )
                    return None
            else:
                wheel_name = backend.build_wheel(
                    wheel_directory=wheel_directory,
                    metadata_directory=metadata_directory,
                )
    except Exception:
        logger.error("Failed building %s for %s", artifact, name)
        return None
    return os.path.join(wheel_directory, wheel_name)
