"""Requirement-line and editable requirement parsing."""

from __future__ import annotations

import os
import urllib.parse

from cpip.core.errors import InstallationError, InvalidWheelFilename
from cpip.core.packaging import Requirement as ParsedRequirement
from cpip.core.packaging import parse_requirement
from cpip.core.urls import path_to_url
from cpip.index.links import Link
from cpip.resolution.engine.input.paths import (
    get_url_from_path,
    looks_like_path,
    normalize_file_url_reference,
)
from cpip.resolution.req_install import InstallRequirement


def install_req_from_line(
    line: str,
    *,
    comes_from: InstallRequirement | str | None = None,
    constraint: bool = False,
    isolated: bool = False,
    user_supplied: bool = False,
    hash_options: dict[str, list[str]] | None = None,
    config_settings: dict[str, object] | None = None,
    permit_editable_wheels: bool = False,
) -> InstallRequirement:
    text = line.strip()
    path_extras: frozenset[str] = frozenset()
    path_text = text
    if "[" in text and text.endswith("]") and " @ " not in text:
        maybe_path, extras_text = text[:-1].split("[", 1)
        extras = frozenset(
            item.strip() for item in extras_text.split(",") if item.strip()
        )
        if extras and (
            looks_like_path(maybe_path)
            or os.path.isdir(maybe_path)
            or os.path.exists(maybe_path)
        ):
            path_text = maybe_path
            path_extras = extras
    if "@" in text and "://" in text:
        try:
            parsed = parse_requirement(text)
        except ValueError:
            pass
        else:
            marker = parsed.marker
            if marker is not None:
                parsed = ParsedRequirement(
                    name=parsed.name,
                    specifier=parsed.specifier,
                    extras=parsed.extras,
                    url=parsed.url,
                    marker=None,
                    raw=parsed.raw,
                )
            if parsed.url is not None:
                return InstallRequirement(
                    parsed,
                    comes_from=comes_from,
                    link=Link(parsed.url),
                    marker_internal=marker,
                    isolated=isolated,
                    user_supplied=user_supplied,
                    hash_options=hash_options or {},
                    constraint=constraint,
                    config_settings=config_settings,
                    permit_editable_wheels=permit_editable_wheels,
                )
    if "://" in text:
        marker_index = text.find("; ")
        if marker_index == -1:
            url, marker = text, None
        else:
            prefix = text[:marker_index]
            parsed_url = urllib.parse.urlparse(prefix)
            if not parsed_url.scheme:
                url, marker = text, None
            else:
                url, marker = prefix, text[marker_index + 2 :].strip()
        parsed = parse_requirement(url)
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(url),
            marker_internal=marker,
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    if looks_like_path(path_text):
        url = get_url_from_path(path_text, path_text)
        if url is None:
            raise InstallationError(
                f"Invalid requirement: {text!r}. It looks like a path.",
            )
        if os.path.isfile(path_text) and path_text.endswith(".txt"):
            raise InstallationError(
                f"Invalid requirement: {text!r}. It looks like a path. The path does exist. "
                "The argument appears to be a requirements file. If that is the case, use the '-r' flag to install",
            )
        parsed = parse_requirement(url)
        if os.path.splitext(path_text)[1].lower() == ".whl":
            wheel_parts = os.path.basename(path_text)[:-4].split("-")
            if len(wheel_parts) >= 2:
                wheel_requirement = parse_requirement(
                    f"{wheel_parts[0]}=={wheel_parts[1]}",
                )
                parsed = ParsedRequirement(
                    name=wheel_requirement.name,
                    specifier=wheel_requirement.specifier,
                    extras=path_extras,
                    url=url,
                    marker=parsed.marker,
                    raw=text,
                )
        if path_extras:
            parsed = ParsedRequirement(
                name=parsed.name,
                specifier=parsed.specifier,
                extras=path_extras,
                url=parsed.url,
                marker=parsed.marker,
                raw=text,
            )
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(url),
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    if text.endswith(".whl") and "@" not in text and "://" not in text:
        parts = text[:-4].split("-")
        if len(parts) < 5:
            raise InvalidWheelFilename(text)
        parsed = parse_requirement(f"{parts[0]}=={parts[1]}")
        return InstallRequirement(
            parsed,
            comes_from=comes_from,
            link=Link(text),
            isolated=isolated,
            user_supplied=user_supplied,
            hash_options=hash_options or {},
            constraint=constraint,
            config_settings=config_settings,
            permit_editable_wheels=permit_editable_wheels,
        )
    try:
        parsed = parse_requirement(text)
    except ValueError as exc:
        message = f"Invalid requirement: {text!r}"
        if "=" in text and "==" not in text:
            message += ". = is not a valid operator. Did you mean == ?"
        raise InstallationError(message) from exc
    if parsed.marker:
        quote: str | None = None
        for char in parsed.marker:
            if char in {"'", '"'}:
                quote = None if quote == char else char
            elif char == ";" and quote is None:
                raise InstallationError(f"Invalid requirement: {text!r}")
    marker = parsed.marker
    if marker is not None:
        parsed = ParsedRequirement(
            name=parsed.name,
            specifier=parsed.specifier,
            extras=parsed.extras,
            url=parsed.url,
            marker=None,
            raw=parsed.raw,
        )
    return InstallRequirement(
        parsed,
        comes_from=comes_from,
        link=(
            Link(parsed.url) if parsed.url else Link(text) if "://" in text else None
        ),
        marker_internal=marker,
        isolated=isolated,
        user_supplied=user_supplied,
        hash_options=hash_options or {},
        constraint=constraint,
        config_settings=config_settings,
        permit_editable_wheels=permit_editable_wheels,
    )


def install_req_from_editable(
    value: str,
    *,
    comes_from: InstallRequirement | str | None = None,
    isolated: bool = False,
    user_supplied: bool = False,
    constraint: bool = False,
    permit_editable_wheels: bool = False,
    hash_options: dict[str, list[str]] | None = None,
    config_settings: dict[str, object] | None = None,
) -> InstallRequirement:
    name, url, extras = parse_editable(value)
    marker: str | None = None
    if name is not None and " ; " in name:
        name, marker = name.split(" ; ", 1)
    if name is None:
        parsed = parse_requirement(
            f"editable-placeholder[{','.join(sorted(extras))}] @ {url}"
            if extras
            else f"editable-placeholder @ {url}",
        )
    else:
        parsed = parse_requirement(
            f"{name}[{','.join(sorted(extras))}] @ {url}"
            if extras
            else f"{name} @ {url}",
        )
    return InstallRequirement(
        parsed,
        comes_from=comes_from,
        link=Link(url),
        marker_internal=marker or parsed.marker,
        editable=True,
        isolated=isolated,
        user_supplied=user_supplied,
        constraint=constraint,
        hash_options=hash_options or {},
        permit_editable_wheels=permit_editable_wheels,
        config_settings=config_settings,
    )


def parse_editable(value: str) -> tuple[str | None, str, set[str]]:
    stripped = value.strip()
    if " @ " in stripped:
        parsed = parse_requirement(stripped)
        requirement_text = parsed.name
        if parsed.marker:
            requirement_text += f" ; {parsed.marker}"
        return requirement_text, parsed.url or "", set(parsed.extras)
    if "#egg=" in stripped:
        base, fragment = stripped.split("#", 1)
        fragment_values = urllib.parse.parse_qs(fragment, keep_blank_values=True)
        egg = fragment_values.get("egg", [""])[0]
        remaining_fragment = urllib.parse.urlencode(
            [
                (key, value)
                for key, values in fragment_values.items()
                if key != "egg"
                for value in values
            ],
        )
        url = base + (f"#{remaining_fragment}" if remaining_fragment else "")
        url = normalize_file_url_reference(stripped) or stripped
        if "[" in egg and egg.endswith("]"):
            name, extras_text = egg[:-1].split("[", 1)
            return (
                name,
                url,
                {item.strip() for item in extras_text.split(",") if item.strip()},
            )
        return egg, url, set()
    extras: set[str] = set()
    path_part = stripped
    if "[" in stripped and stripped.endswith("]"):
        path_part, extras_text = stripped[:-1].split("[", 1)
        extras = {item.strip() for item in extras_text.split(",") if item.strip()}
    if (
        looks_like_path(path_part)
        or os.path.isdir(path_part)
        or os.path.exists(path_part)
    ):
        normalized = normalize_file_url_reference(path_part)
        if normalized is not None:
            return None, normalized, extras
        return (
            None,
            path_to_url(os.path.realpath(os.path.abspath(path_part))),
            extras,
        )
    return None, stripped, extras
