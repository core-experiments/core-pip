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
        self.owned = {normalized_internal(path) for path in owned_paths}
        self.staged_internal: list[StagedFile] = []
        self.staged_destinations: set[Path] = set()
        self.deletions: set[Path] = set()
        self.backups: list[tuple[Path, Path]] = []
        self.created_internal: list[Path] = []
        self.temporary_internal = Path(tempfile.mkdtemp(prefix="pip-install-stage-"))
        self.finished = False

    def add(
        self, source: str | Path, destination: str | Path, *, mode: int | None = None
    ) -> None:
        destination_path = Path(destination)
        if destination_path in self.staged_destinations:
            raise InstallationError(
                f"duplicate installation destination: {destination_path}"
            )
        self.staged_internal.append(StagedFile(Path(source), destination_path, mode))
        self.staged_destinations.add(destination_path)

    def delete(self, path: str | Path) -> None:
        self.deletions.add(Path(path))

    def validate(self) -> None:
        for item in self.staged_internal:
            if not item.source.is_file():
                raise InstallationError(f"staged file does not exist: {item.source}")
            if (
                item.destination.exists()
                and normalized_internal(item.destination) not in self.owned
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
        overlap = self.staged_destinations & self.deletions
        if overlap:
            raise InstallationError(
                f"installation both replaces and deletes: {next(iter(overlap))}"
            )

    def commit(self, *, finalize: bool = True) -> None:
        if self.finished:
            raise RuntimeError("installation transaction has already finished")
        try:
            self.validate()
            for item in self.staged_internal:
                self.backup_if_needed(item.destination)
                item.destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(os.fspath(item.source), os.fspath(item.destination))
                if item.mode is not None:
                    item.destination.chmod(item.mode)
                self.created_internal.append(item.destination)
            for path in sorted(self.deletions, key=os.fspath):
                if path.exists() or path.is_symlink():
                    self.backup_if_needed(path)
                self.remove_empty_parents(path.parent)
            if finalize:
                self.finish_successfully()
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        for path in reversed(self.created_internal):
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for original, backup in reversed(self.backups):
            if backup.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                if original.exists() or original.is_symlink():
                    original.unlink()
                shutil.move(os.fspath(backup), os.fspath(original))
        self.finish_successfully()

    def finalize(self) -> None:
        """Discard retained rollback state after a batch succeeds."""
        if not self.finished:
            self.finish_successfully()

    def backup_if_needed(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        backup = self.temporary_internal / str(len(self.backups))
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(path), os.fspath(backup))
        self.backups.append((path, backup))

    def remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != current.parent:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def finish_successfully(self) -> None:
        shutil.rmtree(self.temporary_internal, ignore_errors=True)
        self.finished = True

    def __enter__(self) -> InstallTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.finished:
            self.rollback()


def normalized_internal(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))
