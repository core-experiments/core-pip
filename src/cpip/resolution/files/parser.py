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

from cpip.core.errors import InstallationError
from cpip.core.packaging import parse_requirement
from cpip.index.prefetch import Prefetcher
from cpip.network.utils import raise_for_status
from cpip.resolution.files.models import (
    ParsedRequirement,
    RequirementsFileParseError,
)
from cpip.resolution.files.options import (
    add_hash_option,
    expand_env_variables,
    merge_config_setting,
    normalize_reference,
    strip_matching_quotes,
)
from cpip.resolution.files.pylock import is_pylock_reference, parse_pylock
from cpip.resolution.input_requirements import (
    install_req_from_editable,
    install_req_from_line,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from cpip.resolution.files.contracts import (
        RequirementSession,
        RequirementSource,
    )

logger = logging.getLogger(__name__)
CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
COMMENT_RE = re.compile(r"(^|\s+)#.*$")
REMOTE_SCHEMES = frozenset(("http", "https", "file"))
REQUIREMENTS_OPTIONS = frozenset(("-r", "--requirement"))
CONSTRAINT_OPTIONS = frozenset(("-c", "--constraint"))
INCLUDE_OPTION_PREFIXES = ("-r", "--requirement", "-c", "--constraint")
FIND_LINKS_OPTIONS = frozenset(("-f", "--find-links"))
INDEX_URL_OPTIONS = frozenset(("-i", "--index-url"))
EDITABLE_OPTIONS = frozenset(("-e", "--editable"))
BOOLEAN_OPTIONS = frozenset(("--no-index", "--pre", "--require-hashes"))
BOM_ENCODINGS = (
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
)


class RequirementFilePrefetcher:
    """Lazily schedule remote requirement-file reads."""

    def __init__(self, session: RequirementSession) -> None:
        self.session = session
        self.worker: Prefetcher[Any, str] | None = None

    def submit(self, url: str) -> None:
        if self.worker is None:
            self.worker = Prefetcher(self.session.get, max_workers=8)
        self.worker.submit(url, url)

    def take(self, url: str) -> Any:
        if self.worker is None:
            return None
        return self.worker.take(url)

    def close(self) -> None:
        if self.worker is not None:
            self.worker.close()


class _RequirementFileTask:
    __slots__ = ("constraint", "filename", "stack")

    def __init__(self, filename: str, constraint: bool, stack: list[str]) -> None:
        self.filename = filename
        self.constraint = constraint
        self.stack = stack


class _RequirementLineTask:
    __slots__ = ("constraint", "filename", "line", "line_number", "stack")

    def __init__(
        self,
        filename: str,
        line_number: int,
        line: str,
        constraint: bool,
        stack: list[str],
    ) -> None:
        self.filename = filename
        self.line_number = line_number
        self.line = line
        self.constraint = constraint
        self.stack = stack


def parse_requirements(
    filename: str,
    session: RequirementSession,
    provider: RequirementSource | None = None,
    options: Any = None,
    constraint: bool = False,
) -> list[ParsedRequirement]:
    prefetcher = RequirementFilePrefetcher(session)
    try:
        return parse_requirements_internal(
            filename,
            session,
            provider=provider,
            options=options,
            constraint=constraint,
            stack=[],
            prefetcher=prefetcher,
        )
    finally:
        prefetcher.close()


def parse_requirements_internal(
    filename: str,
    session: RequirementSession,
    *,
    provider: RequirementSource | None,
    options: Any,
    constraint: bool,
    stack: list[str],
    prefetcher: RequirementFilePrefetcher,
) -> list[ParsedRequirement]:
    pending: list[_RequirementFileTask | _RequirementLineTask] = [
        _RequirementFileTask(filename, constraint, stack),
    ]
    results: list[ParsedRequirement] = []
    content_cache: dict[str, str] = {}
    while pending:
        task = pending.pop()
        if isinstance(task, _RequirementLineTask):
            includes: list[tuple[str, bool]] = []
            results.extend(
                parse_line(
                    task.filename,
                    task.line_number,
                    task.line,
                    session=session,
                    provider=provider,
                    options=options,
                    constraint=task.constraint,
                    stack=task.stack,
                    prefetcher=prefetcher,
                    includes=includes,
                ),
            )
            pending.extend(
                _RequirementFileTask(nested, nested_constraint, task.stack)
                for nested, nested_constraint in reversed(includes)
            )
            continue

        normalized = normalize_reference(task.filename, None)
        if normalized in task.stack:
            previous = task.stack[-1] if task.stack else normalized
            raise RequirementsFileParseError(
                f"{normalized} recursively references itself in {previous}",
            )
        if normalized not in content_cache:
            content_cache[normalized] = _read_requirement_content(
                normalized,
                session,
                prefetcher,
            )
        content = content_cache[normalized]
        if is_pylock_reference(normalized):
            print(
                "WARNING: Using pylock.toml as a requirements source is an experimental feature.",
                file=sys.stderr,
            )
            results.extend(parse_pylock(normalized, content, provider=provider))
            continue

        next_stack = [*task.stack, normalized]
        processed: list[tuple[int, str]] = []
        continuation: tuple[int, str] | None = None
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.split(" #", 1)[0].rstrip() if " #" in raw_line else raw_line
            if line.strip().startswith("#"):
                line = ""
            if continuation is not None:
                processed.append(continuation)
                continuation = None
                if not line:
                    continue
            if line.endswith("\\"):
                continuation = (line_number, line[:-1].rstrip())
            else:
                processed.append((line_number, line))
        if continuation is not None:
            processed.append(continuation)
        prefetch_remote_includes(processed, normalized, prefetcher, task.stack)
        pending.extend(
            _RequirementLineTask(
                normalized,
                line_number,
                line,
                task.constraint,
                next_stack,
            )
            for line_number, line in reversed(processed)
            if line.strip() and not line.strip().startswith("#")
        )
    return results


def _read_requirement_content(
    normalized: str,
    session: RequirementSession,
    prefetcher: RequirementFilePrefetcher,
) -> str:
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme in REMOTE_SCHEMES:
        future = prefetcher.take(normalized)
        response = future.result() if future is not None else session.get(normalized)
        raise_for_status(response)
        return response.text

    try:
        with open(normalized, "rb") as file:
            data = file.read()
    except FileNotFoundError:
        if is_pylock_reference(normalized):
            raise InstallationError(
                f"Error reading pylock file {normalized!r}: file does not exist",
            )
        kind = "constraint file" if normalized.endswith(".txt") else "requirements file"
        raise InstallationError(f"Could not open {kind}: {normalized}")
    for bom, encoding in BOM_ENCODINGS:
        if bom and data.startswith(bom):
            return data.decode(
                "utf-16"
                if encoding.startswith("utf-16")
                else "utf-32"
                if encoding.startswith("utf-32")
                else encoding,
            )
    for line in data.splitlines()[:2]:
        match = CODING_RE.match(line)
        if match is not None:
            return data.decode(match.group(1).decode("ascii", "replace"))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        getencoding = getattr(locale, "getencoding", None)
        encoding = (
            getencoding()
            if callable(getencoding)
            else locale.getpreferredencoding(False)
        )
        logger.warning(
            "unable to decode data from %s with default encoding utf-8, "
            "falling back to locale encoding %s",
            normalized,
            encoding,
        )
        return data.decode(encoding)


def prefetch_remote_includes(
    processed: list[tuple[int, str]],
    filename: str,
    prefetcher: RequirementFilePrefetcher,
    stack: list[str],
) -> None:
    """Start direct remote includes before parsing their preceding lines."""
    for _, line in processed:
        if not line.lstrip().startswith(INCLUDE_OPTION_PREFIXES):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if "=" in token:
                option, value = token.split("=", 1)
            else:
                option = token
                if option not in REQUIREMENTS_OPTIONS | CONSTRAINT_OPTIONS:
                    index += 1
                    continue
                if index + 1 >= len(tokens):
                    break
                index += 1
                value = tokens[index]
            if option in REQUIREMENTS_OPTIONS | CONSTRAINT_OPTIONS:
                nested = normalize_reference(value, filename, as_path=True)
                if nested not in stack and urllib.parse.urlparse(nested).scheme in {
                    "http",
                    "https",
                }:
                    prefetcher.submit(nested)
            index += 1


def parse_line(
    filename: str,
    line_number: int,
    line: str,
    *,
    session: RequirementSession,
    provider: RequirementSource | None,
    options: Any,
    constraint: bool,
    stack: list[str],
    prefetcher: RequirementFilePrefetcher,
    includes: list[tuple[str, bool]],
) -> list[ParsedRequirement]:
    if line.lstrip().startswith("-"):
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            raise RequirementsFileParseError(str(exc)) from exc
        results: list[ParsedRequirement] = []
        remaining_line: str | None = None
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("-"):
                remaining_line = " ".join(tokens[index:])
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
                includes.append((nested, False))
            elif option in CONSTRAINT_OPTIONS:
                nested = normalize_reference(value, filename, as_path=True)
                includes.append((nested, True))
            elif option in FIND_LINKS_OPTIONS:
                if provider is not None:
                    normalized = normalize_reference(value, filename, as_path=True)
                    if not urllib.parse.urlsplit(normalized).scheme and os.path.isabs(
                        normalized,
                    ):
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
                    options.require_hashes = True
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
                        f"invalid use-feature value {value!r}",
                    )
            elif option in EDITABLE_OPTIONS:
                results.extend(
                    parse_requirement_line(
                        filename,
                        line_number,
                        value,
                        constraint=constraint,
                        editable=True,
                    ),
                )
            else:
                raise RequirementsFileParseError(
                    f"Unsupported requirement file option: {option}",
                )
            index += 1
        if remaining_line is not None:
            results.extend(
                parse_requirement_line(
                    filename,
                    line_number,
                    remaining_line,
                    constraint=constraint,
                ),
            )
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
                    config_settings,
                    strip_matching_quotes(tokens[index]),
                )
            elif token.startswith(config_setting_options):
                merge_config_setting(config_settings, token.split("=", 1)[1])
            elif token == "--hash":
                if index + 1 >= len(tokens):
                    raise RequirementsFileParseError(requirement_line)
                index += 1
                add_hash_option(
                    hash_options,
                    tokens[index],
                    original_line=requirement_line,
                )
            elif token.startswith("--hash="):
                add_hash_option(
                    hash_options,
                    token.split("=", 1)[1],
                    original_line=requirement_line,
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
    if requirement_for_install.startswith("file:"):
        file_reference = urllib.parse.urlparse(requirement_for_install)
        if not file_reference.path.startswith("/"):
            requirement_for_install = normalize_reference(
                requirement_for_install,
                filename,
                as_path=True,
            )

    try:
        if editable or option in EDITABLE_OPTIONS:
            install_req_from_editable(requirement_for_install)
        elif (
            requirement_for_install
            and requirement_for_install[0].isalnum()
            and "://" not in requirement_for_install
            and "/" not in requirement_for_install
            and "\\" not in requirement_for_install
            and not requirement_for_install.lower().endswith(".whl")
        ):
            parsed = parse_requirement(requirement_for_install)
            if parsed.marker:
                quote: str | None = None
                for char in parsed.marker:
                    if char in {"'", '"'}:
                        quote = None if quote == char else char
                    elif char == ";" and quote is None:
                        raise ValueError("multiple environment markers")
        else:
            install_req_from_line(requirement_for_install)
    except ValueError as exc:
        message = f"Invalid requirement: {requirement_text!r}"
        if "=" in requirement_text and "==" not in requirement_text:
            message += ". = is not a valid operator. Did you mean == ?"
        raise InstallationError(message) from exc
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
        ),
    ]
