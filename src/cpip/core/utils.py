"""Consolidated core utilities: python environment, filesystem, and context helpers."""

from __future__ import annotations

import errno
import marshal
import os
import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


AuthInfo = tuple[str | None, str | None]


def enum(*sequential: str, **named: str) -> Any:
    values: dict[str, object] = dict(zip(sequential, range(len(sequential))), **named)
    values["reverse_mapping"] = {value: key for key, value in values.items()}
    return type("Enum", (), values)


class ExecutionContext:
    __slots__ = ("version",)

    def __init__(self) -> None:
        self.version: str | None = None


context = ExecutionContext()


def configure(*, version: str | None = None) -> None:
    if version is not None:
        context.version = version


def current_version() -> str | None:
    return context.version


CURRENT_PYTHON_VERSION_INFO = sys.version_info
CURRENT_PYTHON_VERSION = (
    f"{CURRENT_PYTHON_VERSION_INFO.major}.{CURRENT_PYTHON_VERSION_INFO.minor}"
)
CURRENT_PYTHON_VERSION_DIGITS = CURRENT_PYTHON_VERSION.replace(".", "")
CURRENT_PYTHON_VERSION_FULL = ".".join(
    str(part) for part in CURRENT_PYTHON_VERSION_INFO[:3]
)
CURRENT_PYTHON_MAJOR_TAG = f"py{CURRENT_PYTHON_VERSION_INFO.major}"
CURRENT_PYTHON_FULL_TAG = f"py{CURRENT_PYTHON_VERSION_DIGITS}"

# marshal's wire format is not guaranteed stable across interpreters or
# Python versions -- reading data written by a different one is documented
# as unsafe, not just failure-prone. Fold this into a persisted cache's
# storage name (bucket, filename, or key prefix) so a mismatched interpreter
# reads nothing rather than something unsafe. Deterministic across process
# restarts (unlike hash()), so it is safe to use as part of an on-disk name.
CACHE_INTERPRETER_TAG = f"{sys.implementation.name}-{CURRENT_PYTHON_VERSION_DIGITS}"


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def display_path(path: str) -> str:
    if not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, os.getcwd())
    except ValueError:
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    return os.path.join(".", relative)


def load_snapshot(path: str | os.PathLike[str]) -> object | None:
    """Load a marshal snapshot, treating missing or corrupt data as empty."""
    try:
        with open(path, "rb") as stream:
            return marshal.load(stream)
    except (EOFError, OSError, TypeError, ValueError):
        return None


def save_snapshot(path: str | os.PathLike[str], payload: object) -> bool:
    """Atomically write a marshal snapshot and report whether it succeeded."""
    path = os.fspath(path)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temporary, "wb") as stream:
            marshal.dump(payload, stream)  # ty: ignore[invalid-argument-type]
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
