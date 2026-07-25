"""Staged filesystem transactions for package installation."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pip.core.errors import InstallationError


@dataclass(frozen=True, slots=True)
class StagedFile:
    source: Path
    destination: Path
    mode: int | None = None


class InstallTransaction:
    """Validate, apply, and roll back a set of filesystem replacements."""

    def __init__(self, *, owned_paths: Iterable[str | Path] = ()) -> None:
        self._owned = {_normalized(path) for path in owned_paths}
        self._staged: list[StagedFile] = []
        self._deletions: set[Path] = set()
        self._backups: list[tuple[Path, Path]] = []
        self._created: list[Path] = []
        self._temporary = Path(tempfile.mkdtemp(prefix="pip-install-stage-"))
        self._finished = False

    def add(
        self, source: str | Path, destination: str | Path, *, mode: int | None = None
    ) -> None:
        destination_path = Path(destination)
        if any(item.destination == destination_path for item in self._staged):
            raise InstallationError(
                f"duplicate installation destination: {destination_path}"
            )
        self._staged.append(StagedFile(Path(source), destination_path, mode))

    def delete(self, path: str | Path) -> None:
        self._deletions.add(Path(path))

    def validate(self) -> None:
        destinations = {item.destination for item in self._staged}
        for item in self._staged:
            if not item.source.is_file():
                raise InstallationError(f"staged file does not exist: {item.source}")
            if (
                item.destination.exists()
                and _normalized(item.destination) not in self._owned
            ):
                if (
                    item.destination.is_file()
                    and item.destination.read_bytes() == item.source.read_bytes()
                ):
                    continue
                raise InstallationError(
                    f"Cannot install {item.destination} from {item.source}: "
                    "an unrelated file already exists"
                )
        overlap = destinations & self._deletions
        if overlap:
            raise InstallationError(
                f"installation both replaces and deletes: {next(iter(overlap))}"
            )

    def commit(self, *, finalize: bool = True) -> None:
        if self._finished:
            raise RuntimeError("installation transaction has already finished")
        try:
            self.validate()
            for item in self._staged:
                self._backup_if_needed(item.destination)
                item.destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.source, item.destination)
                if item.mode is not None:
                    item.destination.chmod(item.mode)
                self._created.append(item.destination)
            for path in sorted(self._deletions, key=os.fspath):
                if path.exists() or path.is_symlink():
                    self._backup_if_needed(path)
                self._remove_empty_parents(path.parent)
            if finalize:
                self._finish_successfully()
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        for path in reversed(self._created):
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for original, backup in reversed(self._backups):
            if backup.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                if original.exists() or original.is_symlink():
                    original.unlink()
                shutil.move(os.fspath(backup), os.fspath(original))
        self._finish_successfully()

    def finalize(self) -> None:
        """Discard retained rollback state after a batch succeeds."""
        if not self._finished:
            self._finish_successfully()

    def _backup_if_needed(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        backup = self._temporary / str(len(self._backups))
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(path), os.fspath(backup))
        self._backups.append((path, backup))

    def _remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != current.parent:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _finish_successfully(self) -> None:
        shutil.rmtree(self._temporary, ignore_errors=True)
        self._finished = True

    def __enter__(self) -> InstallTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._finished:
            self.rollback()


def _normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))
