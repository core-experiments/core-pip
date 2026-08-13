from __future__ import annotations

import logging
import os.path
import stat
import tempfile
import traceback
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from typing import Any, TypeVar

from cpip.core.utils import enum

logger = logging.getLogger(__name__)
T_internal = TypeVar("T_internal", bound="TempDirectory")


def rmtree(path: str, ignore_errors: bool = False, onexc=None) -> None:
    if onexc is None:

        def onexc(func_internal, filename_internal, exc):
            raise exc

    for root, dirs, files in os.walk(path, topdown=False):
        for filename in files:
            target = os.path.join(root, filename)
            try:
                os.unlink(target)
            except OSError as exc:
                try:
                    os.chmod(target, stat.S_IWRITE)
                    os.unlink(target)
                except OSError:
                    if not ignore_errors:
                        onexc(os.unlink, target, exc)
        for dirname in dirs:
            target = os.path.join(root, dirname)
            try:
                os.rmdir(target)
            except OSError as exc:
                if not ignore_errors:
                    onexc(os.rmdir, target, exc)
    try:
        os.rmdir(path)
    except OSError as exc:
        if not ignore_errors:
            onexc(os.rmdir, path, exc)


def remove_temp_directory(path: str | os.PathLike[str]) -> None:
    """Remove a temporary directory, including read-only VCS files."""
    path_text = os.fspath(path)
    try:
        rmtree(path_text)
    except FileNotFoundError:
        return
    except OSError:
        rmtree(path_text, ignore_errors=True)


tempdir_kinds = enum(
    BUILD_ENV="build-env",
    REQ_BUILD="req-build",
)
tempdir_manager: ExitStack | None = None


@contextmanager
def global_tempdir_manager() -> Generator[None, None, None]:
    global tempdir_manager
    with ExitStack() as stack:
        old_tempdir_manager, tempdir_manager = tempdir_manager, stack
        try:
            yield
        finally:
            tempdir_manager = old_tempdir_manager


class Default_internal:
    pass


default_internal = Default_internal()


class TempDirectory:
    def __init__(
        self,
        path: str | None = None,
        delete: bool | None | Default_internal = default_internal,
        kind: str = "temp",
        globally_managed: bool = False,
        ignore_cleanup_errors: bool = True,
    ):
        if delete is default_internal:
            delete = False if path is not None else None
        if path is None:
            path = self.create_internal(kind)
        self.path_internal = path
        self.deleted_internal = False
        self.delete = delete
        self.kind = kind
        self.ignore_cleanup_errors = ignore_cleanup_errors
        if globally_managed:
            assert tempdir_manager is not None
            tempdir_manager.enter_context(self)

    @property
    def path(self) -> str:
        assert not self.deleted_internal, (
            f"Attempted to access deleted path: {self.path_internal}"
        )
        return self.path_internal

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.path!r}>"

    def __enter__(self: T_internal) -> T_internal:
        return self

    def __exit__(self, exc: Any, value: Any, tb: Any) -> None:
        if (self.delete is not None and self.delete) or self.delete is None:
            self.cleanup()

    def create_internal(self, kind: str) -> str:
        path = os.path.realpath(tempfile.mkdtemp(prefix=f"cpip-{kind}-"))
        logger.debug("Created temporary directory: %s", path)
        return path

    def cleanup(self) -> None:
        self.deleted_internal = True
        try:
            rmtree(self.path_internal)
        except FileNotFoundError:
            return
        except OSError:
            pass
        else:
            return
        errors: list[BaseException] = []

        def onerror(
            func: Callable[..., Any],
            path: str,
            exc_val: BaseException,
        ) -> None:
            formatted = "".join(
                traceback.format_exception_only(type(exc_val), exc_val),
            ).rstrip()
            logger.debug(
                "Failed to remove temporary file '%s' due to %s.",
                path,
                formatted,
            )
            errors.append(exc_val)

        if self.ignore_cleanup_errors:
            try:
                rmtree(self.path_internal, ignore_errors=False)
            except OSError:
                rmtree(self.path_internal, onexc=onerror)
            if errors:
                logger.warning(
                    "Failed to remove contents in a temporary directory '%s'.",
                    self.path_internal,
                )
        else:
            rmtree(self.path_internal)
