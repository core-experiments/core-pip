from __future__ import annotations

import codecs
import locale
import logging
import os
import re
import shlex
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from packaging import pylock
from packaging.utils import parse_sdist_filename, parse_wheel_filename

from pip.core.errors import InstallationError
from pip.core.urls import path_to_url
from pip.index.provider import CandidateProvider
from pip.network.http import NetworkSession
from pip.resolution.req_install import install_req_from_editable, install_req_from_line

logger = logging.getLogger(__name__)
_CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
COMMENT_RE = re.compile(r"(^|\s+)#.*$")


class RequirementsFileParseError(InstallationError):
    """A requirements file could not be parsed."""


@dataclass(frozen=True)
class ParsedRequirement:
    requirement: str
    comes_from: str
    is_editable: bool = False
    constraint: bool = False
    options: dict[str, object] | None = None
    line_source: str | None = None
    locked_link: str | None = None
    locked_hashes: dict[str, list[str]] | None = None
    locked_direct: bool = False
    locked_name: str | None = None


def _is_pylock_reference(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    path = parsed.path or value
    return Path(path).name.startswith("pylock") and path.endswith(".toml")


def _pylock_location(reference: str, path: str | None) -> str:
    if path is None:
        raise InstallationError("pylock package is missing its path")
    parsed = urllib.parse.urlparse(reference)
    if parsed.scheme in {"http", "https"}:
        return urllib.parse.urljoin(reference, path)
    return path_to_url(str((Path(reference).parent / path).resolve()))


def _parse_pylock(
    reference: str,
    content: str,
    *,
    provider: CandidateProvider | None,
) -> list[ParsedRequirement]:
    try:
        lock = pylock.Pylock.from_dict(tomllib.loads(content))
    except Exception as exc:
        raise InstallationError(f"Invalid pylock file {reference!r}: {exc}") from exc
    try:
        selected = list(lock.select())
    except Exception as exc:
        raise InstallationError(
            f"Cannot select requirements from pylock file {reference!r}: {exc}"
        ) from exc
    results: list[ParsedRequirement] = []
    for package, distribution in selected:
        raw_hashes = getattr(distribution, "hashes", {})
        hashes = {name: [value] for name, value in raw_hashes.items()}
        link: str
        direct = False
        if isinstance(distribution, pylock.PackageDirectory):
            link = _pylock_location(reference, distribution.path)
            requirement = link
            direct = True
        elif isinstance(distribution, pylock.PackageArchive):
            link = _pylock_location(reference, distribution.path or distribution.url)
            requirement = f"{package.name} @ {link}"
            direct = True
        elif isinstance(distribution, pylock.PackageVcs):
            link = distribution.url or distribution.path or ""
            requirement = (
                f"{package.name} @ {distribution.type}+{link}@{distribution.commit_id}"
            )
            direct = True
        elif isinstance(distribution, pylock.PackageWheel):
            if (
                provider is not None
                and "binary"
                not in provider.format_control.get_allowed_formats(package.name)
            ):
                if package.sdist is None:
                    raise InstallationError(
                        f"binaries are not permitted for package {package.name!r} and "
                        f"there is no source distribution for it in {reference!r}"
                    )
                distribution = package.sdist
                link = _pylock_location(
                    reference, distribution.path or distribution.url
                )
                hashes = {name: [value] for name, value in distribution.hashes.items()}
            else:
                link = _pylock_location(
                    reference, distribution.path or distribution.url
                )
            _, version, _, _ = parse_wheel_filename(
                distribution.name or Path(link).name
            )
            requirement = f"{package.name}=={version}"
        else:
            if (
                provider is not None
                and "source"
                not in provider.format_control.get_allowed_formats(package.name)
            ):
                raise InstallationError(
                    f"source distributions are not permitted for package {package.name!r} and "
                    f"there is no compatible wheel for it in {reference!r}"
                )
            link = _pylock_location(reference, distribution.path or distribution.url)
            _, version = parse_sdist_filename(distribution.name or Path(link).name)
            requirement = f"{package.name}=={version}"
        results.append(
            ParsedRequirement(
                requirement=requirement,
                comes_from=reference,
                is_editable=isinstance(distribution, pylock.PackageDirectory)
                and bool(distribution.editable),
                options={"hashes": hashes} if hashes else None,
                locked_link=link,
                locked_hashes=hashes,
                locked_direct=direct,
                locked_name=package.name,
            )
        )
    return results


def parse_requirements(
    filename: str,
    session: NetworkSession,
    provider: CandidateProvider | None = None,
    options: Any = None,
    constraint: bool = False,
) -> list[ParsedRequirement]:
    return _parse_requirements(
        filename,
        session,
        provider=provider,
        options=options,
        constraint=constraint,
        stack=[],
    )


def _parse_requirements(
    filename: str,
    session: NetworkSession,
    *,
    provider: CandidateProvider | None,
    options: Any,
    constraint: bool,
    stack: list[str],
) -> list[ParsedRequirement]:
    normalized = _normalize_reference(filename, None)
    if normalized in stack:
        previous = stack[-1] if stack else normalized
        raise RequirementsFileParseError(
            f"{normalized} recursively references itself in {previous}"
        )
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme in {"http", "https", "file"}:
        try:
            from pip.network.utils import raise_for_status

            response = session.get(normalized)
            raise_for_status(response)
            content = response.text
        except InstallationError:
            raise
    else:
        path = Path(normalized)
        if not path.exists():
            if _is_pylock_reference(normalized):
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
                match = _CODING_RE.match(line)
                if match is not None:
                    cookie = match.group(1).decode("ascii", "replace")
                    break
            if cookie is not None:
                content = data.decode(cookie)
            else:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    encoding = locale.getencoding()
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
    if _is_pylock_reference(normalized):
        print(
            "WARNING: Using pylock.toml as a requirements source is an experimental "
            "feature.",
            file=sys.stderr,
        )
        return _parse_pylock(normalized, content, provider=provider)
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
        parsed = _parse_line(
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


def _parse_line(
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
                    _parse_line(
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
                if option in {"--no-index", "--pre", "--require-hashes"}:
                    value = ""
                else:
                    index += 1
                    if index >= len(tokens):
                        raise RequirementsFileParseError(f"{option} requires a value")
                    value = tokens[index]
            if option in {"-e", "--editable"} and index + 1 < len(tokens):
                value = " ".join([value, *tokens[index + 1 :]])
                index = len(tokens) - 1
            if option in {"-r", "--requirement"}:
                nested = _normalize_reference(value, filename, as_path=True)
                results.extend(
                    _parse_requirements(
                        nested,
                        session,
                        provider=provider,
                        options=options,
                        constraint=False,
                        stack=stack,
                    )
                )
            elif option in {"-c", "--constraint"}:
                nested = _normalize_reference(value, filename, as_path=True)
                results.extend(
                    _parse_requirements(
                        nested,
                        session,
                        provider=provider,
                        options=options,
                        constraint=True,
                        stack=stack,
                    )
                )
            elif option in {"-f", "--find-links"}:
                if provider is not None:
                    normalized = _normalize_reference(value, filename, as_path=True)
                    if os.path.exists(normalized):
                        provider.find_links.append(normalized)
                    else:
                        provider.find_links.append(value)
            elif option in {"-i", "--index-url"}:
                if provider is not None and not provider.no_index:
                    provider.index_urls[:] = [_normalize_reference(value, filename)]
                auth = session.auth
                if auth is not None:
                    auth.index_urls = (
                        [] if provider is None else list(provider.index_urls)
                    )
            elif option == "--extra-index-url":
                if provider is not None and not provider.no_index:
                    provider.index_urls.append(_normalize_reference(value, filename))
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
                session.adapters[f"https://{value}/"] = session._trusted_host_adapter
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
            elif option in {"-e", "--editable"}:
                results.extend(
                    _parse_requirement_line(
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
    return _parse_requirement_line(
        filename,
        line_number,
        line,
        constraint=constraint,
    )


def _parse_requirement_line(
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
    requirement_line = value if option in {"-e", "--editable"} else line
    config_setting_options = ("--config-settings", "--config-setting")
    if (
        not any(option in requirement_line for option in config_setting_options)
        and "--hash" not in requirement_line
    ):
        requirement_text, parsed_options = requirement_line.strip(), {}
    else:
        try:
            tokens = shlex.split(requirement_line)
        except ValueError as exc:
            raise RequirementsFileParseError(str(exc)) from exc
        requirement_tokens: list[str] = []
        config_settings: dict[str, object] = {}
        hash_options: dict[str, list[str]] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in config_setting_options:
                if index + 1 >= len(tokens):
                    raise RequirementsFileParseError(f"{token} requires a value")
                index += 1
                _merge_config_setting(config_settings, tokens[index])
            elif token.startswith(config_setting_options):
                _merge_config_setting(config_settings, token.split("=", 1)[1])
            elif token == "--hash":
                if index + 1 >= len(tokens):
                    raise RequirementsFileParseError(requirement_line)
                index += 1
                _add_hash_option(
                    hash_options, tokens[index], original_line=requirement_line
                )
            elif token.startswith("--hash="):
                _add_hash_option(
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
    requirement_text = _expand_env_variables(requirement_text)
    try:
        if editable or option in {"-e", "--editable"}:
            install_req_from_editable(value)
        else:
            install_req_from_line(requirement_text)
    except ValueError as exc:
        raise InstallationError(f"Invalid requirement: {requirement_text!r}") from exc
    comes_from = f"{'-c' if constraint else '-r'} {filename} (line {line_number})"
    metadata: dict[str, object] = {}
    if parsed_options:
        metadata.update(parsed_options)
    return [
        ParsedRequirement(
            requirement=requirement_text
            if option not in {"-e", "--editable"}
            else value,
            comes_from=comes_from,
            is_editable=editable or option in {"-e", "--editable"},
            constraint=constraint,
            options=metadata or None,
            line_source=f"{filename} (line {line_number})",
        )
    ]


def _add_hash_option(
    target: dict[str, list[str]], raw: str, *, original_line: str
) -> None:
    name, sep, digest = raw.partition(":")
    if not sep or not digest:
        raise RequirementsFileParseError(original_line)
    target.setdefault(name, []).append(digest)


def _merge_config_setting(target: dict[str, object], raw: str) -> None:
    key, _, value = raw.partition("=")
    key = key.strip()
    existing = target.get(key)
    if existing is None:
        target[key] = value if _ else ""
    elif isinstance(existing, list):
        values = cast(list[str], existing)
        values.append(value if _ else "")
    else:
        target[key] = [existing, value if _ else ""]


def _normalize_reference(value: str, base: str | None, *, as_path: bool = False) -> str:
    value = _expand_env_variables(value.strip())
    parsed = urllib.parse.urlparse(value)
    base_parsed = urllib.parse.urlparse(base) if base else None
    base_directory = base.rsplit("/", 1)[0] + "/" if base else None
    if parsed.scheme:
        if base_parsed is not None and base_parsed.scheme:
            return urllib.parse.urljoin(base_directory, value)
        return value
    if (
        not as_path
        and not any(sep in value for sep in ("/", os.sep))
        and not value.startswith(".")
    ):
        return value
    path = Path(value).expanduser()
    if base and not path.is_absolute():
        if base_parsed is not None and base_parsed.scheme:
            return urllib.parse.urljoin(base_directory, value)
        path = Path(base).resolve().parent / path
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _expand_env_variables(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = os.getenv(name)
        return match.group(0) if replacement in {None, ""} else str(replacement)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
