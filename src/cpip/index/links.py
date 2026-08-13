from __future__ import annotations

import datetime
import functools
import os
import posixpath
import re
import stat
import urllib.parse

from cpip.core.errors import DiagnosticCpipError
from cpip.core.hashes import Hashes
from cpip.core.urls import (
    path_to_url,
    redact_auth_from_url,
    split_auth_from_netloc,
    url_to_path,
)
from cpip.index.hashes import SUPPORTED_HASHES
from cpip.index.paths import PathComponent
from cpip.index.source_models import ArtifactKind, MetadataFile

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Mapping

VCS_SCHEMES_internal = tuple(f"{scheme}+" for scheme in ("git", "hg", "svn", "bzr"))
VCS_SCHEMES = frozenset(("git", "hg", "svn", "bzr"))
SOURCE_ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.lzma",
    ".tgz",
    ".zip",
)
WHEEL_EXTENSION = ".whl"
SUPPORTED_EXTENSIONS = (WHEEL_EXTENSION, *SOURCE_ARCHIVE_SUFFIXES)

# Python 3.11+ wraps urlsplit in an lru_cache, which is pure overhead here:
# every Link is built from a distinct URL, so the cache almost never hits and
# every call still pays to hash the url and probe the cache dict. functools
# always exposes the pre-decoration function as __wrapped__ regardless of
# Python version, so this skips the cache without depending on any
# version-specific internal -- on 3.9/3.10, where urlsplit isn't cached at
# all, __wrapped__ doesn't exist and this is exactly urlsplit itself.
_urlsplit = getattr(urllib.parse.urlsplit, "__wrapped__", urllib.parse.urlsplit)
REQ_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EGG_FRAGMENT_RE = re.compile(r"[#&]egg=([^&]*)")
HASH_URL_FRAGMENT_RE = re.compile(
    r"[#&]({choices})=([^&]*)".format(
        choices="|".join(re.escape(name) for name in SUPPORTED_HASHES),
    ),
)


def hash_from_url_fragment(url: str) -> tuple[str, str] | None:
    match = HASH_URL_FRAGMENT_RE.search(url)
    return match.groups() if match is not None else None  # ty:ignore[invalid-return-type]


class InvalidEggFragment(DiagnosticCpipError):
    """A VCS egg fragment is not a valid direct reference."""

    reference = "invalid-egg-fragment"

    def __init__(self, message: str, *, hint_stmt: str = "") -> None:
        super().__init__(message=message, hint_stmt=hint_stmt)


@functools.total_ordering
class Link:
    __slots__ = [
        "_hash",
        "cache_link_parsing",
        "comes_from",
        "egg_fragment",
        "file_path_internal",
        "filename_internal",
        "hashes_internal",
        "kind",
        "local_identity_internal",
        "local_is_dir_internal",
        "metadata_file_data",
        "parsed_url_internal",
        "path_internal",
        "requires_python",
        "text",
        "upload_time",
        "url",
        "yanked_reason",
    ]

    def __init__(
        self,
        url: str,
        comes_from: str | None = None,
        requires_python: str | None = None,
        yanked_reason: str | None = None,
        metadata_file_data: MetadataFile | None = None,
        upload_time: datetime.datetime | None = None,
        cache_link_parsing: bool = True,
        hashes: Mapping[str, str] | None = None,
        text: str = "",
        kind: ArtifactKind | None = None,
        local_path_internal: str | None = None,
        local_identity_internal: str | None = None,
        local_is_dir_internal: bool | None = None,
    ) -> None:
        if url.startswith("\\\\"):
            url = path_to_url(url)
        self.parsed_url_internal = _urlsplit(url)
        self.url = url
        self._hash = hash(url)
        self.path_internal = urllib.parse.unquote(self.parsed_url_internal.path)
        self.filename_internal: PathComponent | None = None
        if local_path_internal is not None:
            self.file_path_internal = local_path_internal
        else:
            try:
                self.file_path_internal = (
                    url_to_path(url)
                    if self.parsed_url_internal.scheme == "file"
                    else None
                )
            except ValueError:
                # Preserve deferred validation for non-local file URLs.
                self.file_path_internal = None
        link_hash = hash_from_url_fragment(url)
        hashes_from_link = {} if link_hash is None else {link_hash[0]: link_hash[1]}
        self.hashes_internal = (
            hashes_from_link if hashes is None else {**hashes, **hashes_from_link}
        )
        self.comes_from = comes_from
        self.requires_python = requires_python or None
        self.yanked_reason = yanked_reason
        self.metadata_file_data = metadata_file_data
        self.upload_time = upload_time
        self.cache_link_parsing = cache_link_parsing
        self.egg_fragment = self.egg_fragment_internal()
        self.text = text
        self.local_identity_internal = local_identity_internal
        self.local_is_dir_internal = local_is_dir_internal
        self.kind = kind if kind is not None else self.artifact_kind()

    @property
    def scheme(self) -> str:
        return self.parsed_url_internal.scheme

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        source_url: str | None,
        text: str = "",
        hashes: Mapping[str, object] | None = None,
        requires_python: str | None = None,
        yanked_reason: str | None = None,
        metadata_file: MetadataFile | None = None,
        upload_time: datetime.datetime | None = None,
    ) -> Link:
        normalized_hashes = (
            None
            if hashes is None
            else {str(name): str(value) for name, value in hashes.items()}
        )
        return cls(
            url,
            comes_from=source_url,
            requires_python=requires_python,
            yanked_reason=yanked_reason,
            metadata_file_data=metadata_file,
            upload_time=upload_time,
            hashes=normalized_hashes,
            text=text,
        )

    @classmethod
    def from_cached_record(
        cls,
        url: str,
        *,
        parsed_url: urllib.parse.SplitResult,
        source_url: str | None,
        text: str,
        hashes: dict[str, str],
        requires_python: str | None,
        yanked_reason: str | None,
        metadata_file: MetadataFile | None,
        upload_time: datetime.datetime | None,
    ) -> Link:
        """Restore a link from the trusted on-disk catalog representation."""
        link = cls.__new__(cls)
        link.parsed_url_internal = parsed_url
        link.url = url
        link._hash = hash(url)
        link.path_internal = urllib.parse.unquote(parsed_url.path)
        link.filename_internal = None
        link.file_path_internal = None
        link.hashes_internal = hashes
        link.comes_from = source_url
        link.requires_python = requires_python or None
        link.yanked_reason = yanked_reason
        link.metadata_file_data = metadata_file
        link.upload_time = upload_time
        link.cache_link_parsing = True
        link.egg_fragment = None
        link.text = text
        link.local_identity_internal = None
        link.local_is_dir_internal = None
        link.kind = cls.artifact_kind_from_filename(
            posixpath.basename(link.path_internal.rstrip("/")),
        )
        return link

    @classmethod
    def from_path(
        cls,
        path: str,
        *,
        source_url: str | None,
        is_dir: bool | None = None,
        local_identity: str | None = None,
    ) -> Link:
        path_text = os.fspath(path)
        path_stat = None
        if is_dir is None:
            try:
                path_stat = os.stat(path_text)
            except OSError:
                is_dir = False
            else:
                is_dir = stat.S_ISDIR(path_stat.st_mode)
        if local_identity is None and path_stat is not None:
            local_identity = (
                f"stat:{path_stat.st_dev}:{path_stat.st_ino}:"
                f"{path_stat.st_size}:{path_stat.st_mtime_ns}"
            )
        # Keep the lexical path used by the caller.  Re-resolving temporary
        # paths through a file URL can rewrite `/var` to `/private/var` on
        # macOS, losing access to the project metadata.
        local_path = os.path.abspath(path_text) if is_dir else path_text
        return cls(
            path_to_url(local_path),
            comes_from=source_url,
            text=os.path.basename(path_text),
            kind=(
                ArtifactKind.SOURCE_TREE
                if is_dir
                else cls.artifact_kind_from_filename(os.path.basename(path_text))
            ),
            local_path_internal=str(local_path),
            local_identity_internal=local_identity,
            local_is_dir_internal=is_dir,
        )

    @property
    def metadata_file(self) -> MetadataFile | None:
        return self.metadata_file_data

    @property
    def source_url(self) -> str | None:
        return self.comes_from

    def metadata_link(self) -> Link | None:
        if self.metadata_file_data is None:
            return None
        hashes = self.metadata_file_data.hashes
        return Link(f"{self.url_without_fragment}.metadata", hashes=hashes)

    @property
    def is_yanked(self) -> bool:
        return self.yanked_reason is not None

    def artifact_kind(self) -> ArtifactKind:
        is_source_tree = self.is_vcs
        if self.is_file:
            try:
                cached = self.local_is_dir_internal
                if cached is None:
                    path_stat = os.stat(self.file_path)
                    is_source_tree = stat.S_ISDIR(path_stat.st_mode)
                    self.local_identity_internal = (
                        f"stat:{path_stat.st_dev}:{path_stat.st_ino}:"
                        f"{path_stat.st_size}:{path_stat.st_mtime_ns}"
                    )
                else:
                    is_source_tree = cached
                self.local_is_dir_internal = is_source_tree
            except (OSError, ValueError):
                is_source_tree = False
                self.local_is_dir_internal = False
        if is_source_tree:
            return ArtifactKind.SOURCE_TREE
        return self.artifact_kind_from_filename(str(self.filename))

    @staticmethod
    def artifact_kind_from_filename(filename: str) -> ArtifactKind:
        if filename.endswith(WHEEL_EXTENSION):
            return ArtifactKind.WHEEL
        if filename.endswith(".metadata"):
            return ArtifactKind.METADATA
        if filename.endswith(".attestation"):
            return ArtifactKind.ATTESTATION
        if filename.endswith(SOURCE_ARCHIVE_SUFFIXES):
            return ArtifactKind.SDIST
        return ArtifactKind.UNKNOWN

    @property
    def url_without_fragment(self) -> str:
        return urllib.parse.urlunsplit(self.parsed_url_internal._replace(fragment=""))

    @property
    def filename(self) -> PathComponent:
        cached = self.filename_internal
        if cached is not None:
            return cached
        name = PathComponent.from_name(posixpath.basename(self.path.rstrip("/")))
        filename = name or PathComponent.from_name(
            split_auth_from_netloc(self.netloc)[0],
        )
        self.filename_internal = filename
        return filename

    @property
    def is_wheel(self) -> bool:
        return self.ext == WHEEL_EXTENSION

    @property
    def file_path(self) -> str:
        if self.file_path_internal is None:
            return url_to_path(self.url)
        return self.file_path_internal

    @property
    def ext(self) -> str:
        return self.splitext()[1]

    def splitext(self) -> tuple[str, str]:
        base, ext = posixpath.splitext(posixpath.basename(self.path.rstrip("/")))
        if base.lower().endswith(".tar"):
            ext = base[-4:] + ext
            base = base[:-4]
        return base, ext

    @property
    def hash_name(self) -> str | None:
        return next(iter(self.hashes_internal), None)

    @property
    def hashes(self) -> dict[str, str]:
        return self.hashes_internal

    @property
    def hash(self) -> str | None:
        return next(iter(self.hashes_internal.values()), None)

    @property
    def subdirectory_fragment(self) -> str | None:
        match = re.search(r"[#&]subdirectory=([^&]*)", self.url)
        return match.group(1) if match else None

    @property
    def is_vcs(self) -> bool:
        return self.scheme in VCS_SCHEMES or self.url.startswith(VCS_SCHEMES_internal)

    @property
    def is_file(self) -> bool:
        return self.scheme == "file"

    @property
    def is_existing_dir(self) -> bool:
        if not self.is_file:
            return False
        cached = self.local_is_dir_internal
        return os.path.isdir(self.file_path) if cached is None else cached

    def is_hash_allowed(self, hashes: Hashes | None) -> bool:
        if hashes is None:
            return False
        return any(
            hashes.is_hash_allowed(name, digest)
            for name, digest in self.hashes_internal.items()
        )

    @property
    def redacted_url(self) -> str:
        return redact_auth_from_url(self.url)

    @property
    def netloc(self) -> str:
        return self.parsed_url_internal.netloc

    @property
    def path(self) -> str:
        return self.path_internal

    @property
    def show_url(self) -> str:
        return posixpath.basename(self.url.split("#", 1)[0].split("?", 1)[0])

    @property
    def has_hash(self) -> bool:
        return bool(self.hashes_internal)

    def egg_fragment_internal(self) -> str | None:
        url = self.url

        # ``egg=`` fragments are a legacy VCS-URL feature and rare in
        # practice; skipping the regex for the common case where the
        # substring is absent avoids firing the engine on every link.
        if "egg=" not in url:
            return None

        match = EGG_FRAGMENT_RE.search(url)
        if not match:
            return None
        name = match.group(1)
        if not REQ_NAME_RE.fullmatch(name):
            hint = (
                r"Use 'name\[extra] @ URL'. Version specifiers are silently ignored "
                "in egg fragments."
                if any(op in name for op in ("==", ">=", "<=", "!=", "~=", ">", "<"))
                or ("[" in name and "]" in name)
                else ""
            )
            raise InvalidEggFragment(f"Invalid egg fragment: {name!r}", hint_stmt=hint)
        return name

    def __str__(self) -> str:
        rp = (
            f" (requires-python:{self.requires_python})" if self.requires_python else ""
        )
        return (
            f"{self.redacted_url} (from {self.comes_from}){rp}"
            if self.comes_from
            else self.redacted_url
        )

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return self.url == other.url if isinstance(other, Link) else NotImplemented

    def __lt__(self, other: object) -> bool:
        return self.url < other.url if isinstance(other, Link) else NotImplemented

    def __repr__(self) -> str:
        return f"<Link {self}>"
