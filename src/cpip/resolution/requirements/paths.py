"""Path and file-URL normalization for requirements."""

from __future__ import annotations

import ntpath
import os
import urllib.parse
from pathlib import Path

from cpip.core.errors import InstallationError
from cpip.core.urls import url_to_path


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
        if local_path.is_file():
            return file_url_with_fragment(local_path, parsed.fragment)
        if local_path.is_dir():
            setup_py = local_path / "setup.py"
            pyproject = local_path / "pyproject.toml"
            if not setup_py.is_file() and not pyproject.is_file():
                raise InstallationError(
                    "Neither 'setup.py' nor 'pyproject.toml' found."
                )
            return file_url_with_fragment(local_path, parsed.fragment)
        return None
    if " @ " in path or "@git+" in path or "://" in path and not Path(path).exists():
        return None
    if os.path.isfile(path):
        return Path(path).resolve(strict=False).as_uri()
    if os.path.isdir(path):
        setup_py = os.path.join(path, "setup.py")
        pyproject = os.path.join(path, "pyproject.toml")
        if not os.path.isfile(setup_py) and not os.path.isfile(pyproject):
            raise InstallationError("Neither 'setup.py' nor 'pyproject.toml' found.")
        return Path(path).resolve(strict=False).as_uri()
    return None


def normalize_file_url_reference(value: str) -> str | None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    return file_url_with_fragment(path_from_file_url(parsed), parsed.fragment)


def path_from_file_url(parsed: urllib.parse.ParseResult) -> Path:
    path = Path(url_to_path(urllib.parse.urlunparse(parsed)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def file_url_with_fragment(path: Path, fragment: str) -> str:
    url = path.resolve(strict=False).as_uri()
    return f"{url}#{fragment}" if fragment else url
