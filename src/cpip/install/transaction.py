"""Staged filesystem transactions for package installation."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable

from cpip.core.errors import InstallationError


def _read_staged_source(path: str | None) -> bytes:
    if path is None:
        raise ValueError("staged source path is missing")
    with open(path, "rb") as source_file:
        return source_file.read()


class StagedFile:
    __slots__ = (
        "clone",
        "contents",
        "destination_text",
        "mode",
        "source_text",
    )

    def __init__(
        self,
        source_text: str | None,
        destination_text: str,
        mode: int | None = None,
        contents: bytes | None = None,
        clone: bool = False,
    ) -> None:
        if source_text is None and contents is None:
            raise ValueError("staged file needs a source or contents")
        self.contents = contents
        self.clone = clone
        self.source_text = source_text
        self.destination_text = destination_text
        self.mode = mode


class InstallTransaction:
    """Validate, apply, and roll back a set of filesystem replacements."""

    def __init__(self, *, owned_paths: Iterable[str] = ()) -> None:
        self.owned = {normalized_internal(path) for path in owned_paths}
        self.staged_internal: list[StagedFile] = []
        self.staged_destinations: set[str] = set()
        self.deletions: set[str] = set()
        self.backups: list[tuple[str, str]] = []
        self.created_internal: list[str] = []
        self.destination_presence: dict[str, bool] = {}
        self.temporary_internal = tempfile.mkdtemp(prefix="cpip-install-stage-")
        self.finished = False

    def add(
        self,
        source: str,
        destination: str,
        *,
        mode: int | None = None,
    ) -> None:
        source_text = source if isinstance(source, str) else os.fspath(source)
        destination_text = (
            destination if isinstance(destination, str) else os.fspath(destination)
        )
        if destination_text in self.staged_destinations:
            raise InstallationError(
                f"duplicate installation destination: {destination_text}",
            )
        self.staged_internal.append(StagedFile(source_text, destination_text, mode))
        self.staged_destinations.add(destination_text)

    def add_contents(
        self,
        destination: str,
        contents: bytes,
        *,
        mode: int | None = None,
    ) -> None:
        """Stage bytes for one destination without creating a temp source file."""
        destination_text = (
            destination if isinstance(destination, str) else os.fspath(destination)
        )
        if destination_text in self.staged_destinations:
            raise InstallationError(
                f"duplicate installation destination: {destination_text}",
            )
        self.staged_internal.append(
            StagedFile(None, destination_text, mode, contents=contents),
        )
        self.staged_destinations.add(destination_text)

    def add_clone(
        self,
        source: str,
        destination: str,
        *,
        mode: int | None = None,
    ) -> None:
        """Stage an immutable cache source without consuming it at commit."""
        source_text = source if isinstance(source, str) else os.fspath(source)
        destination_text = (
            destination if isinstance(destination, str) else os.fspath(destination)
        )
        if destination_text in self.staged_destinations:
            raise InstallationError(
                f"duplicate installation destination: {destination_text}",
            )
        self.staged_internal.append(
            StagedFile(source_text, destination_text, mode, clone=True),
        )
        self.staged_destinations.add(destination_text)

    def delete(self, path: str) -> None:
        self.deletions.add(os.fspath(path))

    def adopt(self, other: InstallTransaction) -> None:
        """Merge staged actions from a transaction that has not committed."""
        self.owned.update(other.owned)
        self.created_internal.extend(other.created_internal)
        for item in other.staged_internal:
            if item.contents is None:
                assert item.source_text is not None
                operation = self.add_clone if item.clone else self.add
                operation(item.source_text, item.destination_text, mode=item.mode)
            else:
                self.add_contents(item.destination_text, item.contents, mode=item.mode)
        self.deletions.update(other.deletions)

    def record_created(self, destination: str) -> None:
        """Record a path written directly for rollback by the caller."""
        self.created_internal.append(os.fspath(destination))

    def validate(self) -> None:
        for item in self.staged_internal:
            if item.source_text is not None and not os.path.isfile(item.source_text):
                raise InstallationError(
                    f"staged file does not exist: {item.source_text}",
                )
            destination_text = item.destination_text
            try:
                destination_stat = os.stat(destination_text)
            except OSError:
                destination_exists = os.path.lexists(destination_text)
                destination_visible = False
                destination_is_file = False
            else:
                destination_exists = True
                destination_visible = True
                destination_is_file = stat.S_ISREG(destination_stat.st_mode)
            self.destination_presence[destination_text] = destination_exists
            if (
                destination_exists
                and destination_visible
                and normalized_internal(item.destination_text) not in self.owned
            ):
                if destination_is_file:
                    with open(destination_text, "rb") as destination_file:
                        destination_contents = destination_file.read()
                    source_contents = (
                        item.contents
                        if item.contents is not None
                        else _read_staged_source(item.source_text)
                    )
                    if destination_contents == source_contents:
                        continue
                raise InstallationError(
                    f"Cannot install {item.destination_text} from {item.source_text}: "
                    "an unrelated file already exists",
                )
        overlap = self.staged_destinations & self.deletions
        if overlap:
            raise InstallationError(
                f"installation both replaces and deletes: {next(iter(overlap))}",
            )

    def commit(self, *, finalize: bool = True) -> None:
        if self.finished:
            raise RuntimeError("installation transaction has already finished")
        try:
            self.validate()
            created_directories: set[str] = set()
            backup_if_needed = self.backup_if_needed
            makedirs = os.makedirs
            replace = os.replace
            chmod = os.chmod
            append_created = self.created_internal.append
            for item in self.staged_internal:
                backup_if_needed(item.destination_text)
                destination_parent_text = (
                    os.path.dirname(item.destination_text) or os.curdir
                )
                if destination_parent_text not in created_directories:
                    makedirs(destination_parent_text, exist_ok=True)
                    created_directories.add(destination_parent_text)
                if item.contents is None:
                    assert item.source_text is not None
                    if item.clone:
                        from cpip.platform.clone import clone_path

                        clone_path(item.source_text, item.destination_text)
                    else:
                        try:
                            replace(item.source_text, item.destination_text)
                        except OSError as exc:
                            if exc.errno != errno.EXDEV:
                                raise
                            shutil.move(item.source_text, item.destination_text)
                else:
                    # Mark the path before writing: a short write must still
                    # be removed by rollback before any backup is restored.
                    append_created(item.destination_text)
                    with open(item.destination_text, "wb") as destination_file:
                        destination_file.write(item.contents)
                if item.mode is not None:
                    chmod(item.destination_text, item.mode)
                if item.contents is None:
                    append_created(item.destination_text)
            for path in sorted(self.deletions):
                self.backup_if_needed(path)
                self.remove_empty_parents(os.path.dirname(path))
            if finalize:
                self.finish_successfully()
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        for path in reversed(self.created_internal):
            try:
                path_stat = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(
                path_stat.st_mode,
            ):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        for original, backup in reversed(self.backups):
            if os.path.exists(backup):
                os.makedirs(os.path.dirname(original) or os.curdir, exist_ok=True)
                if os.path.lexists(original):
                    os.unlink(original)
                shutil.move(backup, original)
        self.finish_successfully()

    def finalize(self) -> None:
        """Discard retained rollback state after a batch succeeds."""
        if not self.finished:
            self.finish_successfully()

    def backup_if_needed(self, path: str) -> None:
        path_text = path if isinstance(path, str) else os.fspath(path)
        if path_text in self.destination_presence:
            if not self.destination_presence[path_text]:
                return
        elif not os.path.lexists(path_text):
            return
        backup = os.path.join(self.temporary_internal, str(len(self.backups)))
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.move(path_text, backup)
        self.backups.append((path_text, backup))

    def remove_empty_parents(self, directory: str) -> None:
        current = directory
        while current and current != os.path.dirname(current):
            try:
                os.rmdir(current)
            except OSError:
                return
            current = os.path.dirname(current)

    def finish_successfully(self) -> None:
        shutil.rmtree(self.temporary_internal, ignore_errors=True)
        self.finished = True

    def __enter__(self) -> InstallTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.finished:
            self.rollback()


def normalized_internal(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))
