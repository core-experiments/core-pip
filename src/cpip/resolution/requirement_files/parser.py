"""Requirements-file loading and line parsing."""

from __future__ import annotations

import codecs
import locale
import logging
import os
import re
import shlex
import sys
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cpip.core.errors import InstallationError
from cpip.resolution.requirement_files.models import (
    ParsedRequirement,
    RequirementsFileParseError,
)
from cpip.resolution.requirement_files.options import (
    add_hash_option,
    expand_env_variables,
    merge_config_setting,
    normalize_reference,
    strip_matching_quotes,
)
from cpip.resolution.requirement_files.pylock import is_pylock_reference, parse_pylock

if TYPE_CHECKING:
    from cpip.index.provider import CandidateProvider
    from cpip.network.http import NetworkSession

logger = logging.getLogger(__name__)
CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
COMMENT_RE = re.compile(r"(^|\s+)#.*$")
REMOTE_SCHEMES = frozenset(("http", "https", "file"))
REQUIREMENTS_OPTIONS = frozenset(("-r", "--requirement"))
CONSTRAINT_OPTIONS = frozenset(("-c", "--constraint"))
FIND_LINKS_OPTIONS = frozenset(("-f", "--find-links"))
INDEX_URL_OPTIONS = frozenset(("-i", "--index-url"))
EDITABLE_OPTIONS = frozenset(("-e", "--editable"))
BOOLEAN_OPTIONS = frozenset(("--no-index", "--pre", "--require-hashes"))


def parse_requirements(
    filename: str,
    session: NetworkSession,
    provider: CandidateProvider | None = None,
    options: Any = None,
    constraint: bool = False,
) -> list[ParsedRequirement]:
    return parse_requirements_internal(
        filename,
        session,
        provider=provider,
        options=options,
        constraint=constraint,
        stack=[],
    )


def parse_requirements_internal(
    filename: str,
    session: NetworkSession,
    *,
    provider: CandidateProvider | None,
    options: Any,
    constraint: bool,
    stack: list[str],
) -> list[ParsedRequirement]:
    normalized = normalize_reference(filename, None)
    if normalized in stack:
        previous = stack[-1] if stack else normalized
        raise RequirementsFileParseError(
            f"{normalized} recursively references itself in {previous}"
        )
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme in REMOTE_SCHEMES:
        try:
            from cpip.network.utils import raise_for_status

            response = session.get(normalized)
            raise_for_status(response)
            content = response.text
        except InstallationError:
            raise
    else:
        path = Path(normalized)
        if not path.exists():
            if is_pylock_reference(normalized):
                raise InstallationError(
                    f"Error reading pylock file {normalized!r}: file does not exist"
                )
            kind = (
                "constraint file"
                if normalized.endswith(".txt")
                else "requirements file"
            )
            raise InstallationError(f"Could not open {kind}: {normalized}")
        data = path.read_bytes()
        content = None
        for bom, encoding in [
            (codecs.BOM_UTF32_BE, "utf-32-be"),
            (codecs.BOM_UTF32_LE, "utf-32-le"),
            (codecs.BOM_UTF8, "utf-8-sig"),
            (codecs.BOM_UTF16_BE, "utf-16-be"),
            (codecs.BOM_UTF16_LE, "utf-16-le"),
        ]:
            if bom and data.startswith(bom):
                content = data.decode(
                    "utf-16"
                    if encoding.startswith("utf-16")
                    else "utf-32"
                    if encoding.startswith("utf-32")
                    else encoding
                )
                break
        if content is None:
            cookie = None
            for line in data.splitlines()[:2]:
                match = CODING_RE.match(line)
                if match is not None:
                    cookie = match.group(1).decode("ascii", "replace")
                    break
            if cookie is not None:
                content = data.decode(cookie)
            else:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    getencoding = getattr(locale, "getencoding", None)
                    encoding = (
                        getencoding()
                        if callable(getencoding)
                        else locale.getpreferredencoding(False)
                    )
                    logger.warning(
                        "unable to decode data from %s with default encoding %s, "
                        "falling back to encoding from locale: %s. "
                        "If this is intentional you should specify the encoding with a "
                        "PEP-263 style comment, e.g. '# -*- coding: %s -*-'",
                        str(path),
                        "utf-8",
                        encoding,
                        encoding,
                    )
                    content = data.decode(encoding)
    if is_pylock_reference(normalized):
        print(
            "WARNING: Using pylock.toml as a requirements source is an experimental "
            "feature.",
            file=sys.stderr,
        )
        return parse_pylock(normalized, content, provider=provider)
    results: list[ParsedRequirement] = []
    next_stack = [*stack, normalized]
    processed: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if line.strip().startswith("#"):
            line = ""
        if pending is not None:
            if line:
                processed.append(pending)
                pending = None
            else:
                processed.append(pending)
                pending = None
                continue
        if line.endswith("\\"):
            pending = (line_number, line[:-1].rstrip())
            continue
        processed.append((line_number, line))
    if pending is not None:
        processed.append(pending)
    for line_number, line in processed:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if not line:
            continue
        parsed = parse_line(
            normalized,
            line_number,
            line,
            session=session,
            provider=provider,
            options=options,
            constraint=constraint,
            stack=next_stack,
        )
        results.extend(parsed)
    return results


def parse_line(
    filename: str,
    line_number: int,
    line: str,
    *,
    session: NetworkSession,
    provider: CandidateProvider | None,
    options: Any,
    constraint: bool,
    stack: list[str],
) -> list[ParsedRequirement]:
    if line.lstrip().startswith("-"):
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise RequirementsFileParseError(str(exc)) from exc
        results: list[ParsedRequirement] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("-"):
                results.extend(
                    parse_line(
                        filename,
                        line_number,
                        " ".join(tokens[index:]),
                        session=session,
                        provider=provider,
                        options=options,
                        constraint=constraint,
                        stack=stack,
                    )
                )
                break
            if "=" in token:
                option, value = token.split("=", 1)
            else:
                option = token
                if option in BOOLEAN_OPTIONS:
                    value = ""
                else:
                    index += 1
                    if index >= len(tokens):
                        raise RequirementsFileParseError(f"{option} requires a value")
                    value = tokens[index]
            if option in EDITABLE_OPTIONS and index + 1 < len(tokens):
                value = " ".join([value, *tokens[index + 1 :]])
                index = len(tokens) - 1
            if option in REQUIREMENTS_OPTIONS:
                nested = normalize_reference(value, filename, as_path=True)
                results.extend(
                    parse_requirements_internal(
                        nested,
                        session,
                        provider=provider,
                        options=options,
                        constraint=False,
                        stack=stack,
                    )
                )
            elif option in CONSTRAINT_OPTIONS:
                nested = normalize_reference(value, filename, as_path=True)
                results.extend(
                    parse_requirements_internal(
                        nested,
                        session,
                        provider=provider,
                        options=options,
                        constraint=True,
                        stack=stack,
                    )
                )
            elif option in FIND_LINKS_OPTIONS:
                if provider is not None:
                    normalized = normalize_reference(value, filename, as_path=True)
                    if os.path.exists(normalized):
                        provider.find_links.append(normalized)
                    else:
                        provider.find_links.append(value)
            elif option in INDEX_URL_OPTIONS:
                if provider is not None and not provider.no_index:
                    provider.index_urls[:] = [normalize_reference(value, filename)]
                auth = session.auth
                if auth is not None:
                    auth.index_urls = (
                        [] if provider is None else list(provider.index_urls)
                    )
            elif option == "--extra-index-url":
                if provider is not None and not provider.no_index:
                    provider.index_urls.append(normalize_reference(value, filename))
                auth = session.auth
                if auth is not None:
                    auth.index_urls = (
                        [] if provider is None else list(provider.index_urls)
                    )
            elif option == "--no-index":
                if provider is not None:
                    provider.no_index = True
                    provider.index_urls.clear()
                auth = session.auth
                if auth is not None:
                    auth.index_urls = []
            elif option == "--trusted-host":
                session.trusted_hosts.add(value.lower().split(":", 1)[0])
                logger.info(
                    "adding trusted host: %r (from line %d of %s)",
                    value,
                    line_number,
                    filename,
                )
            elif option == "--pre":
                if provider is not None and provider.release_control is not None:
                    provider.release_control.apply("all_releases", ":all:")
            elif option == "--require-hashes":
                if options is not None:
                    setattr(options, "require_hashes", True)
            elif option == "--all-releases":
                if provider is not None and provider.release_control is not None:
                    provider.release_control.apply("all_releases", value)
            elif option == "--only-final":
                if provider is not None and provider.release_control is not None:
                    provider.release_control.apply("only_final", value)
            elif option == "--only-binary":
                if provider is not None and provider.format_control is not None:
                    provider.format_control.apply("only-binary", value)
            elif option == "--no-binary":
                if provider is not None and provider.format_control is not None:
                    provider.format_control.apply("no-binary", value)
            elif option == "--use-feature":
                if value != "fast-deps":
                    raise RequirementsFileParseError(
                        f"invalid use-feature value {value!r}"
                    )
            elif option in EDITABLE_OPTIONS:
                results.extend(
                    parse_requirement_line(
                        filename,
                        line_number,
                        value,
                        constraint=constraint,
                        editable=True,
                    )
                )
            else:
                raise RequirementsFileParseError(
                    f"Unsupported requirement file option: {option}"
                )
            index += 1
        return results
    return parse_requirement_line(
        filename,
        line_number,
        line,
        constraint=constraint,
    )


def parse_requirement_line(
    filename: str,
    line_number: int,
    line: str,
    *,
    constraint: bool,
    editable: bool = False,
) -> list[ParsedRequirement]:
    if "=" in line and line.partition("=")[0].startswith("-"):
        option, value = line.split("=", 1)
    else:
        option, _, value = line.partition(" ")
    value = value.strip()
    requirement_line = value if option in EDITABLE_OPTIONS else line
    config_setting_options = ("--config-settings", "--config-setting")
    if (
        not any(option in requirement_line for option in config_setting_options)
        and "--hash" not in requirement_line
    ):
        requirement_text, parsed_options = requirement_line.strip(), {}
    else:
        try:
            tokens = shlex.split(requirement_line, posix=os.name != "nt")
        except ValueError as exc:
            raise RequirementsFileParseError(str(exc)) from exc
        requirement_tokens: list[str] = []
        config_settings: dict[str, object] = {}
        hash_options: dict[str, list[str]] = {}
        index = 0
        while index < len(tokens):
            token = strip_matching_quotes(tokens[index])
            if token in config_setting_options:
                if index + 1 >= len(tokens):
                    raise RequirementsFileParseError(f"{token} requires a value")
                index += 1
                merge_config_setting(
                    config_settings, strip_matching_quotes(tokens[index])
                )
            elif token.startswith(config_setting_options):
                merge_config_setting(config_settings, token.split("=", 1)[1])
            elif token == "--hash":
                if index + 1 >= len(tokens):
                    raise RequirementsFileParseError(requirement_line)
                index += 1
                add_hash_option(
                    hash_options, tokens[index], original_line=requirement_line
                )
            elif token.startswith("--hash="):
                add_hash_option(
                    hash_options, token.split("=", 1)[1], original_line=requirement_line
                )
            else:
                requirement_tokens.append(token)
            index += 1
        parsed_options: dict[str, object] = {}
        if config_settings:
            parsed_options["config_settings"] = config_settings
        if hash_options:
            parsed_options["hashes"] = hash_options
        requirement_text = " ".join(requirement_tokens)
    requirement_text = expand_env_variables(requirement_text)
    requirement_for_install = requirement_text
    file_reference = urllib.parse.urlparse(requirement_for_install)
    if file_reference.scheme == "file" and not file_reference.path.startswith("/"):
        requirement_for_install = normalize_reference(
            requirement_for_install, filename, as_path=True
        )
    from cpip.resolution.req_install import (
        install_req_from_editable,
        install_req_from_line,
    )

    try:
        if editable or option in EDITABLE_OPTIONS:
            install_req_from_editable(requirement_for_install)
        else:
            install_req_from_line(requirement_for_install)
    except ValueError as exc:
        raise InstallationError(f"Invalid requirement: {requirement_text!r}") from exc
    comes_from = f"{'-c' if constraint else '-r'} {filename} (line {line_number})"
    metadata: dict[str, object] = {}
    if parsed_options:
        metadata.update(parsed_options)
    return [
        ParsedRequirement(
            requirement=requirement_for_install,
            comes_from=comes_from,
            is_editable=editable or option in EDITABLE_OPTIONS,
            constraint=constraint,
            options=metadata or None,
            line_source=f"{filename} (line {line_number})",
        )
    ]
