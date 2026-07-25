from __future__ import annotations

import datetime
import functools
import posixpath
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from pip.core.errors import DiagnosticPipError
from pip.core.hashes import Hashes
from pip.core.urls import (
    path_to_url,
    redact_auth_from_url,
    split_auth_from_netloc,
    url_to_path,
)
from pip.index.hashes import SUPPORTED_HASHES, supported_hashes
from pip.index.datetime import parse_iso_datetime
from pip.index.paths import PathComponent
from pip.index.vcs import VCS_SCHEMES
from pip.index.source_models import ArtifactKind, MetadataFile

_VCS_SCHEMES = tuple(f"{scheme}+" for scheme in VCS_SCHEMES)
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
_REQ_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HASH_URL_FRAGMENT_RE = re.compile(
    r"[#&]({choices})=([^&]*)".format(
        choices="|".join(re.escape(name) for name in SUPPORTED_HASHES)
    )
)


@functools.cache
def _hash_from_url_fragment(url: str) -> tuple[str, str] | None:
    match = _HASH_URL_FRAGMENT_RE.search(url)
    return match.groups() if match is not None else None


class InvalidEggFragment(DiagnosticPipError):
    """A VCS egg fragment is not a valid direct reference."""

    reference = "invalid-egg-fragment"

    def __init__(self, message: str, *, hint_stmt: str = "") -> None:
        super().__init__(message=message, hint_stmt=hint_stmt)


def _clean_url_path_part(part: str) -> str:
    return urllib.parse.quote(urllib.parse.unquote(part))


def _clean_file_url_path(part: str) -> str:
    ret = urllib.request.pathname2url(urllib.request.url2pathname(part))
    if ret.startswith("///"):
        ret = ret.removeprefix("//")
    return ret


_reserved_chars_re = re.compile(r"(@|%2F)", re.IGNORECASE)


def _clean_url_path(path: str, is_local_path: bool) -> str:
    clean_func = _clean_file_url_path if is_local_path else _clean_url_path_part
    parts = _reserved_chars_re.split(path)
    cleaned: list[str] = []
    for to_clean, reserved in zip(parts[::2], parts[1::2] + [""]):
        cleaned.extend((clean_func(to_clean), reserved.upper()))
    return "".join(cleaned)


def _ensure_quoted_url(url: str) -> str:
    result = urllib.parse.urlsplit(url)
    path = _clean_url_path(result.path, is_local_path=not result.netloc)
    ret = urllib.parse.urlunsplit(result._replace(scheme="file", path=path))
    return result.scheme + ret[4:]


def _absolute_link_url(base_url: str, url: str) -> str:
    return (
        url
        if url.startswith(("https://", "http://"))
        else urllib.parse.urljoin(base_url, url)
    )


class LinkType(Enum):
    candidate = "candidate"
    format_unsupported = "format-unsupported"
    format_invalid = "format-invalid"
    different_project = "different-project"
    requires_python_mismatch = "requires-python-mismatch"
    yanked = "yanked"
    platform_mismatch = "platform-mismatch"
    upload_too_late = "upload-too-late"
    upload_time_missing = "upload-time-missing"


@functools.total_ordering
class Link:
    __slots__ = [
        "_parsed_url",
        "_url",
        "_path",
        "_hashes",
        "comes_from",
        "requires_python",
        "yanked_reason",
        "metadata_file_data",
        "upload_time",
        "cache_link_parsing",
        "egg_fragment",
        "text",
        "kind",
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
    ) -> None:
        if url.startswith("\\\\"):
            url = path_to_url(url)
        self._parsed_url = urllib.parse.urlsplit(url)
        self._url = url
        self._path = urllib.parse.unquote(self._parsed_url.path)
        link_hash = _hash_from_url_fragment(url)
        hashes_from_link = {} if link_hash is None else {link_hash[0]: link_hash[1]}
        self._hashes = (
            hashes_from_link if hashes is None else {**hashes, **hashes_from_link}
        )
        self.comes_from = comes_from
        self.requires_python = requires_python or None
        self.yanked_reason = yanked_reason
        self.metadata_file_data = metadata_file_data
        self.upload_time = upload_time
        self.cache_link_parsing = cache_link_parsing
        self.egg_fragment = self._egg_fragment()
        self.text = text
        self.kind = kind if kind is not None else self._artifact_kind()

    @property
    def scheme(self) -> str:
        return self._parsed_url.scheme

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
    ) -> "Link":
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
    def from_path(cls, path: Path, *, source_url: str | None) -> "Link":
        if path.is_dir():
            return cls(
                path.resolve().as_uri(),
                comes_from=source_url,
                text=path.name,
                kind=ArtifactKind.SOURCE_TREE,
            )
        return cls.from_url(path.as_uri(), source_url=source_url)

    @classmethod
    def from_element(
        cls, attrs: dict[str, str | None], page_url: str, base_url: str
    ) -> "Link | None":
        href = attrs.get("href")
        if not href:
            return None
        url = _ensure_quoted_url(_absolute_link_url(base_url, href))
        metadata_info = attrs.get("data-core-metadata")
        if metadata_info is None:
            metadata_info = attrs.get("data-dist-info-metadata")
        if metadata_info == "true":
            metadata = MetadataFile(None)
        elif metadata_info is None:
            metadata = None
        else:
            name, sep, value = metadata_info.partition("=")
            metadata = (
                MetadataFile(supported_hashes({name: value}))
                if sep
                else MetadataFile(None)
            )
        upload_time = (
            parse_iso_datetime(attrs["data-upload-time"])
            if attrs.get("data-upload-time")
            else None
        )
        return cls(
            url,
            comes_from=page_url,
            requires_python=attrs.get("data-requires-python"),
            yanked_reason=attrs.get("data-yanked"),
            metadata_file_data=metadata,
            upload_time=upload_time,
            text=attrs.get("text", ""),
        )

    @classmethod
    def from_json(cls, file_data: dict[str, Any], page_url: str) -> "Link | None":
        file_url = file_data.get("url")
        if file_url is None:
            return None
        absolute = _ensure_quoted_url(_absolute_link_url(page_url, file_url))
        requires_python = file_data.get("requires-python")
        yanked = file_data.get("yanked")
        hashes = file_data.get("hashes", {})
        metadata_info = file_data.get("core-metadata")
        if metadata_info is None:
            metadata_info = file_data.get("dist-info-metadata")
        if isinstance(metadata_info, dict):
            metadata = MetadataFile(supported_hashes(metadata_info))
        elif metadata_info:
            metadata = MetadataFile(None)
        else:
            metadata = None
        upload_time = (
            parse_iso_datetime(file_data["upload-time"])
            if file_data.get("upload-time")
            else None
        )
        return cls(
            absolute,
            comes_from=page_url,
            requires_python=requires_python
            if isinstance(requires_python, str)
            else None,
            yanked_reason=""
            if yanked and not isinstance(yanked, str)
            else (yanked or None),
            metadata_file_data=metadata,
            upload_time=upload_time,
            hashes=hashes if isinstance(hashes, dict) else None,
            text=str(file_data.get("filename") or ""),
        )

    @property
    def metadata_file(self) -> MetadataFile | None:
        return self.metadata_file_data

    @property
    def source_url(self) -> str | None:
        return self.comes_from

    def metadata_link(self) -> "Link | None":
        if self.metadata_file_data is None:
            return None
        hashes = self.metadata_file_data.hashes
        return Link(f"{self.url_without_fragment}.metadata", hashes=hashes)

    @property
    def is_yanked(self) -> bool:
        return self.yanked_reason is not None

    def _artifact_kind(self) -> ArtifactKind:
        is_source_tree = self.is_vcs
        if self.is_file:
            try:
                is_source_tree = Path(self.file_path).is_dir()
            except ValueError:
                pass
        if is_source_tree:
            return ArtifactKind.SOURCE_TREE
        filename = str(self.filename)
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
        return urllib.parse.urlunsplit(self._parsed_url._replace(fragment=""))

    @property
    def filename(self) -> PathComponent:
        name = PathComponent.from_name(posixpath.basename(self.path.rstrip("/")))
        return name or PathComponent.from_name(split_auth_from_netloc(self.netloc)[0])

    @property
    def is_wheel(self) -> bool:
        return self.ext == WHEEL_EXTENSION

    @property
    def file_path(self) -> str:
        return url_to_path(self.url)

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
        return next(iter(self._hashes), None)

    @property
    def hashes(self) -> dict[str, str]:
        return self._hashes

    @property
    def hash(self) -> str | None:
        return next(iter(self._hashes.values()), None)

    @property
    def subdirectory_fragment(self) -> str | None:
        match = re.search(r"[#&]subdirectory=([^&]*)", self.url)
        return match.group(1) if match else None

    @property
    def is_vcs(self) -> bool:
        return self.scheme in {"git", "hg", "svn", "bzr"} or self.url.startswith(
            _VCS_SCHEMES
        )

    @property
    def is_file(self) -> bool:
        return self.scheme == "file"

    @property
    def is_existing_dir(self) -> bool:
        return self.is_file and Path(self.file_path).is_dir()

    def is_hash_allowed(self, hashes: Hashes | None) -> bool:
        if hashes is None:
            return False
        return any(
            hashes.is_hash_allowed(name, digest)
            for name, digest in self._hashes.items()
        )

    @property
    def url(self) -> str:
        return self._url

    @property
    def redacted_url(self) -> str:
        return redact_auth_from_url(self.url)

    @property
    def netloc(self) -> str:
        return self._parsed_url.netloc

    @property
    def path(self) -> str:
        return self._path

    @property
    def show_url(self) -> str:
        return posixpath.basename(self.url.split("#", 1)[0].split("?", 1)[0])

    @property
    def has_hash(self) -> bool:
        return bool(self._hashes)

    def as_hashes(self) -> Hashes:
        return Hashes({name: [value] for name, value in self._hashes.items()})

    def _egg_fragment(self) -> str | None:
        match = re.search(r"[#&]egg=([^&]*)", self._url)
        if not match:
            return None
        name = match.group(1)
        if not _REQ_NAME_RE.fullmatch(name):
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
        return hash(self.url)

    def __eq__(self, other: object) -> bool:
        return self.url == other.url if isinstance(other, Link) else NotImplemented

    def __lt__(self, other: object) -> bool:
        return self.url < other.url if isinstance(other, Link) else NotImplemented

    def __repr__(self) -> str:
        return f"<Link {self}>"
