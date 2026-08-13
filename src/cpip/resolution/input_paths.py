"""Path and file-URL normalization for resolution inputs."""

from __future__ import annotations

import ntpath
import os
import stat
import urllib.parse

from cpip.core.errors import InstallationError
from cpip.core.urls import path_to_url, url_to_path


def looks_like_path(value: str) -> bool:
    return (
        value.startswith((".", "/", "~"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
        or "://" in value
        or " @ " in value
        or bool(ntpath.splitdrive(value)[0])
    )


def get_url_from_path_with_mode(
    path: str,
    name: str,
) -> tuple[str | None, int | None]:
    """Return a normalized path URL and the mode observed while resolving it."""
    parsed = urllib.parse.urlparse(path)
    if parsed.scheme == "file":
        local_path = path_from_file_url(parsed)
        mode = _stat_mode(local_path)
        if mode is None:
            return None, None
        if stat.S_ISREG(mode):
            return file_url_with_fragment(local_path, parsed.fragment), mode
        if stat.S_ISDIR(mode):
            if not _has_project_file(local_path):
                raise InstallationError(
                    "Neither 'setup.py' nor 'pyproject.toml' found.",
                )
            return file_url_with_fragment(local_path, parsed.fragment), mode
        return None, mode
    if " @ " in path or "@git+" in path:
        return None, None
    mode = _stat_mode(path)
    if mode is None:
        return None, None
    if stat.S_ISREG(mode):
        return path_to_url(os.path.realpath(path)), mode
    if stat.S_ISDIR(mode):
        if not _has_project_file(path):
            raise InstallationError("Neither 'setup.py' nor 'pyproject.toml' found.")
        return path_to_url(os.path.realpath(path)), mode
    return None, mode


def _stat_mode(path: str) -> int | None:
    try:
        return os.stat(path).st_mode
    except OSError:
        return None


def _has_project_file(path: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(
                entry.name in {"setup.py", "pyproject.toml"} and entry.is_file()
                for entry in entries
            )
    except OSError:
        return False


def normalize_file_url_reference(value: str) -> str | None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    return file_url_with_fragment(path_from_file_url(parsed), parsed.fragment)


def path_from_file_url(parsed: urllib.parse.ParseResult) -> str:
    path = url_to_path(urllib.parse.urlunparse(parsed))
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    return os.path.realpath(path)


def file_url_with_fragment(path: str, fragment: str) -> str:
    url = path_to_url(os.path.realpath(path))
    return f"{url}#{fragment}" if fragment else url
