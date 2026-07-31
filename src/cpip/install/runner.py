"""Helpers for invoking the currently running cpip from a subprocess."""

import os
from pathlib import Path


cpip_runner: str | None = None


def set_cpip_runner(path: str) -> None:
    """Set the runner path when cpip is executing from a source checkout."""
    global cpip_runner
    cpip_runner = path
    from cpip.cli._execution_context import configure

    configure(runner=path)


def get_runnable_pip() -> str:
    """Return the cpip runner script used for isolated build dependencies."""
    from cpip.cli._execution_context import current_runner

    runner_override = cpip_runner or current_runner()
    if runner_override is not None:
        return runner_override
    import importlib.util

    from cpip.core.cpip_version import get_cpip_distribution

    runner = Path(str(get_cpip_distribution().locate_file("cpip/__cpip-runner__.py")))
    if runner.is_file():
        return os.fsdecode(runner.resolve())

    # Editable source environments do not have the runner in site-packages.
    cpip_spec = importlib.util.find_spec("cpip")
    if cpip_spec is None or cpip_spec.origin is None:
        raise RuntimeError("cpip runner could not be located")
    return os.fsdecode(Path(cpip_spec.origin).resolve().with_name("__cpip-runner__.py"))
