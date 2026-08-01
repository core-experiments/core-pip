"""Requirement-file option and reference normalization."""

from __future__ import annotations

import ntpath
import os
import re
import urllib.parse
from typing import cast

from cpip.core.urls import path_to_url
from cpip.resolution.engine.input.files.models import RequirementsFileParseError


def strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def add_hash_option(
    target: dict[str, list[str]],
    raw: str,
    *,
    original_line: str,
) -> None:
    name, sep, digest = raw.partition(":")
    if not sep or not digest:
        raise RequirementsFileParseError(original_line)
    target.setdefault(name, []).append(digest)


def merge_config_setting(target: dict[str, object], raw: str) -> None:
    key, _, value = raw.partition("=")
    key = key.strip()
    existing = target.get(key)
    if existing is None:
        target[key] = value if _ else ""
    elif isinstance(existing, list):
        values = cast("list[str]", existing)
        values.append(value if _ else "")
    else:
        target[key] = [existing, value if _ else ""]


def normalize_reference(value: str, base: str | None, *, as_path: bool = False) -> str:
    value = expand_env_variables(value.strip())
    parsed = urllib.parse.urlparse(value)
    windows_path = bool(
        ntpath.splitdrive(value)[0] and len(value) > 2 and value[2] in "/\\",
    )
    base_parsed = urllib.parse.urlparse(base) if base else None
    base_directory = base.rsplit("/", 1)[0] + "/" if base else None
    if parsed.scheme and not windows_path:
        if (
            parsed.scheme == "file"
            and base
            and not parsed.netloc
            and not parsed.path.startswith("/")
        ):
            base_path = os.path.dirname(os.path.realpath(base))
            return path_to_url(os.path.join(base_path, parsed.path)) + (
                f"#{parsed.fragment}" if parsed.fragment else ""
            )
        if base_parsed is not None and base_parsed.scheme:
            assert base_directory is not None
            return urllib.parse.urljoin(base_directory, value)
        return value
    if (
        not as_path
        and not any(sep in value for sep in ("/", os.sep))
        and not value.startswith(".")
    ):
        return value
    path = os.path.expanduser(value)
    if base and not os.path.isabs(path):
        if base_parsed is not None and base_parsed.scheme:
            assert base_directory is not None
            return urllib.parse.urljoin(base_directory, value)
        path = os.path.join(os.path.dirname(os.path.realpath(base)), path)
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def expand_env_variables(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = os.getenv(name)
        return match.group(0) if replacement in {None, ""} else str(replacement)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
