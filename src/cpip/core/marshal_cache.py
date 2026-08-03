"""Small helpers for resilient, atomic marshal snapshots."""

from __future__ import annotations

import marshal
import os


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
