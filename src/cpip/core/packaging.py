from __future__ import annotations

import os
import re
import sys
import urllib.parse
from functools import lru_cache

from cpip.core.names import NORMALIZE_RE, canonicalize_name

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any


VERSION_RE = re.compile(
    r"""

    ^\s*

    v?

    (?:(?P<epoch>\d+)!)?

    (?P<release>\d+(?:\.\d+)*)

    (?:

        [._-]?

        (?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)

        [._-]?

        (?P<pre_n>\d+)?

    )?

    (?:

        (?:-(?P<post_n1>\d+))

        |

        (?:[._-]?(?P<post_l>post|rev|r)[._-]?(?P<post_n2>\d+)?)

    )?

    (?:

        [._-]?dev[._-]?(?P<dev_n>\d+)?

    )?

    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?

    \s*$

    """,
    re.IGNORECASE | re.VERBOSE,
)

REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

SPEC_RE = re.compile(r"(===|==|!=|~=|<=|>=|<|>)\s*([^,]+)")

# frozenset(), unlike (), is not a singleton -- every call allocates. Most
# requirements have no extras, so this constant is reused instead of paying
# for a fresh empty frozenset on every one of them.
EMPTY_FROZENSET: frozenset[str] = frozenset()


def canonicalize_version(version: str) -> str:
    """Return the normalized public form of a PEP 440 version."""

    return str(Version(version))


def safe_extra(extra: str) -> str:
    return canonicalize_name(extra)


@lru_cache(maxsize=8)
def default_environment(extra: str | None = None) -> dict[str, str]:
    # Only reached when a requirement actually carries an environment
    # marker (see marker_applies' early exit), so keep it off the far more
    # common marker-free parse's import cost. Every field but "extra" is
    # fixed for the life of the process, so a cache miss only ever pays
    # the platform-module cost once per distinct `extra` value seen
    # (currently always None -- _marker_applies_cached reads "extra" from
    # its own extras set, not this dict's field).
    import platform

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


# String forms that failed comparison coercion -- see the comparison-method
# comment block in Version. Keyed on the str() result rather than the
# operand's type so that a type whose instances only sometimes stringify to
# a version is judged per instance, exactly as uncached parsing would.
# In practice this holds the resolver sentinels' "-inf"/"+inf"; the cap
# keeps operands with per-instance str() forms (e.g. bare object()s, whose
# text embeds their address) from growing it without bound.
_uncoercible_strings: set[str] = set()

_UNCOERCIBLE_STRINGS_CAP = 64


class Version:
    __slots__ = (
        "_hash",
        "_public",
        "comparison_key",
        "dev",
        "epoch",
        "local",
        "post",
        "pre",
        "release",
    )

    def __init__(self, value: str):
        raw = value.strip()

        if raw and raw.replace(".", "").isdecimal() and ".." not in raw:
            release = raw.split(".")

            self.epoch = 0

            self.release = tuple(map(int, release))

            self.pre = None

            self.post = None

            self.dev = None

            self.local = None

            self._public = None

            self.comparison_key = self.build_comparison_key()

            self._hash = hash(self.comparison_key)

            return

        match = VERSION_RE.match(raw)

        if match is None:
            raise InvalidVersion(value)

        self.epoch = int(match.group("epoch") or 0)

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

        post_n = match.group("post_n1") or match.group("post_n2")

        self.post = (
            int(post_n or 0)
            if match.group("post_l") is not None or match.group("post_n1") is not None
            else None
        )

        self.dev = (
            int(match.group("dev_n") or 0) if match.group("dev_n") is not None else None
        )

        self.local = (
            NORMALIZE_RE.sub(".", match.group("local").lower())
            if match.group("local") is not None
            else None
        )

        self._public = None

        self.comparison_key = self.build_comparison_key()

        self._hash = hash(self.comparison_key)

    @classmethod
    @lru_cache(maxsize=65536)
    def from_cache_state(cls, state: tuple[object, ...]) -> Version:
        """Restore a previously validated parsed version without reparsing it."""

        value = cls.__new__(cls)

        value.epoch = state[0]

        value.release = state[1]

        value.pre = state[2]

        value.post = state[3]

        value.dev = state[4]

        value.local = state[5]

        value._public = state[6]

        value.comparison_key = state[7]

        value._hash = hash(value.comparison_key)

        return value

    def cache_state_internal(self) -> tuple[object, ...]:
        return (
            self.epoch,
            self.release,
            self.pre,
            self.post,
            self.dev,
            self.local,
            self.public,
            self.comparison_key,
        )

    @property
    def public(self) -> str:
        public = self._public

        if public is None:
            public = self.format_public()

            self._public = public

        return public

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def base_version(self) -> str:
        """Return the release portion without pre/dev/post/local markers."""

        return ".".join(str(part) for part in self.release)

    def key_internal(
        self,
    ) -> Any:
        return self.comparison_key

    def build_comparison_key(
        self,
    ) -> Any:
        release = self.normalized_release()

        if self.pre is None and self.post is None and self.dev is None:
            suffix = (3, 0, 0, 0, 1, 0)

        elif self.pre is None and self.post is None and self.dev is not None:
            suffix = (-1, 0, 0, 0, 0, self.dev)

        else:
            pre_rank, pre_number = (
                (3, 0) if self.pre is None else (self.pre[0], self.pre[1])
            )

            post_rank = 0 if self.post is None else 1

            post_number = 0 if self.post is None else self.post

            dev_rank = 1 if self.dev is None else 0

            suffix = (
                pre_rank,
                pre_number,
                post_rank,
                post_number,
                dev_rank,
                self.dev or 0,
            )

        key: tuple[object, ...] = (self.epoch, release, suffix)

        if self.local is not None:
            local = tuple(
                (1, int(part)) if part.isdigit() else (0, part)
                for part in self.local.split(".")
            )

            key += (local,)

        return key

    def normalized_release(self) -> tuple[int, ...]:
        release = self.release

        while len(release) > 1 and release[-1] == 0:
            release = release[:-1]

        return release

    # Comparisons coerce strings directly and any other operand through
    # Version(str(other)) -- the latter is what lets this class compare
    # against another packaging library's Version (whose str() is a valid
    # version) rather than only its own instances. The wrinkle is the
    # resolver's range arithmetic, which compares Version bounds against
    # its infinity sentinels constantly: str(sentinel) is "-inf"/"+inf",
    # so the coercion used to pay a full VERSION_RE match attempt and a
    # raised-and-caught InvalidVersion per bound comparison, only to land
    # on the reflected dispatch anyway. _uncoercible_strings remembers the
    # string forms that failed so every later comparison against them is a
    # set lookup straight to NotImplemented -- and because whether a given
    # string parses is deterministic, caching by string is exactly
    # equivalent to reparsing it, for every operand type.

    def _coerced(self, other: object) -> Version | None:
        text = str(other)

        if text in _uncoercible_strings:
            return None

        try:
            coerced = Version(text)

        except InvalidVersion:
            if len(_uncoercible_strings) < _UNCOERCIBLE_STRINGS_CAP:
                _uncoercible_strings.add(text)

            return None

        return coerced

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            if isinstance(other, str):
                try:
                    other = Version(other)

                except InvalidVersion:
                    return NotImplemented

            else:
                other = self._coerced(other)

                if other is None:
                    return NotImplemented

        return self.comparison_key == other.comparison_key

    def __hash__(self) -> int:
        return self._hash

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            if isinstance(other, str):
                other = Version(other)

            else:
                other = self._coerced(other)

                if other is None:
                    return NotImplemented

        return self.comparison_key < other.comparison_key

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Version):
            if isinstance(other, str):
                other = Version(other)

            else:
                other = self._coerced(other)

                if other is None:
                    return NotImplemented

        return self.comparison_key <= other.comparison_key

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            if isinstance(other, str):
                other = Version(other)

            else:
                other = self._coerced(other)

                if other is None:
                    return NotImplemented

        return self.comparison_key > other.comparison_key

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Version):
            if isinstance(other, str):
                other = Version(other)

            else:
                other = self._coerced(other)

                if other is None:
                    return NotImplemented

        return self.comparison_key >= other.comparison_key

    def __str__(self) -> str:
        return self.public

    def __repr__(self) -> str:
        return f"<Version({self.public!r})>"

    def format_public(self) -> str:
        parts = []

        if self.epoch:
            parts.append(f"{self.epoch}!")

        parts.append(".".join(str(part) for part in self.release))

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


class Specifier:
    __slots__ = ("_compatible_upper_bound", "_parsed_version", "operator", "version")

    def __init__(self, operator: str, version: str) -> None:
        self.operator = operator

        self.version = version

        self._parsed_version: Version | None = None

        self._compatible_upper_bound: Version | None = None

        if self.operator == "===":
            return

        validated = Version(self.version.rstrip(".*"))

        if not self.version.endswith(".*"):
            self._parsed_version = validated

    @classmethod
    def from_cache_state(cls, state: tuple[object, ...]) -> Specifier:
        """Restore a previously validated specifier without reparsing its version."""

        value = cls.__new__(cls)

        value.operator = state[0]

        value.version = state[1]

        parsed_state = state[2]

        value._parsed_version = (
            None if parsed_state is None else Version.from_cache_state(parsed_state)
        )

        value._compatible_upper_bound = None

        return value

    def cache_state_internal(self) -> tuple[object, ...]:
        return (
            self.operator,
            self.version,
            None
            if self._parsed_version is None
            else self._parsed_version.cache_state_internal(),
        )

    @property
    def parsed_version(self) -> Version:
        if self._parsed_version is None:
            self._parsed_version = Version(self.version)

        return self._parsed_version

    @property
    def compatible_upper_bound(self) -> Version:
        if self._compatible_upper_bound is None:
            self._compatible_upper_bound = compatible_upper_bound_internal(
                self.parsed_version,
            )

        return self._compatible_upper_bound

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Specifier) and (
            self.operator,
            self.version,
        ) == (other.operator, other.version)

    def __hash__(self) -> int:
        return hash((self.operator, self.version))

    def contains(self, version: Version) -> bool:
        if self.operator == "===":
            return version.public == self.version

        if self.operator in {"==", "!="} and self.version.endswith(".*"):
            prefix = self.version[:-2]

            matches = version.public == prefix or version.public.startswith(
                prefix + ".",
            )

            return matches if self.operator == "==" else not matches

        other = self.parsed_version

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
            return version >= other and version < self.compatible_upper_bound

        raise ValueError(f"unknown specifier operator: {self.operator}")


def compatible_upper_bound_internal(version: Version) -> Version:
    release = list(version.release)

    if len(release) == 1:
        release[0] += 1

    else:
        release[-2] += 1

        release = release[:-1]

    return Version(".".join(str(part) for part in release))


_CONTAINS_CACHE_SIZE = 4096


class SpecifierSet:
    __slots__ = (
        "_bounds_cache",
        "_contains_cache",
        "_explicitly_allows_prereleases",
        "_text",
        "raw",
        "specifiers",
    )

    def __init__(self, value: str = ""):
        self.raw = value.strip()

        # A list comprehension, not a generator expression: this
        # constructor runs for every Requires-Dist line of every candidate
        # examined during resolution, and a genexpr pays a generator-frame
        # resumption per specifier where the comprehension runs in one.
        self.specifiers = tuple(
            [Specifier(op, ver.strip()) for op, ver in SPEC_RE.findall(self.raw)],
        )

        if self.raw and not self.specifiers:
            raise ValueError(f"invalid version specifier: {value!r}")

        self._text: str | None = None

        self._explicitly_allows_prereleases: bool | None = None

        self._contains_cache: dict[tuple[Version, bool], bool] = {}

        self._bounds_cache: (
            tuple[
                tuple[Version, bool] | None,
                tuple[Version, bool] | None,
            ]
            | None
        ) = None

    @classmethod
    def from_cache_state(cls, state: tuple[object, ...]) -> SpecifierSet:
        """Restore a previously validated set without rerunning its parser."""

        value = cls.__new__(cls)

        value.raw = state[0]

        value.specifiers = tuple(
            Specifier.from_cache_state(specifier_state)
            for specifier_state in state[1]  # ty:ignore[not-iterable]
        )

        value._text = state[2]

        value._explicitly_allows_prereleases = None

        value._contains_cache = {}

        value._bounds_cache = None

        return value

    def cache_state_internal(self) -> tuple[object, ...]:
        return (
            self.raw,
            tuple(specifier.cache_state_internal() for specifier in self.specifiers),
            self.text,
        )

    @property
    def text(self) -> str:
        text = self._text

        if text is None:
            text = ",".join(
                f"{specifier.operator}{specifier.version}"
                for specifier in self.specifiers
            )

            self._text = text

        return text

    def contains(
        self,
        version: Version | str,
        *,
        allow_prereleases: bool = False,
    ) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)

        if not self.specifiers and not parsed.is_prerelease:
            return True

        key = parsed, allow_prereleases

        cached = self._contains_cache.get(key)

        if cached is not None:
            return cached

        if parsed.is_prerelease and not allow_prereleases:
            explicitly_allowed = self._explicitly_allows_prereleases

            if explicitly_allowed is None:
                explicitly_allowed = any(
                    specifier.operator != "==="
                    and not specifier.version.endswith(".*")
                    and specifier.parsed_version.is_prerelease
                    for specifier in self.specifiers
                )

                self._explicitly_allows_prereleases = explicitly_allowed

            if not explicitly_allowed:
                self._remember_contains(key, False)

                return False

        result = all(spec.contains(parsed) for spec in self.specifiers)

        self._remember_contains(key, result)

        return result

    def _remember_contains(self, key: tuple[Version, bool], result: bool) -> None:
        # Bounded: instances are shared per specifier text for the life of
        # the process (see _interned_specifier_set), so a long-lived embedder
        # checking ever-new versions against ">=1" must not grow this
        # without limit. A sweep keeps the common case -- a few hundred
        # versions per specifier -- fully cached.
        cache = self._contains_cache

        if len(cache) >= _CONTAINS_CACHE_SIZE:
            cache.clear()

        cache[key] = result

    def bounds(
        self,
    ) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
        """Return conservative lower and upper bounds with inclusive flags."""

        cached = self._bounds_cache

        if cached is not None:
            return cached

        lower: tuple[Version, bool] | None = None

        upper: tuple[Version, bool] | None = None

        def tighten_lower(version: Version, inclusive: bool) -> None:
            nonlocal lower

            if lower is None or version > lower[0]:
                lower = (version, inclusive)

            elif version == lower[0]:
                lower = (version, lower[1] and inclusive)

        def tighten_upper(version: Version, inclusive: bool) -> None:
            nonlocal upper

            if upper is None or version < upper[0]:
                upper = (version, inclusive)

            elif version == upper[0]:
                upper = (version, upper[1] and inclusive)

        for specifier in self.specifiers:
            if specifier.operator == "==" and not specifier.version.endswith(".*"):
                tighten_lower(specifier.parsed_version, True)

                tighten_upper(specifier.parsed_version, True)

            elif specifier.operator == ">=":
                tighten_lower(specifier.parsed_version, True)

            elif specifier.operator == ">":
                tighten_lower(specifier.parsed_version, False)

            elif specifier.operator == "<=":
                tighten_upper(specifier.parsed_version, True)

            elif specifier.operator == "<":
                tighten_upper(specifier.parsed_version, False)

            elif specifier.operator == "~=":
                tighten_lower(specifier.parsed_version, True)

                tighten_upper(specifier.compatible_upper_bound, False)

        result = lower, upper

        self._bounds_cache = result

        return result

    def __bool__(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SpecifierSet):
            return NotImplemented

        return self.raw == other.raw


class Requirement:
    __slots__ = (
        "_canonical_name",
        "_is_unnamed_direct",
        "extras",
        "marker",
        "name",
        "raw",
        "specifier",
        "url",
    )

    def __init__(
        self,
        name: str,
        specifier: SpecifierSet,
        extras: frozenset[str],
        url: str | None = None,
        marker: str | None = None,
        raw: str = "",
    ) -> None:
        self.name = name

        self.specifier = specifier

        self.extras = extras

        self.url = url

        self.marker = marker

        self.raw = raw

        self._canonical_name: str | None = None

        self._is_unnamed_direct: bool | None = None

    @classmethod
    @lru_cache(maxsize=16384)
    def from_cache_state(cls, state: tuple[object, ...]) -> Requirement:
        """Restore a previously validated requirement without reparsing it."""

        return cls(
            name=state[0],  # ty:ignore[invalid-argument-type]
            specifier=SpecifierSet.from_cache_state(state[1]),  # ty:ignore[invalid-argument-type]
            extras=frozenset(state[2]),  # ty:ignore[invalid-argument-type]
            url=state[3],  # ty:ignore[invalid-argument-type]
            marker=state[4],  # ty:ignore[invalid-argument-type]
            raw=state[5],  # ty:ignore[invalid-argument-type]
        )

    def cache_state_internal(self) -> tuple[object, ...]:
        return (
            self.name,
            self.specifier.cache_state_internal(),
            tuple(sorted(self.extras)),
            self.url,
            self.marker,
            self.raw,
        )

    @property
    def canonical_name(self) -> str:
        if self._canonical_name is None:
            self._canonical_name = canonicalize_name(self.name)

        return self._canonical_name

    @property
    def is_unnamed_direct(self) -> bool:
        """Whether this requirement locates an artifact rather than naming one.

        A URL requirement or a bare local path has no metadata to trust until
        the artifact is fetched, so callers that would otherwise reject on
        name/version mismatch defer that check.  Every candidate link for a
        package is evaluated against the same requirement, so this is cached
        rather than recomputed per link.
        """

        cached = self._is_unnamed_direct

        if cached is None:
            cached = (
                self.url is not None
                or self.raw.startswith("file:")
                or self.raw.startswith((".", "/", "~"))
                or is_windows_path(self.raw)
            )

            self._is_unnamed_direct = cached

        return cached

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Requirement) and (
            self.name,
            self.specifier,
            self.extras,
            self.url,
            self.marker,
            self.raw,
        ) == (
            other.name,
            other.specifier,
            other.extras,
            other.url,
            other.marker,
            other.raw,
        )

    def copy_with(self, **changes: object) -> Requirement:
        values = {
            "name": self.name,
            "specifier": self.specifier,
            "extras": self.extras,
            "url": self.url,
            "marker": self.marker,
            "raw": self.raw,
        }

        values.update(changes)

        return type(self)(**values)

    def is_satisfied_by(
        self,
        version: str | Version,
        *,
        allow_prereleases: bool = True,
    ) -> bool:
        if not self.specifier.specifiers:
            parsed = version if isinstance(version, Version) else Version(version)

            return allow_prereleases or not parsed.is_prerelease

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


@lru_cache(maxsize=16384)
def parse_requirement(value: str) -> Requirement:
    raw = value.strip()

    if not raw:
        raise ValueError("empty requirement")

    if looks_like_direct_reference(raw):
        egg_name, egg_extras = egg_fragment_internal(raw)

        if egg_name is not None:
            inferred = project_from_direct_reference(raw)

            if inferred is not None and raw.split("#", 1)[0].lower().endswith(".whl"):
                name, version = inferred

                specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()

                return Requirement(
                    name=name,
                    extras=egg_extras,
                    specifier=specifier,
                    url=raw if looks_like_url(raw) else None,
                    marker=None,
                    raw=raw,
                )

            return Requirement(
                name=egg_name,
                extras=egg_extras,
                specifier=SpecifierSet(),
                url=raw if looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )

        inferred = project_from_direct_reference(raw)

        if inferred is not None:
            name, version = inferred

            specifier = SpecifierSet(f"=={version}") if version else SpecifierSet()

            return Requirement(
                name=name,
                extras=EMPTY_FROZENSET,
                specifier=specifier,
                url=raw if looks_like_url(raw) else None,
                marker=None,
                raw=raw,
            )

        # A direct URL to a source directory does not contain a wheel
        # filename or an ``#egg`` fragment from which to get the project
        # name.  Keep the URL as the locator, but use its final path
        # component as the resolver key.  Treating the complete URL as the
        # name makes dependencies such as ``lib_a @ file:///.../lib_a``
        # appear to require a project literally named ``file:///...``.
        name = raw
        if looks_like_url(raw):
            path = urllib.parse.unquote(urllib.parse.urlsplit(raw).path)
            name = path.rstrip("/").rsplit("/", 1)[-1] or raw

        return Requirement(
            name=name,
            extras=EMPTY_FROZENSET,
            specifier=SpecifierSet(),
            url=raw if looks_like_url(raw) else None,
            marker=None,
            raw=raw,
        )

    req_part, marker = split_marker(raw)

    name_match = REQ_NAME_RE.match(req_part)

    if name_match is None:
        raise ValueError(f"invalid requirement: {value!r}")

    name = name_match.group(1)

    rest = req_part[name_match.end() :].strip()

    extras: frozenset[str] = EMPTY_FROZENSET

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

        if spec and ("[" in spec or "]" in spec or SPEC_RE.sub("", spec).strip(" ,")):
            raise ValueError(f"invalid version specifier: {value!r}")

    return Requirement(
        name=name,
        extras=extras,
        specifier=_interned_specifier_set(spec),
        url=url,
        marker=marker,
        raw=raw,
    )


# parse_requirement is cached on the whole requirement string, so
# "leaf-0>=1.1.0" and "leaf-1>=1.1.0" each parsed their own
# SpecifierSet(">=1.1.0") -- a Specifier and a Version per clause -- although
# real metadata repeats the same handful of specifier texts across thousands
# of Requires-Dist lines. SpecifierSet's only mutable state is memo caches
# (text, contains, bounds, prerelease flag), so one instance per distinct
# text can back every requirement that uses it, and those memo caches are
# then shared as well. Bounded like the other parse caches: a sweep of
# unique texts clears it rather than growing without limit.
_SPECIFIER_SET_CACHE_SIZE = 4096
_specifier_sets: dict[str, SpecifierSet] = {}


def _interned_specifier_set(spec: str) -> SpecifierSet:
    specifier_set = _specifier_sets.get(spec)

    if specifier_set is None:
        specifier_set = SpecifierSet(spec)

        if len(_specifier_sets) >= _SPECIFIER_SET_CACHE_SIZE:
            _specifier_sets.clear()

        _specifier_sets[spec] = specifier_set

    return specifier_set


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


def looks_like_direct_reference(value: str) -> bool:
    return (
        looks_like_url(value)
        or value.startswith((".", "/", "~"))
        or is_windows_path(value)
    )


def looks_like_url(value: str) -> bool:
    if is_windows_path(value):
        return False

    if ":" not in value:
        return False

    parsed = urllib.parse.urlparse(value)

    return bool(parsed.scheme and (parsed.netloc or parsed.path))


def is_windows_path(value: str) -> bool:
    return (
        len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "/\\"
    )


def project_from_direct_reference(value: str) -> tuple[str, str | None] | None:
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


def egg_fragment_internal(value: str) -> tuple[str | None, frozenset[str]]:
    fragment = urllib.parse.urlparse(value).fragment

    if not fragment:
        return None, EMPTY_FROZENSET

    for key, raw_name in urllib.parse.parse_qsl(fragment, keep_blank_values=True):
        if key != "egg" or not raw_name:
            continue

        name = urllib.parse.unquote(raw_name)

        extras: frozenset[str] = EMPTY_FROZENSET

        if "[" in name and name.endswith("]"):
            name, extras_text = name[:-1].split("[", 1)

            extras = frozenset(
                safe_extra(part.strip())
                for part in extras_text.split(",")
                if part.strip()
            )

        if REQ_NAME_RE.fullmatch(name):
            return name, extras

    return None, EMPTY_FROZENSET


def split_marker(value: str) -> tuple[str, str | None]:
    # The character walk below exists only to skip a ";" inside a quoted
    # string. The overwhelming majority of requirement lines have no ";" at
    # all, and nearly all of the rest have no quote character before their
    # first ";" (quotes in the marker *after* it, e.g.
    # `pkg ; python_version >= "3.8"`, don't affect the split point) --
    # both answered by C-level scans instead of a per-character Python
    # loop.
    semicolon = value.find(";")

    if semicolon == -1:
        return value, None

    head = value[:semicolon]

    if "'" not in head and '"' not in head:
        return head.strip(), value[semicolon + 1 :].strip()

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

    normalized_extras = tuple(sorted(safe_extra(extra) for extra in extras if extra))

    return _marker_applies_cached(marker, normalized_extras)


@lru_cache(maxsize=4096)
def _marker_applies_cached(marker: str, extras: tuple[str, ...]) -> bool:
    env = default_environment()

    return marker_applies_internal(marker, env, set(extras))


def marker_applies_internal(
    marker: str,
    env: dict[str, str],
    extras: set[str],
) -> bool:
    or_clauses = re.split(r"\s+or\s+", marker, flags=re.IGNORECASE)

    return any(marker_and_clause_matches(clause, env, extras) for clause in or_clauses)


def marker_and_clause_matches(
    clause_text: str,
    env: dict[str, str],
    extras: set[str],
) -> bool:
    clauses = re.split(r"\s+and\s+", clause_text, flags=re.IGNORECASE)

    for clause in clauses:
        clause = strip_grouping_parentheses(clause.strip())

        match = re.match(
            r"\s*([A-Za-z0-9_]+)\s*(==|!=|<=|>=|<|>|in|not in)\s*(['\"])(.*?)\3\s*$",
            clause,
        )

        if match is None:
            return False

        key, op, _, expected = match.groups()

        if key == "extra":
            if not extra_marker_clause_matches(op, expected, extras):
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


def strip_grouping_parentheses(text: str) -> str:
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


def extra_marker_clause_matches(op: str, expected: str, extras: set[str]) -> bool:
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

    return any(compare_extra(extra, op, expected) for extra in extras)


def compare_extra(actual: str, op: str, expected: str) -> bool:
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
