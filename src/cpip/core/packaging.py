from __future__ import annotations

import os
import re
import sys
import urllib.parse

from cpip.core.caches import bounded_put, memoized, register_table
from cpip.core.names import canonicalize_name
from cpip.core.versions import FINAL_SUFFIX, InvalidVersion, Version

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any


REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

SPEC_RE = re.compile(r"(===|==|!=|~=|<=|>=|<|>)\s*([^,]+)")

# frozenset(), unlike (), is not a singleton -- every call allocates. Most
# requirements have no extras, so this constant is reused instead of paying
# for a fresh empty frozenset on every one of them.
EMPTY_FROZENSET: frozenset[str] = frozenset()


def safe_extra(extra: str) -> str:
    return canonicalize_name(extra)


@memoized(8)
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


class Specifier:
    """One clause of a version specifier, frozen at construction.

    ``parsed_version`` is the operand as a Version -- the prefix for a
    wildcard clause, ``None`` only for ``===`` whose operand is arbitrary
    text. ``contains`` implements PEP 440's per-operator rules, including
    the ones plain ordering gets wrong: ``==``/``!=`` ignore the candidate's
    local label unless the clause names one, ``<V`` excludes V's own
    prereleases and ``>V`` excludes V's post-releases and local versions,
    wildcards and ``~=`` match release segments rather than text.
    """

    __slots__ = ("is_wildcard", "operator", "parsed_version", "version")

    # Declared because __init__ writes them through object.__setattr__,
    # which no checker can follow back to an attribute.
    operator: str
    version: str
    parsed_version: Version | None
    is_wildcard: bool

    def __init__(self, operator: str, version: str) -> None:
        wildcard = version.endswith(".*")
        _write_operator(self, operator)
        _write_version(self, version)
        if operator == "===":
            _write_is_wildcard(self, False)
            _write_parsed_version(self, None)
            return
        if wildcard and operator not in ("==", "!="):
            raise InvalidVersion(version)
        _write_is_wildcard(self, wildcard)
        _write_parsed_version(self, Version(version[:-2] if wildcard else version))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Specifier is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Specifier is immutable (tried to delete {name!r})")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Specifier) and (
            self.operator,
            self.version,
        ) == (other.operator, other.version)

    def __hash__(self) -> int:
        return hash((self.operator, self.version))

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"

    def __repr__(self) -> str:
        return f"<Specifier({self.operator!r}, {self.version!r})>"

    def contains(self, version: Version) -> bool:
        operator = self.operator
        other = self.parsed_version

        if other is None:  # ===
            return version.public == self.version

        if operator == "==":
            if self.is_wildcard:
                return _prefix_matches(version, other)
            # A clause without a local label matches every local version
            # of its release; one with a label matches exactly.
            return version == other or (
                bool(version[3]) and not other[3] and version[:3] == other[:3]
            )

        if operator == "!=":
            if self.is_wildcard:
                return not _prefix_matches(version, other)
            return version != other and not (
                bool(version[3]) and not other[3] and version[:3] == other[:3]
            )

        if operator == ">=":
            return version >= other

        if operator == "<=":
            return version <= other

        if operator == ">":
            if not version > other:
                return False
            suffix = version[2]
            # Not a post-release of V itself (V with post/dev/local removed
            # equals the candidate's) unless V is a post-release, and not a
            # local version of V.
            if suffix[2] == 1 and other[2][2] == 0:
                pre_rank, pre_number = suffix[0], suffix[1]
                base_suffix = (
                    FINAL_SUFFIX
                    if pre_rank == 3
                    else (pre_rank, pre_number, 0, 0, 1, 0)
                )
                if other == (version[0], version[1], base_suffix, ()):
                    return False
            return not (version[3] and not other[3] and version[:3] == other[:3])

        if operator == "<":
            if not version < other:
                return False
            # Not a prerelease of V itself -- anything from V.dev0 up --
            # unless V is a prerelease.
            if version.is_prerelease and not other.is_prerelease:
                other_suffix = other[2]
                earliest_suffix = (
                    (-1, 0, 0, 0, 0, 0)
                    if other_suffix == FINAL_SUFFIX
                    else (3, 0, other_suffix[2], other_suffix[3], 0, 0)
                )
                if version >= (other[0], other[1], earliest_suffix, ()):
                    return False
            return True

        if operator == "~=":
            return version >= other and _prefix_matches(version, other, compatible=True)

        raise ValueError(f"unknown specifier operator: {operator}")


# Frozen classes write their slots through the slot descriptors: a direct
# descriptor call, unlike object.__setattr__, skips the attribute lookup.
_write_operator = Specifier.__dict__["operator"].__set__
_write_version = Specifier.__dict__["version"].__set__
_write_is_wildcard = Specifier.__dict__["is_wildcard"].__set__
_write_parsed_version = Specifier.__dict__["parsed_version"].__set__


def _prefix_matches(
    version: Version, prefix: Version, *, compatible: bool = False
) -> bool:
    """PEP 440 prefix matching: the candidate's epoch and leading release
    segments equal the prefix's, zero-padded, ignoring its local label.

    For ``~=`` the prefix is the operand's release minus its last segment --
    the ``==X.Y.*`` half of the compatible-release clause. A single-segment
    operand (which the reference grammar rejects) reads as ``==X.*``.
    """
    if version[0] != prefix[0]:
        return False
    release = prefix.release
    if compatible:
        release = release[:-1] or release
    elif prefix[2] != FINAL_SUFFIX and version[2] != prefix[2]:
        return False
    width = len(release)
    candidate = version.release
    if len(candidate) < width:
        candidate = candidate + (0,) * (width - len(candidate))
    return candidate[:width] == release


def compatible_upper_bound_internal(version: Version) -> Version:
    """The exclusive upper bound ``~=version`` implies, epoch included."""
    release = list(version.release)
    if len(release) == 1:
        release[0] += 1
    else:
        release[-2] += 1
        release = release[:-1]
    text = ".".join(str(part) for part in release)
    return Version(f"{version[0]}!{text}" if version[0] else text)


def _bounds_of(
    specifiers: tuple[Specifier, ...],
) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
    """Conservative lower and upper bounds with inclusive flags.

    Drops ``!=``, ``===`` and wildcards and reads ``~=`` as its half-open
    interval; every one of those is a widening, which is what the callers
    (interval intersection in the resolver, bisection over sorted catalog
    summaries) need.
    """
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None

    for specifier in specifiers:
        operator = specifier.operator
        parsed = specifier.parsed_version
        if parsed is None or specifier.is_wildcard:
            continue
        if operator in ("==", ">=", ">", "~="):
            inclusive = operator != ">"
            if lower is None or parsed > lower[0]:
                lower = (parsed, inclusive)
            elif parsed == lower[0]:
                lower = (parsed, lower[1] and inclusive)
        if operator in ("==", "<=", "<", "~="):
            bound = (
                compatible_upper_bound_internal(parsed) if operator == "~=" else parsed
            )
            inclusive = operator in ("==", "<=")
            if upper is None or bound < upper[0]:
                upper = (bound, inclusive)
            elif bound == upper[0]:
                upper = (bound, upper[1] and inclusive)

    return lower, upper


_CONTAINS_CACHE_SIZE = 4096

# One SpecifierSet per distinct text. Real metadata repeats the same handful
# of specifier texts across thousands of Requires-Dist lines, and a set's
# derived state and containment answers are then shared by every
# requirement that names it. Unsynchronized like every table in
# cpip.core.caches: two threads missing on one text build two equal sets
# and one wins the slot.
_SPECIFIER_SET_CACHE_SIZE = 4096
_specifier_sets: dict[str, SpecifierSet] = register_table({})


class SpecifierSet:
    """A comma-separated list of clauses, frozen, interned by text.

    ``SpecifierSet(text)`` returns the instance already built for that text
    (whitespace-stripped) while it is in the table; ``SpecifierSet()`` is
    the shared empty set. Everything derived from the clauses is computed
    once here:

    * ``text`` -- the canonical spelling (clauses joined by commas);
    * ``bounds`` -- conservative ``(lower, upper)`` with inclusive flags;
    * ``exact_version`` -- the one release a sole ``==`` clause names, or
      None (a wildcard, ``===``, a range or several clauses name no unique
      release);
    * ``is_pinned`` -- some ``==``/``===`` clause without a wildcard, so
      the set admits at most one release (the yank/hash policy question);
    * ``explicitly_allows_prereleases`` -- some clause other than ``!=``
      or ``===`` names a prerelease, which per PEP 440 opts the set in to
      prereleases without the caller asking.
    """

    __slots__ = (
        "_bounds",
        "_contains",
        "_contains_with_prereleases",
        "_exact_version",
        "_explicitly_allows_prereleases",
        "_is_pinned",
        "_text",
        "specifiers",
    )

    specifiers: tuple[Specifier, ...]
    # Everything else is computed on first read and stored once; a set that
    # is only ever asked "contains?" pays for nothing it does not use.
    _text: str
    _bounds: tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]
    _exact_version: Version | None
    _is_pinned: bool
    _explicitly_allows_prereleases: bool
    _contains: dict[Version, bool]
    _contains_with_prereleases: dict[Version, bool]

    def __new__(cls, value: str = "") -> SpecifierSet:
        key = value.strip()
        cached = _specifier_sets.get(key)
        if cached is not None:
            return cached

        # A list comprehension, not a generator expression: this runs for
        # every Requires-Dist line of every candidate examined during
        # resolution, and a genexpr pays a generator-frame resumption per
        # clause where the comprehension runs in one.
        specifiers = tuple(
            [Specifier(op, ver.strip()) for op, ver in SPEC_RE.findall(key)],
        )
        if key and not specifiers:
            raise ValueError(f"invalid version specifier: {value!r}")

        self = object.__new__(cls)
        _write_specifiers(self, specifiers)
        if len(_specifier_sets) >= _SPECIFIER_SET_CACHE_SIZE:
            _specifier_sets.clear()
        _specifier_sets[key] = self
        return self

    @property
    def text(self) -> str:
        """The canonical spelling: clauses joined by commas."""
        try:
            return self._text
        except AttributeError:
            text = ",".join([f"{s.operator}{s.version}" for s in self.specifiers])
            object.__setattr__(self, "_text", text)
            return text

    @property
    def bounds(self) -> tuple[tuple[Version, bool] | None, tuple[Version, bool] | None]:
        """Conservative ``(lower, upper)`` bounds with inclusive flags."""
        try:
            return self._bounds
        except AttributeError:
            bounds = _bounds_of(self.specifiers)
            object.__setattr__(self, "_bounds", bounds)
            return bounds

    @property
    def exact_version(self) -> Version | None:
        """The one release a sole ``==`` clause names, or None: a wildcard,
        ``===``, a range or several clauses name no unique release."""
        try:
            return self._exact_version
        except AttributeError:
            exact = None
            if len(self.specifiers) == 1:
                only = self.specifiers[0]
                if only.operator == "==" and not only.is_wildcard:
                    exact = only.parsed_version
            object.__setattr__(self, "_exact_version", exact)
            return exact

    @property
    def is_pinned(self) -> bool:
        """Some ``==``/``===`` clause without a wildcard: the set admits at
        most one release (the yank and hash policy question)."""
        try:
            return self._is_pinned
        except AttributeError:
            pinned = False
            for specifier in self.specifiers:
                if specifier.operator in ("==", "===") and not specifier.is_wildcard:
                    pinned = True
                    break
            object.__setattr__(self, "_is_pinned", pinned)
            return pinned

    @property
    def explicitly_allows_prereleases(self) -> bool:
        """Some clause other than ``!=``/``===`` names a prerelease, which
        per PEP 440 opts the set in to prereleases without being asked."""
        try:
            return self._explicitly_allows_prereleases
        except AttributeError:
            allowed = False
            for specifier in self.specifiers:
                parsed = specifier.parsed_version
                if (
                    parsed is not None
                    and specifier.operator != "!="
                    and parsed.is_prerelease
                ):
                    allowed = True
                    break
            object.__setattr__(self, "_explicitly_allows_prereleases", allowed)
            return allowed

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"SpecifierSet is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"SpecifierSet is immutable (tried to delete {name!r})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (SpecifierSet, (self.text,))

    def contains(
        self,
        version: Version | str,
        *,
        allow_prereleases: bool = False,
    ) -> bool:
        parsed = version if isinstance(version, Version) else Version(version)
        specifiers = self.specifiers

        if not specifiers and not parsed.is_prerelease:
            return True

        # Two caches keyed directly by Version rather than one keyed by
        # (Version, bool): the lookup is the whole cost of a warm call.
        # Bounded: instances live as long as the intern table keeps them,
        # so a long-lived embedder checking ever-new versions against
        # ">=1" must not grow this without limit.
        try:
            cache = (
                self._contains_with_prereleases if allow_prereleases else self._contains
            )
        except AttributeError:
            cache = {}
            object.__setattr__(
                self,
                "_contains_with_prereleases" if allow_prereleases else "_contains",
                cache,
            )
        cached = cache.get(parsed)
        if cached is not None:
            return cached

        if (
            parsed.is_prerelease
            and not allow_prereleases
            and not self.explicitly_allows_prereleases
        ):
            result = False
        else:
            result = True
            for specifier in specifiers:
                if not specifier.contains(parsed):
                    result = False
                    break

        bounded_put(cache, parsed, result, _CONTAINS_CACHE_SIZE)
        return result

    def __bool__(self) -> bool:
        return bool(self.specifiers)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"<SpecifierSet({self.text!r})>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SpecifierSet):
            return NotImplemented
        return self.specifiers == other.specifiers

    def __hash__(self) -> int:
        return hash(self.specifiers)


_write_specifiers = SpecifierSet.__dict__["specifiers"].__set__
_write_is_pinned = SpecifierSet.__dict__["is_pinned"].__set__
_write_exact_version = SpecifierSet.__dict__["exact_version"].__set__
_write_contains = SpecifierSet.__dict__["_contains"].__set__
_write_contains_with_prereleases = SpecifierSet.__dict__[
    "_contains_with_prereleases"
].__set__


class Requirement:
    """A parsed PEP 508 requirement, frozen.

    Identity is ``(canonical_name, specifier, extras, url, marker)``; ``raw``
    is provenance -- the text as read -- and two requirements that differ
    only in spelling or whitespace are equal.
    """

    __slots__ = (
        "_is_unnamed_direct",
        "canonical_name",
        "extras",
        "marker",
        "name",
        "raw",
        "specifier",
        "url",
    )

    name: str
    canonical_name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    url: str | None
    marker: str | None
    raw: str
    _is_unnamed_direct: bool

    def __init__(
        self,
        name: str,
        specifier: SpecifierSet,
        extras: frozenset[str],
        url: str | None = None,
        marker: str | None = None,
        raw: str = "",
    ) -> None:
        _write_name(self, name)
        _write_canonical_name(self, canonicalize_name(name))
        _write_specifier(self, specifier)
        _write_extras(self, extras)
        _write_url(self, url)
        _write_marker(self, marker)
        _write_raw(self, raw)

    @property
    def is_unnamed_direct(self) -> bool:
        """Whether this requirement locates an artifact rather than naming one.

        A URL requirement or a bare local path has no metadata to trust until
        the artifact is fetched, so callers that would otherwise reject on a
        name/version mismatch defer that check. Computed on first read.
        """
        try:
            return self._is_unnamed_direct
        except AttributeError:
            raw = self.raw
            result = (
                self.url is not None
                or raw.startswith(("file:", ".", "/", "~"))
                or is_windows_path(raw)
            )
            object.__setattr__(self, "_is_unnamed_direct", result)
            return result

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Requirement is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Requirement is immutable (tried to delete {name!r})")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Requirement) and (
            self.canonical_name,
            self.specifier,
            self.extras,
            self.url,
            self.marker,
        ) == (
            other.canonical_name,
            other.specifier,
            other.extras,
            other.url,
            other.marker,
        )

    def __hash__(self) -> int:
        return hash(
            (self.canonical_name, self.specifier, self.extras, self.url, self.marker)
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
        return type(self)(**values)  # type: ignore[arg-type]

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

    def __repr__(self) -> str:
        return f"<Requirement({str(self)!r})>"


_write_name = Requirement.__dict__["name"].__set__
_write_canonical_name = Requirement.__dict__["canonical_name"].__set__
_write_specifier = Requirement.__dict__["specifier"].__set__
_write_extras = Requirement.__dict__["extras"].__set__
_write_url = Requirement.__dict__["url"].__set__
_write_marker = Requirement.__dict__["marker"].__set__
_write_raw = Requirement.__dict__["raw"].__set__


@memoized(16384)
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


@memoized(4096)
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

        if op in {"<", "<=", ">", ">="} and not _compare_marker_values(
            actual, op, expected
        ):
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

    return any(_compare_marker_values(extra, op, expected) for extra in extras)


def _compare_marker_values(actual: str, op: str, expected: str) -> bool:
    """Order two marker operands as versions when both parse, else as text."""
    try:
        left: Any = Version(actual)
        right: Any = Version(expected)
    except InvalidVersion:
        left = actual
        right = expected
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(f"unsupported marker operator: {op}")
