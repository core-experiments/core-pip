"""Helpers for invoking the currently running pip from a subprocess."""

import importlib.metadata
import importlib.util
import os
from pathlib import Path


_pip_runner: str | None = None


def set_pip_runner(path: str) -> None:
    """Set the runner path when pip is executing from a source checkout."""
    global _pip_runner
    _pip_runner = path


def get_runnable_pip() -> str:
    """Return the pip runner script used for isolated build dependencies."""
    if _pip_runner is not None:
        return _pip_runner
    runner = Path(
        importlib.metadata.distribution("pip").locate_file("pip/__pip-runner__.py")
    )
    if runner.is_file():
        return os.fsdecode(runner.resolve())

    # Editable source environments do not have the runner in site-packages.
    pip_spec = importlib.util.find_spec("pip")
    if pip_spec is None or pip_spec.origin is None:
        raise RuntimeError("pip runner could not be located")
    return os.fsdecode(Path(pip_spec.origin).resolve().with_name("__pip-runner__.py"))
