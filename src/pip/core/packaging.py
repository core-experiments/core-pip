from __future__ import annotations

import os
import platform
import re
import sys
import urllib.parse
from dataclasses import dataclass
from functools import total_ordering
from typing import Iterable

_NORMALIZE_RE = re.compile(r"[-_.]+")
_VERSION_RE = re.compile(
    r"""
    ^\s*
    v?
    (?P<release>\d+(?:\.\d+)*)
    (?:
        (?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)
        (?P<pre_n>\d*)?
    )?
    (?:
        (?:\.post|post|rev|r)
        (?P<post_n>\d*)?
    )?
    (?:
        (?:\.dev|dev)
        (?P<dev_n>\d*)?
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SPEC_RE = re.compile(r"(===|==|!=|~=|<=|>=|<|>)\s*([^,]+)")


def canonicalize_name(name: str) -> str:
    return _NORMALIZE_RE.sub("-", name).lower()


def safe_extra(extra: str) -> str:
    return canonicalize_name(extra)


def default_environment(extra: str | None = None) -> dict[str, str]:
    impl = platform.python_implementation()
    version = platform.python_version()
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": impl,
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": version,
        "python_version": ".".join(version.split(".")[:2]),
        "sys_platform": sys.platform,
        "extra": extra or "",
    }


class InvalidVersion(ValueError):
    pass


@total_ordering
class Version:
    def __init__(self, value: str):
        raw = value.strip()
        match = _VERSION_RE.match(raw)
        if match is None:
            raise InvalidVersion(value)
        self.release = tuple(int(part) for part in match.group("release").split("."))
        pre_l = match.group("pre_l")
        pre_n = match.group("pre_n")
        if pre_l:
            label = pre_l.lower()
            if label in {"alpha"}:
                label = "a"
            elif label in {"beta"}:
                label = "b"
            elif label in {"c", "pre", "preview"}:
                label = "rc"
            self.pre = ({"a": 0, "b": 1, "rc": 2}[label], int(pre_n or 0))
        else:
            self.pre = None
        self.post = (
            int(match.group("post_n") or 0)
            if match.group("post_n") is not None
            else None
        )
        self.dev = (
            int(match.group("dev_n") or 0) if match.group("dev_n") is not None else None
        )
        self.local = (
            _NORMALIZE_RE.sub(".", match.group("local").lower())
            if match.group("local") is not None
            else None
        )
        self.public = self._format_public()

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def base_version(self) -> str:
        """Return the release portion without pre/dev/post/local markers."""
        return ".".join(str(part) for part in self.release)

    def _key(self) -> tuple[tuple[int, ...], int, tuple[int, int], int, int, str]:
        release = self._normalized_release()
        if self.dev is not None:
            dev_rank = 0
            dev = self.dev
        else:
            dev_rank = 1
            dev = 0
        if self.pre is None:
            pre_rank = 3
            pre = (0, 0)
        else:
            pre_rank = 1
            pre = self.pre
        post = -1 if self.post is None else self.post
        return (
            release,
            dev_rank,
            (pre_rank, pre[0] * 1000000 + pre[1]),
            post,
            dev,
            self.local or "",
        )

    def _normalized_release(self) -> tuple[int, ...]:
        release = self.release
        while len(release) > 1 and release[-1] == 0:
            release = release[:-1]
        return release

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            try:
                other = Version(str(other))
            except InvalidVersion:
                return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            other = Version(str(other))
        return self._key() < other._key()

    def __str__(self) -> str:
        return self.public

    def __repr__(self) -> str:
        return f"<Version({self.public!r})>"

    def _format_public(self) -> str:
        parts = [".".join(str(part) for part in self.release)]
        if self.pre is not None:
            label = {0: "a", 1: "b", 2: "rc"}[self.pre[0]]
            parts.append(f"{label}{self.pre[1]}")
        if self.post is not None:
            parts.append(f".post{self.post}")
        if self.dev is not None:
            parts.append(f".dev{self.dev}")
        if self.local is not None:
            parts.append(f"+{self.local}")
        return "".join(parts)


@dataclass(frozen=True)
class Specifier:
    operator: str
    version: str

    def contains(self, version: Version) -> bool:
        if self.operator == "===":
            return version.public == self.version
        if self.operator == "==" and self.version.endswith(".*"):
            prefix = self.version[:-2]
            return version.public == prefix or version.public.startswith(prefix + ".")
        other = Version(self.version)
        if self.operator == "==":
            return version == other
        if self.operator == "!=":
            return version != other
        if self.operator == ">=":
            return version >= other
        if self.operator == "<=":
            return version <= other
        if self.operator == ">":
            return version > other
        if self.operator == "<":
            return version < other
        if self.operator == "~=":
            return version >= other and version < _compatible_upper_bound(other)
        raise ValueError(f"unknown specifier operator: {self.operator}")


def _compatible_upper_bound(version: Version) -> Version:
    release = list(version.release)
    if len(release) == 1:
        release[0] += 1
    else:
        release[-2] += 1
        release = release[:-1]
    return Version(".".join(str(part) for part in release))


class SpecifierSet:
    def __init__(self, value: str = ""):
        self.raw = value.strip()
        self.specifiers = tuple(
            Specifier(op, ver.strip()) for op, ver in _SPEC_RE.findall(self.raw)
        )
        if self.raw and not self.specifiers:
            raise ValueError(f"invalid version specifier: {value!r}")
        for specifier in self.specifiers:
            if specifier.operator != "===":
                try:
                    Version(specifier.version.rstrip(".*"))
                except InvalidVersion as exc:
                    raise ValueError(f"invalid version specifier: {value!r}") from exc

    def contains(
        self, version: Version | str, *, allow_prereleases: bool = False
    ) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        if parsed.is_prerelease and not allow_prereleases:
            if not any(
                Version(spec.version).is_prerelease
                for spec in self.specifiers
                if spec.operator != "===" and not spec.version.endswith(".*")
            ):
                return False
        return all(spec.contains(parsed) for spec in self.specifiers)

    def __bool__(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        return ",".join(f"{spec.operator}{spec.version}" for spec in self.specifiers)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SpecifierSet):
            return NotImplemented
        return self.raw == other.raw


@dataclass(frozen=True)
class Requirement:
    name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    url: str | None = None
    marker: str | None = None
    raw: str = ""

    @property
    def canonical_name(self) -> str:
        return canonicalize_name(self.name)

    def is_satisfied_by(
        self, version: str | Version, *, allow_prereleases: bool = True
    ) -> bool:
        return self.specifier.contains(version, allow_prereleases=allow_prereleases)

    def __str__(self) -> str:
        parts = [self.name]
        if self.extras:
            parts.append("[" + ",".join(sorted(self.extras)) + "]")
        if self.url is not None:
            parts.append(" @ " + self.url)
        else:
            parts.append(str(self.specifier))
        if self.marker:
            parts.append("; " + self.marker.replace("'", '"'))
        return "".join(parts)


def parse_requirement(value: str) -> Requirement:
    raw = value.strip()
    if not raw:
        raise ValueError("empty requirement")
    if _looks_like_direct_reference(raw):
        egg_name, egg_extras = _egg_fragment(raw)
        if egg_name is not None:
            inferred = _project_from_direct_reference(raw)
            if inferred is not None and raw.split("#", 1)[0].lower().endswith(".whl"):
                name, version = inferred
                specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()
                return Requirement(
                    name=name,
                    extras=egg_extras,
                    specifier=specifier,
                    url=raw if _looks_like_url(raw) else None,
                    marker=None,
                    raw=raw,
                )
            return Requirement(
                name=egg_name,
                extras=egg_extras,
                specifier=SpecifierSet(),
                url=raw if _looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )
        inferred = _project_from_direct_reference(raw)
        if inferred is not None:
            name, version = inferred
            specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()
            return Requirement(
                name=name,
                extras=frozenset(),
                specifier=specifier,
                url=raw if _looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )
        return Requirement(
            name=raw,
            extras=frozenset(),
            specifier=SpecifierSet(),
            url=raw if _looks_like_url(raw) else None,
            marker=None,
            raw=raw,
        )
    req_part, marker = _split_marker(raw)
    name_match = _REQ_NAME_RE.match(req_part)
    if name_match is None:
        raise ValueError(f"invalid requirement: {value!r}")
    name = name_match.group(1)
    rest = req_part[name_match.end() :].strip()
    extras: frozenset[str] = frozenset()
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            raise ValueError(f"invalid extras in requirement: {value!r}")
        extras = frozenset(
            safe_extra(part.strip()) for part in rest[1:end].split(",") if part.strip()
        )
        rest = rest[end + 1 :].strip()
    url: str | None = None
    if rest.startswith("@"):
        url = rest[1:].strip()
        spec = ""
    else:
        if rest.startswith("(") and rest.endswith(")"):
            rest = rest[1:-1].strip()
        spec = rest
        if spec and ("[" in spec or "]" in spec or _SPEC_RE.sub("", spec).strip(" ,")):
            raise ValueError(f"invalid version specifier: {value!r}")
    return Requirement(
        name=name,
        extras=extras,
        specifier=SpecifierSet(spec),
        url=url,
        marker=marker,
        raw=raw,
    )


def canonicalize_requirement(value: str) -> str:
    """Return a stable textual form of a PEP 508 requirement."""
    requirement = parse_requirement(value.strip())
    parts = [canonicalize_name(requirement.name)]
    if requirement.extras:
        parts.append(",".join(sorted(canonicalize_name(e) for e in requirement.extras)))
        parts[-1] = f"[{parts[-1]}]"
    if requirement.url:
        parts.append(f" @ {requirement.url}")
    elif requirement.specifier:
        parts.append(str(requirement.specifier))
    if requirement.marker:
        parts.append(f"; {requirement.marker}")
    return "".join(parts)


def _looks_like_direct_reference(value: str) -> bool:
    return _looks_like_url(value) or value.startswith((".", "/", "~"))


def _looks_like_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return bool(parsed.scheme and (parsed.netloc or parsed.path))


def _project_from_direct_reference(value: str) -> tuple[str, str | None] | None:
    parsed = urllib.parse.urlparse(value)
    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    if not filename:
        return None
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        if len(parts) >= 5 and parts[0] and parts[1]:
            return parts[0].replace("_", "-"), parts[1]
        return None
    stem = filename
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tgz", ".zip"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        return None
    name, separator, version = stem.rpartition("-")
    if not separator or not name or not version:
        return None
    return name.replace("_", "-"), version


def _egg_fragment(value: str) -> tuple[str | None, frozenset[str]]:
    fragment = urllib.parse.urlparse(value).fragment
    if not fragment:
        return None, frozenset()
    for key, raw_name in urllib.parse.parse_qsl(fragment, keep_blank_values=True):
        if key != "egg" or not raw_name:
            continue
        name = urllib.parse.unquote(raw_name)
        extras: frozenset[str] = frozenset()
        if "[" in name and name.endswith("]"):
            name, extras_text = name[:-1].split("[", 1)
            extras = frozenset(
                safe_extra(part.strip())
                for part in extras_text.split(",")
                if part.strip()
            )
        if _REQ_NAME_RE.fullmatch(name):
            return name, extras
    return None, frozenset()


def _split_marker(value: str) -> tuple[str, str | None]:
    in_quote: str | None = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            in_quote = None if in_quote == char else char
        elif char == ";" and in_quote is None:
            return value[:index].strip(), value[index + 1 :].strip()
    return value, None


def marker_applies(marker: str | None, *, extras: Iterable[str] = ()) -> bool:
    if not marker:
        return True
    extra_values = {safe_extra(extra) for extra in extras if extra}
    env = default_environment()
    return _marker_applies(marker, env, extra_values)


def _marker_applies(
    marker: str,
    env: dict[str, str],
    extras: set[str],
) -> bool:
    or_clauses = re.split(r"\s+or\s+", marker, flags=re.IGNORECASE)
    return any(_marker_and_clause_matches(clause, env, extras) for clause in or_clauses)


def _marker_and_clause_matches(
    clause_text: str,
    env: dict[str, str],
    extras: set[str],
) -> bool:
    clauses = re.split(r"\s+and\s+", clause_text, flags=re.IGNORECASE)
    for clause in clauses:
        clause = _strip_grouping_parentheses(clause.strip())
        match = re.match(
            r"\s*([A-Za-z0-9_]+)\s*(==|!=|<=|>=|<|>|in|not in)\s*(['\"])(.*?)\3\s*$",
            clause,
        )
        if match is None:
            return False
        key, op, _, expected = match.groups()
        if key == "extra":
            if not _extra_marker_clause_matches(op, expected, extras):
                return False
            continue
        actual = env.get(key, "")
        if op == "==" and actual != expected:
            return False
        if op == "!=" and actual == expected:
            return False
        if op == "in" and actual not in {part.strip() for part in expected.split(",")}:
            return False
        if op == "not in" and actual in {part.strip() for part in expected.split(",")}:
            return False
        if op in {"<", "<=", ">", ">="}:
            try:
                actual_v = Version(actual)
                expected_v = Version(expected)
                actual_cmp: object = actual_v
                expected_cmp: object = expected_v
            except InvalidVersion:
                actual_cmp = actual
                expected_cmp = expected
            if op == "<" and not actual_cmp < expected_cmp:
                return False
            if op == "<=" and not actual_cmp <= expected_cmp:
                return False
            if op == ">" and not actual_cmp > expected_cmp:
                return False
            if op == ">=" and not actual_cmp >= expected_cmp:
                return False
    return True


def _strip_grouping_parentheses(text: str) -> str:
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        balanced = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _extra_marker_clause_matches(op: str, expected: str, extras: set[str]) -> bool:
    if not extras:
        extras = {""}
    expected = safe_extra(expected)
    if op == "==":
        return expected in extras
    if op == "!=":
        return expected not in extras
    if op == "in":
        expected_values = {safe_extra(part.strip()) for part in expected.split(",")}
        return any(extra in expected_values for extra in extras)
    if op == "not in":
        expected_values = {safe_extra(part.strip()) for part in expected.split(",")}
        return all(extra not in expected_values for extra in extras)
    return any(_compare_extra(extra, op, expected) for extra in extras)


def _compare_extra(actual: str, op: str, expected: str) -> bool:
    try:
        actual_v = Version(actual)
        expected_v = Version(expected)
        actual_cmp: object = actual_v
        expected_cmp: object = expected_v
    except InvalidVersion:
        actual_cmp = actual
        expected_cmp = expected
    if op == "<":
        return actual_cmp < expected_cmp
    if op == "<=":
        return actual_cmp <= expected_cmp
    if op == ">":
        return actual_cmp > expected_cmp
    if op == ">=":
        return actual_cmp >= expected_cmp
    raise ValueError(f"unsupported extra marker operator: {op}")
