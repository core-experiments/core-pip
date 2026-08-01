"""Path and file-URL normalization for requirements."""

from __future__ import annotations

import ntpath
import os
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


def get_url_from_path(path: str, name: str) -> str | None:
    parsed = urllib.parse.urlparse(path)
    if parsed.scheme == "file":
        local_path = path_from_file_url(parsed)
        if os.path.isfile(local_path):
            return file_url_with_fragment(local_path, parsed.fragment)
        if os.path.isdir(local_path):
            setup_py = os.path.join(local_path, "setup.py")
            pyproject = os.path.join(local_path, "pyproject.toml")
            if not os.path.isfile(setup_py) and not os.path.isfile(pyproject):
                raise InstallationError(
                    "Neither 'setup.py' nor 'pyproject.toml' found.",
                )
            return file_url_with_fragment(local_path, parsed.fragment)
        return None
    if " @ " in path or "@git+" in path or ("://" in path and not os.path.exists(path)):
        return None
    if os.path.isfile(path):
        return path_to_url(os.path.realpath(path))
    if os.path.isdir(path):
        setup_py = os.path.join(path, "setup.py")
        pyproject = os.path.join(path, "pyproject.toml")
        if not os.path.isfile(setup_py) and not os.path.isfile(pyproject):
            raise InstallationError("Neither 'setup.py' nor 'pyproject.toml' found.")
        return path_to_url(os.path.realpath(path))
    return None


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
