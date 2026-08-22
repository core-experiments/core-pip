"""PEP 440 versions as their own ordering key.

A :class:`Version` *is* a tuple -- ``(epoch, release, suffix, local)`` --
laid out so that tuple comparison is PEP 440 ordering. Sorting, ``max``,
dict keys, bisection and the resolver's interval arithmetic all compare
Versions in C with no Python-level dunder in the way, and there is no
separate "comparison key" to keep in step with the object.

The rules that follow from that:

* A Version compares only with a Version. ``version == "1.0"`` is
  ``False`` and ``version < "1.0"`` raises ``TypeError``; parse the text
  first. (Equality with a plain tuple of the same shape holds, as for any
  tuple subclass -- do not mix the two as keys of one dict.)
* Format with f-strings or ``str()``; ``"%s" % version`` would treat the
  tuple as the argument list.
* ``marshal`` rejects the subclass, so a Version that leaks into an
  on-disk payload fails closed; :meth:`to_wire` produces the plain-tuple
  record the catalog summaries store and :meth:`from_wire` reads it.
* Instances are immutable and interned: ``Version(text)`` returns the
  instance already built for that text while it is in the table, so equal
  texts normally share one object, and the table is bounded and swept.
"""

from __future__ import annotations

import re

from cpip.core.caches import register_table
from cpip.core.names import NORMALIZE_RE

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


class InvalidVersion(ValueError):
    pass


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
        [._-]?(?P<dev_l>dev)[._-]?(?P<dev_n>\d+)?
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PRE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "pre": 2,
    "preview": 2,
    "rc": 2,
}
_PRE_LABEL = ("a", "b", "rc")

# The suffix element of the key. Six integers ordered so that
# dev < pre < final < post and a dev of any of those sorts before it:
#   (pre_rank, pre_number, post_rank, post_number, dev_rank, dev_number)
# with pre_rank 3 meaning "no prerelease", dev_rank 1 meaning "no dev", and
# a bare dev release (no pre, no post) taking pre_rank -1 so that it sorts
# before every prerelease of the same release.
FINAL_SUFFIX = (3, 0, 0, 0, 1, 0)
_NO_LOCAL: tuple[()] = ()

_VERSIONS_LIMIT = 65536
_versions: dict[str, Version] = register_table({})


class Version(tuple):
    """A parsed PEP 440 version; see the module docstring for the rules."""

    # No __slots__: a tuple subclass cannot add slots, so the display fields
    # live in the instance dict -- ``release`` written once in __new__,
    # ``public`` on first use (most versions a resolve builds are compared
    # and discarded, never printed).
    release: tuple[int, ...]

    def __new__(cls, value: str) -> Version:
        cached = _versions.get(value)
        if cached is not None:
            return cached

        raw = value.strip()
        if (
            raw
            and raw.replace(".", "").isdecimal()
            and ".." not in raw
            and raw[0] != "."
            and raw[-1] != "."
        ):
            epoch = 0
            release = tuple(map(int, raw.split(".")))
            suffix = FINAL_SUFFIX
            local: Any = _NO_LOCAL
        else:
            match = VERSION_RE.match(raw)
            if match is None:
                raise InvalidVersion(value)
            (
                epoch_text,
                release_text,
                pre_label,
                pre_number,
                post_number_1,
                post_label,
                post_number_2,
                dev_label,
                dev_number,
                local_text,
            ) = match.groups()
            epoch = int(epoch_text) if epoch_text else 0
            release = tuple(map(int, release_text.split(".")))
            pre = (
                (_PRE_RANK[pre_label.lower()], int(pre_number or 0))
                if pre_label
                else None
            )
            if post_number_1 is not None:
                post: int | None = int(post_number_1)
            elif post_label is not None:
                post = int(post_number_2 or 0)
            else:
                post = None
            dev = int(dev_number or 0) if dev_label is not None else None

            if pre is None and post is None and dev is None:
                suffix = FINAL_SUFFIX
            elif pre is None and post is None:
                suffix = (-1, 0, 0, 0, 0, dev)
            else:
                suffix = (
                    3 if pre is None else pre[0],
                    0 if pre is None else pre[1],
                    0 if post is None else 1,
                    0 if post is None else post,
                    1 if dev is None else 0,
                    dev or 0,
                )

            if local_text is not None:
                local = tuple(
                    (1, int(part)) if part.isdigit() else (0, part)
                    for part in NORMALIZE_RE.sub(".", local_text.lower()).split(".")
                )
            else:
                local = _NO_LOCAL

        normalized = release
        while len(normalized) > 1 and normalized[-1] == 0:
            normalized = normalized[:-1]

        self = tuple.__new__(cls, (epoch, normalized, suffix, local))
        self.__dict__["release"] = release
        if len(_versions) >= _VERSIONS_LIMIT:
            _versions.clear()
        _versions[value] = self
        return self

    # --- immutability -------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Version is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Version is immutable (tried to delete {name!r})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (Version, (self.public,))

    def __copy__(self) -> Version:
        return self

    def __deepcopy__(self, memo: object) -> Version:
        return self

    # --- display ------------------------------------------------------

    @property
    def public(self) -> str:
        """The canonical PEP 440 spelling."""
        fields = self.__dict__
        public = fields.get("public")
        if public is None:
            public = fields["public"] = self._format_public()
        return public

    def _format_public(self) -> str:
        epoch, _release, suffix, local = self
        parts = [f"{epoch}!" if epoch else "", ".".join(map(str, self.release))]
        if suffix != FINAL_SUFFIX:
            pre_rank, pre_number, post_rank, post_number, dev_rank, dev_number = suffix
            if 0 <= pre_rank < 3:
                parts.append(f"{_PRE_LABEL[pre_rank]}{pre_number}")
            if post_rank:
                parts.append(f".post{post_number}")
            if dev_rank == 0:
                parts.append(f".dev{dev_number}")
        if local:
            parts.append("+" + ".".join(str(part[1]) for part in local))
        return "".join(parts)

    def __str__(self) -> str:
        return self.public

    def __repr__(self) -> str:
        return f"<Version({self.public!r})>"

    # --- derived, all cold ---------------------------------------------

    @property
    def epoch(self) -> int:
        return self[0]

    @property
    def is_prerelease(self) -> bool:
        suffix = self[2]
        return suffix[0] != 3 or suffix[4] == 0

    @property
    def local(self) -> str | None:
        local = self[3]
        if not local:
            return None
        return ".".join(str(part[1]) for part in local)

    @property
    def base_version(self) -> str:
        """Epoch and release only, without pre/post/dev/local markers."""
        release = ".".join(map(str, self.release))
        return f"{self[0]}!{release}" if self[0] else release

    # --- wire record --------------------------------------------------

    def to_wire(self) -> tuple[str, tuple[int, ...], tuple[Any, ...]]:
        """The record cached catalog summaries store: ``(public, release, key)``.

        Plain tuples only (``marshal`` rejects the subclass). The key is kept
        on disk so a sorted summary can be bisected without rebuilding its
        Versions; the text is the source of truth when one is rebuilt.
        """
        return (self.public, self.release, tuple(self))

    @classmethod
    def from_wire(cls, state: Any) -> Version:
        """The Version for a :meth:`to_wire` record, through the intern table."""
        return cls(state[0])


_INT_ONLY = {int}


def is_version_wire(value: object) -> bool:
    """Whether ``value`` has the exact shape of a :meth:`Version.to_wire` record."""
    if not isinstance(value, tuple) or len(value) != 3:
        return False
    public, release, key = value
    if not isinstance(public, str) or not isinstance(release, tuple) or not release:
        return False
    if set(map(type, release)) != _INT_ONLY:
        return False
    if not isinstance(key, tuple) or len(key) != 4:
        return False
    epoch, normalized, suffix, local = key
    if type(epoch) is not int or not isinstance(normalized, tuple) or not normalized:
        return False
    stripped = release
    while len(stripped) > 1 and stripped[-1] == 0:
        stripped = stripped[:-1]
    if normalized != stripped:
        return False
    if (
        not isinstance(suffix, tuple)
        or len(suffix) != 6
        or set(map(type, suffix)) != _INT_ONLY
    ):
        return False
    return isinstance(local, tuple)


def version_of(value: Version | str) -> Version | None:
    """The Version for an attribute that may still be text.

    Installed-distribution records carry the version as the text read from
    METADATA or RECORD; comparing that text with a Version must parse it
    first (a Version never compares equal to text). None when the text is
    not a PEP 440 version, which a caller treats as "not the same version".
    """
    if isinstance(value, Version):
        return value
    try:
        return Version(value)
    except InvalidVersion:
        return None


ZERO_VERSION = Version("0")
"""The one "no declared version" sentinel (unknown direct sources, the resolver root)."""
