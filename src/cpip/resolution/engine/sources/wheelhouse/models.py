"""Value objects for the local pure-wheel resolver."""

from __future__ import annotations

from functools import lru_cache

EMPTY_MARKER_CONTEXT = frozenset(("",))
EMPTY_EXTRAS = frozenset()


@lru_cache(maxsize=4096)
def canonicalize_name(value: str) -> str:
    return value.replace("_", "-").replace(".", "-").lower()


class LocalWheelVersion:
    __slots__ = ("_hash", "_normalized", "release", "text")

    def __init__(self, release: tuple[int, ...], text: str) -> None:
        self.release = release
        self.text = text
        normalized = release
        while len(normalized) > 1 and normalized[-1] == 0:
            normalized = normalized[:-1]
        self._normalized = normalized
        self._hash = hash(normalized)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, LocalWheelVersion):
            return NotImplemented
        return self._normalized < other._normalized

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        return not self <= other

    def __ge__(self, other: object) -> bool:
        return not self < other

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, LocalWheelVersion)
            and self._normalized == other._normalized
        )

    def __hash__(self) -> int:
        return self._hash

    def __str__(self) -> str:
        return self.text


class LocalWheelSpecifier:
    __slots__ = ("_compatible_upper", "_wildcard_prefix", "values")

    def __init__(self, values: tuple[tuple[str, LocalWheelVersion | str], ...]) -> None:
        self.values = values
        if (
            len(values) == 1
            and values[0][0] == "=="
            and isinstance(values[0][1], LocalWheelVersion)
        ):
            self._compatible_upper = ()
            self._wildcard_prefix = ()
            return
        compatible_upper: list[tuple[int, ...] | None] = []
        wildcard_prefix: list[str | None] = []
        for operator, expected in values:
            if operator == "~=" and isinstance(expected, LocalWheelVersion):
                upper = list(expected.release)
                if len(upper) == 1:
                    upper[0] += 1
                else:
                    upper[-2] += 1
                    upper = upper[:-1]
                compatible_upper.append(tuple(upper))
            else:
                compatible_upper.append(None)
            if operator in {"==", "!="} and isinstance(expected, str):
                wildcard_prefix.append(expected[:-2])
            else:
                wildcard_prefix.append(None)
        self._compatible_upper = tuple(compatible_upper)
        self._wildcard_prefix = tuple(wildcard_prefix)

    def contains(self, version: LocalWheelVersion) -> bool:
        if (
            len(self.values) == 1
            and self.values[0][0] == "=="
            and isinstance(self.values[0][1], LocalWheelVersion)
        ):
            return version._normalized == self.values[0][1]._normalized
        for index, (operator, expected) in enumerate(self.values):
            if isinstance(expected, str):
                prefix = self._wildcard_prefix[index]
                assert prefix is not None
                matches = version.text == prefix or version.text.startswith(
                    prefix + ".",
                )
                result = matches if operator == "==" else not matches
            elif operator == "===":
                result = version.text == expected.text
            elif operator == "==":
                result = version._normalized == expected._normalized
            elif operator == "!=":
                result = version._normalized != expected._normalized
            elif operator == ">=":
                result = version._normalized >= expected._normalized
            elif operator == "<=":
                result = version._normalized <= expected._normalized
            elif operator == ">":
                result = version._normalized > expected._normalized
            elif operator == "<":
                result = version._normalized < expected._normalized
            elif operator == "~=":
                upper = self._compatible_upper[index]
                assert upper is not None
                result = (
                    version._normalized >= expected._normalized
                    and version._normalized < upper
                )
            else:
                return False
            if not result:
                return False
        return True


class LocalWheelRequirement:
    __slots__ = (
        "_marker_expected",
        "_marker_value",
        "canonical_name",
        "extras",
        "marker",
        "name",
        "specifier",
    )

    def __init__(
        self,
        name: str,
        specifier: LocalWheelSpecifier,
        extras: frozenset[str],
        marker: tuple[str, str] | None = None,
        *,
        _normalized_extras: bool = False,
    ) -> None:
        self.name = name
        self.specifier = specifier
        self.extras = (
            extras
            if _normalized_extras
            else frozenset(item.replace("_", "-").lower() for item in extras)
            if extras
            else EMPTY_EXTRAS
        )
        self.marker = marker
        self.canonical_name = canonicalize_name(name)
        if marker is None:
            self._marker_value = ""
            self._marker_expected = EMPTY_EXTRAS
        else:
            operator, value = marker
            self._marker_value = value.replace("_", "-").lower()
            self._marker_expected = (
                frozenset(item.replace("_", "-").lower() for item in value.split(","))
                if operator in {"in", "not in"}
                else EMPTY_EXTRAS
            )

    def is_satisfied_by(self, version: LocalWheelVersion) -> bool:
        return self.specifier.contains(version)

    def marker_applies(self, context: frozenset[str] | None = None) -> bool:
        if not self.marker:
            return True
        operator, _ = self.marker
        values = self.extras if context is None else context
        if not values:
            values = EMPTY_MARKER_CONTEXT
        if operator == "==":
            return self._marker_value in values
        if operator == "!=":
            return self._marker_value not in values
        if operator == "in":
            return bool(values & self._marker_expected)
        return not values & self._marker_expected


class LocalWheelCandidate:
    __slots__ = (
        "canonical_name",
        "dependencies",
        "from_cache",
        "name",
        "path",
        "provided_extras",
        "requires_python",
        "source_hashes",
        "source_kind",
        "source_url",
        "source_vcs",
        "version",
        "yanked_reason",
    )

    def __init__(
        self,
        name: str,
        version: LocalWheelVersion,
        path: str,
        dependencies: tuple[LocalWheelRequirement, ...],
        provided_extras: frozenset[str] = frozenset(),
        requires_python: str | None = None,
        source_url: str | None = None,
        source_hashes: dict[str, str] | None = None,
        source_kind: str | None = "wheel",
        source_vcs: str | None = None,
        from_cache: bool = False,
        yanked_reason: str | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.path = path
        self.dependencies = dependencies
        self.provided_extras = provided_extras
        self.requires_python = requires_python
        self.source_url = source_url
        self.source_hashes = source_hashes
        self.source_kind = source_kind
        self.source_vcs = source_vcs
        self.from_cache = from_cache
        self.yanked_reason = yanked_reason
        self.canonical_name = canonicalize_name(name)


@lru_cache(maxsize=4096)
def dependencies_for_extras(
    candidate: LocalWheelCandidate,
    extras: frozenset[str],
) -> tuple[LocalWheelRequirement, ...]:
    """Return marker-free dependency requirements for one extras context."""
    if all(dependency.marker is None for dependency in candidate.dependencies):
        return candidate.dependencies
    return tuple(
        dependency
        if dependency.marker is None
        else LocalWheelRequirement(
            dependency.name,
            dependency.specifier,
            dependency.extras,
            None,
            _normalized_extras=True,
        )
        for dependency in candidate.dependencies
        if dependency.marker is None or dependency.marker_applies(extras)
    )
