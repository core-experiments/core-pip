"""Staged filesystem transactions for package installation."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from cpip.core.errors import InstallationError


class StagedFile:
    __slots__ = (
        "source",
        "contents",
        "destination",
        "source_text",
        "destination_text",
        "mode",
    )

    def __init__(
        self,
        source: Path | None,
        destination: Path,
        mode: int | None = None,
        contents: bytes | None = None,
    ) -> None:
        if source is None and contents is None:
            raise ValueError("staged file needs a source or contents")
        self.source = source
        self.contents = contents
        self.destination = destination
        self.source_text = os.fspath(source) if source is not None else None
        self.destination_text = os.fspath(destination)
        self.mode = mode


class InstallTransaction:
    """Validate, apply, and roll back a set of filesystem replacements."""

    def __init__(self, *, owned_paths: Iterable[str | Path] = ()) -> None:
        self.owned = {normalized_internal(path) for path in owned_paths}
        self.staged_internal: list[StagedFile] = []
        self.staged_destinations: set[Path] = set()
        self.deletions: set[Path] = set()
        self.backups: list[tuple[Path, Path]] = []
        self.created_internal: list[Path] = []
        self.destination_presence: dict[Path, bool] = {}
        self.temporary_internal = Path(tempfile.mkdtemp(prefix="cpip-install-stage-"))
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

    def add_contents(
        self,
        destination: str | Path,
        contents: bytes,
        *,
        mode: int | None = None,
    ) -> None:
        """Stage bytes for one destination without creating a temp source file."""
        destination_path = Path(destination)
        if destination_path in self.staged_destinations:
            raise InstallationError(
                f"duplicate installation destination: {destination_path}"
            )
        self.staged_internal.append(
            StagedFile(None, destination_path, mode, contents=contents)
        )
        self.staged_destinations.add(destination_path)

    def delete(self, path: str | Path) -> None:
        self.deletions.add(Path(path))

    def adopt(self, other: InstallTransaction) -> None:
        """Merge staged actions from a transaction that has not committed."""
        self.owned.update(other.owned)
        self.created_internal.extend(other.created_internal)
        for item in other.staged_internal:
            if item.contents is None:
                self.add(item.source, item.destination, mode=item.mode)
            else:
                self.add_contents(item.destination, item.contents, mode=item.mode)
        self.deletions.update(other.deletions)

    def record_created(self, destination: str | Path) -> None:
        """Record a path written directly for rollback by the caller."""
        self.created_internal.append(Path(destination))

    def validate(self) -> None:
        for item in self.staged_internal:
            if item.source is not None and not os.path.isfile(item.source_text):
                raise InstallationError(f"staged file does not exist: {item.source}")
            destination_exists = os.path.lexists(item.destination)
            self.destination_presence[item.destination] = destination_exists
            if (
                destination_exists
                and os.path.exists(item.destination)
                and normalized_internal(item.destination) not in self.owned
            ):
                if os.path.isfile(item.destination):
                    with open(item.destination, "rb") as destination_file:
                        destination_contents = destination_file.read()
                    source_contents = (
                        item.contents
                        if item.contents is not None
                        else item.source.read_bytes()
                    )
                    if destination_contents == source_contents:
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
            created_directories: set[Path] = set()
            backup_if_needed = self.backup_if_needed
            makedirs = os.makedirs
            fspath = os.fspath
            replace = os.replace
            chmod = os.chmod
            append_created = self.created_internal.append
            for item in self.staged_internal:
                backup_if_needed(item.destination)
                destination_parent = item.destination.parent
                if destination_parent not in created_directories:
                    makedirs(fspath(destination_parent), exist_ok=True)
                    created_directories.add(destination_parent)
                if item.contents is None:
                    try:
                        replace(item.source_text, item.destination_text)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        shutil.move(item.source_text, item.destination_text)
                else:
                    # Mark the path before writing: a short write must still
                    # be removed by rollback before any backup is restored.
                    append_created(item.destination)
                    with open(item.destination_text, "wb") as destination_file:
                        destination_file.write(item.contents)
                if item.mode is not None:
                    chmod(item.destination, item.mode)
                if item.contents is None:
                    append_created(item.destination)
            for path in sorted(self.deletions, key=os.fspath):
                if os.path.lexists(path):
                    self.backup_if_needed(path)
                self.remove_empty_parents(path.parent)
            if finalize:
                self.finish_successfully()
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        for path in reversed(self.created_internal):
            if os.path.lexists(path):
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
        for original, backup in reversed(self.backups):
            if os.path.exists(backup):
                os.makedirs(os.fspath(original.parent), exist_ok=True)
                if os.path.lexists(original):
                    os.unlink(original)
                shutil.move(os.fspath(backup), os.fspath(original))
        self.finish_successfully()

    def finalize(self) -> None:
        """Discard retained rollback state after a batch succeeds."""
        if not self.finished:
            self.finish_successfully()

    def backup_if_needed(self, path: Path) -> None:
        if path in self.destination_presence:
            if not self.destination_presence[path]:
                return
        elif not os.path.lexists(path):
            return
        backup = self.temporary_internal / str(len(self.backups))
        os.makedirs(os.fspath(backup.parent), exist_ok=True)
        shutil.move(os.fspath(path), os.fspath(backup))
        self.backups.append((path, backup))

    def remove_empty_parents(self, directory: Path) -> None:
        current = directory
        while current != current.parent:
            try:
                os.rmdir(current)
            except OSError:
                return
            current = current.parent

    def finish_successfully(self) -> None:
        shutil.rmtree(os.fspath(self.temporary_internal), ignore_errors=True)
        self.finished = True

    def __enter__(self) -> InstallTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.finished:
            self.rollback()


def normalized_internal(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))
