"""Staged filesystem transactions for package installation."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable

from cpip.core.errors import InstallationError
from cpip.platform.clone import clone_path


def _read_staged_source(path: str | None) -> bytes:
    if path is None:
        raise ValueError("staged source path is missing")
    with open(path, "rb") as source_file:
        return source_file.read()


def _write_contents(path: str, contents: bytes, mode: int) -> None:
    """Write staged bytes to `path` with raw fd calls.

    A wheel is typically thousands of small files, so the per-file cost of
    building an ``io.BufferedWriter`` (``open`` + fstat + isatty + lseek
    before the first byte) dominates over the actual write. A bare
    os.open/write/close skips all of that per staged file -- the same trade
    ``unpacking._write_stream_to_path`` already makes for archive
    extraction, including its short-write loop: os.write may accept fewer
    bytes than offered even on a regular file.

    `mode` is applied at creation via os.open (the caller decides whether
    the umask makes a follow-up chmod necessary, so it can usually be
    skipped entirely).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        view = memoryview(contents)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("could not write staged file contents")
            view = view[written:]
    finally:
        os.close(fd)


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
            # One lstat answers everything except the symlink case: absent
            # means absent (the old stat + lexists fallback paid a second
            # syscall per file to learn that on every fresh install), and a
            # regular file or directory stats identically through lstat.
            # Only a symlink needs the follow-the-link stat to learn what
            # it points at (or that it dangles).
            try:
                destination_lstat = os.lstat(destination_text)
            except (FileNotFoundError, NotADirectoryError):
                # Only genuine absence (nothing there, or a parent path
                # component is a plain file) may be recorded as absent --
                # that record is what lets backup_if_needed skip the backup,
                # so a permission or I/O failure here must propagate rather
                # than silently authorize an unprotected overwrite later.
                destination_exists = False
                destination_visible = False
                destination_is_file = False
            else:
                destination_exists = True
                if stat.S_ISLNK(destination_lstat.st_mode):
                    try:
                        destination_stat = os.stat(destination_text)
                    except OSError:
                        destination_visible = False
                        destination_is_file = False
                    else:
                        destination_visible = True
                        destination_is_file = stat.S_ISREG(destination_stat.st_mode)
                else:
                    destination_visible = True
                    destination_is_file = stat.S_ISREG(destination_lstat.st_mode)
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
            # Reading the umask means momentarily setting it; probe with
            # all-bits-masked rather than the customary os.umask(0) so a
            # concurrent thread creating a file inside the two-call window
            # gets an over-restricted mode (a visible glitch) instead of an
            # under-restricted one (a silent security hole).
            umask = os.umask(0o777)
            os.umask(umask)
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
                        clone_path(item.source_text, item.destination_text)
                    else:
                        try:
                            replace(item.source_text, item.destination_text)
                        except OSError as exc:
                            if exc.errno != errno.EXDEV:
                                raise
                            shutil.move(item.source_text, item.destination_text)
                    # Record before chmod: if chmod raises, the destination
                    # is already installed and rollback must know to remove
                    # it (and restore any backup underneath).
                    append_created(item.destination_text)
                    if item.mode is not None:
                        chmod(item.destination_text, item.mode)
                else:
                    # Mark the path before writing: a short write must still
                    # be removed by rollback before any backup is restored.
                    append_created(item.destination_text)
                    _write_contents(
                        item.destination_text,
                        item.contents,
                        0o666 if item.mode is None else item.mode,
                    )
                    # os.open creates the file with ``mode & ~umask``, so
                    # when the umask strips none of the requested bits the
                    # file already has exactly the wanted mode and the
                    # follow-up chmod is a per-file syscall for nothing --
                    # the overwhelmingly common case (0o644/0o755 under
                    # umask 022). backup_if_needed moved any pre-existing
                    # destination aside first, so the open always creates.
                    if item.mode is not None and item.mode & umask:
                        chmod(item.destination_text, item.mode)
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
