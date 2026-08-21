"""Wheel archive validation and RECORD metadata helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import stat

from cpip.core.errors import InstallationError

TYPE_CHECKING = False

if TYPE_CHECKING:
    import zipfile

    from cpip.install.target import InstallTarget

DestinationCache = dict[tuple[str, str], str]
ResolvedRoots = dict[str, str]
EMPTY_RECORD_METADATA = (
    "sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU",
    "0",
)


def validate_member_parts(name: str) -> tuple[str, ...]:
    if "\\" in name:
        raise InstallationError(f"wheel member uses an invalid separator: {name!r}")
    if name.startswith("/"):
        raise InstallationError(
            f"wheel member is outside the install destination: {name!r}",
        )
    parts = tuple(part for part in name.split("/") if part and part != ".")
    if ".." in parts:
        raise InstallationError(
            f"wheel member is outside the install destination: {name!r}",
        )
    return parts


def destination_internal_parts_text(
    target: InstallTarget,
    parts: tuple[str, ...],
    display_relative: tuple[str, ...] | str,
    *,
    resolved_directories: DestinationCache | None = None,
    resolved_roots: ResolvedRoots | None = None,
) -> str:
    if not parts or not parts[0].endswith(".data"):
        return _safe_destination_parts_with_text(
            target.purelib,
            parts,
            display_relative,
            resolved_directories=resolved_directories,
            resolved_roots=resolved_roots,
        )
    if len(parts) < 3 or parts[1] not in {
        "purelib",
        "platlib",
        "scripts",
        "data",
        "headers",
    }:
        raise InstallationError(f"invalid wheel data path: {display_relative}")
    base = getattr(target, parts[1])
    return _safe_destination_parts_with_text(
        base,
        parts[2:],
        display_relative,
        resolved_directories=resolved_directories,
        resolved_roots=resolved_roots,
    )


def _safe_destination_parts_with_text(
    root: str,
    parts: tuple[str, ...],
    display_relative: tuple[str, ...] | str,
    *,
    resolved_directories: DestinationCache | None = None,
    resolved_roots: ResolvedRoots | None = None,
) -> str:
    resolved_parent = _resolved_parent_directory(
        root,
        parts[:-1],
        display_relative,
        resolved_directories=resolved_directories,
        resolved_roots=resolved_roots,
    )
    name = parts[-1] if parts else ""
    destination_text = os.path.join(resolved_parent, name)
    return destination_text


def _resolved_parent_directory(
    root: str,
    parent_parts: tuple[str, ...],
    display_relative: tuple[str, ...] | str,
    *,
    resolved_directories: DestinationCache | None = None,
    resolved_roots: ResolvedRoots | None = None,
) -> str:
    root_text = root
    parent_text = os.path.join(*parent_parts) if parent_parts else ""
    cache_key = (root_text, parent_text)
    resolved_parent = (
        resolved_directories.get(cache_key)
        if resolved_directories is not None
        else None
    )
    if resolved_parent is None:
        resolved_root = (
            resolved_roots.get(root_text) if resolved_roots is not None else None
        )
        if resolved_root is None:
            resolved_root = os.path.realpath(root_text)
            if resolved_roots is not None:
                resolved_roots[root_text] = resolved_root
        resolved_parent_text = (
            resolved_root
            if not parent_parts
            else os.path.realpath(os.path.join(root_text, *parent_parts))
        )
        try:
            if (
                os.path.commonpath((resolved_parent_text, resolved_root))
                != resolved_root
            ):
                raise ValueError
        except (OSError, ValueError) as exc:
            raise InstallationError(
                f"wheel member escapes installation root: {display_relative}",
            ) from exc
        if resolved_directories is not None:
            resolved_directories[cache_key] = resolved_parent_text
        resolved_parent = resolved_parent_text
    return resolved_parent


_DATA_KINDS = frozenset({"purelib", "platlib", "scripts", "data", "headers"})


class MemberPaths:
    """Per-wheel resolver for one member's staged source, destination and RECORD key.

    The install loop used to validate, split, join and destination-resolve
    every member name from scratch -- a tuple of parts, a join of the stage
    root with all of them, another join of the parent parts just to key the
    realpath cache, and a final join of the resolved parent with the name --
    which on a many-thousand-member wheel put posixpath.join and friends at
    a quarter of the install's time. Members are overwhelmingly siblings,
    so this resolves each *directory* once (validation, staged prefix,
    resolved destination prefix, RECORD key prefix) and appends the bare
    name per member.

    Anything the per-directory shortcut cannot answer byte-for-byte --
    an empty, ``.``, ``..``, backslash- or colon-bearing basename, a
    top-level name ending in ``.data``, or a directory that fails validation
    -- takes the original per-member path so every error message and edge
    case stays exactly as before.
    """

    __slots__ = (
        "directories",
        "resolved_directories",
        "resolved_roots",
        "stage_root",
        "target",
    )

    def __init__(
        self,
        target: InstallTarget,
        stage_root: str,
        *,
        resolved_directories: DestinationCache | None = None,
        resolved_roots: ResolvedRoots | None = None,
    ) -> None:
        self.target = target
        self.stage_root = stage_root
        self.resolved_directories = resolved_directories
        self.resolved_roots = resolved_roots
        self.directories: dict[str, tuple[tuple[str, ...], str, str, str] | None] = {}

    def resolve(self, filename: str) -> tuple[tuple[str, ...], str, str, str]:
        """Return ``(relative_parts, source_text, destination_text, record_key)``.

        Identical to ``validate_member_parts(filename)``,
        ``os.path.join(stage_root, *parts)``,
        ``destination_internal_parts_text(target, parts, filename, ...)`` and
        ``"/".join(parts)`` computed independently.
        """
        directory, _, name = filename.rpartition("/")
        if (
            name
            and name != "."
            and name != ".."
            and "\\" not in name
            and ":" not in name
            and (directory or not name.endswith(".data"))
        ):
            try:
                entry = self.directories[directory]
            except KeyError:
                entry = self._directory_entry(directory, filename)
                self.directories[directory] = entry
            if entry is not None:
                prefix, source_prefix, destination_prefix, record_prefix = entry
                return (
                    (*prefix, name),
                    source_prefix + name,
                    destination_prefix + name,
                    record_prefix + name,
                )
        parts = validate_member_parts(filename)
        return (
            parts,
            os.path.join(self.stage_root, *parts),
            destination_internal_parts_text(
                self.target,
                parts,
                filename,
                resolved_directories=self.resolved_directories,
                resolved_roots=self.resolved_roots,
            ),
            "/".join(parts),
        )

    def _directory_entry(
        self,
        directory: str,
        display_relative: str,
    ) -> tuple[tuple[str, ...], str, str, str] | None:
        try:
            prefix = validate_member_parts(directory)
        except InstallationError:
            # Let the per-member path raise, with the member's full name.
            return None
        if prefix and prefix[0].endswith(".data"):
            if len(prefix) < 2 or prefix[1] not in _DATA_KINDS:
                return None
            root = getattr(self.target, prefix[1])
            parent_parts = prefix[2:]
        else:
            root = self.target.purelib
            parent_parts = prefix
        resolved_parent = _resolved_parent_directory(
            root,
            parent_parts,
            display_relative,
            resolved_directories=self.resolved_directories,
            resolved_roots=self.resolved_roots,
        )
        return (
            prefix,
            os.path.join(self.stage_root, *prefix, ""),
            os.path.join(resolved_parent, ""),
            "/".join(prefix) + "/" if prefix else "",
        )


def mode_from_external_attr(external_attr: int) -> int | None:
    mode = external_attr >> 16
    return mode if mode and stat.S_ISREG(mode) else None


def zip_mode(info: zipfile.ZipInfo) -> int | None:
    return mode_from_external_attr(info.external_attr)


def record_metadata_internal(contents: bytes) -> tuple[str, str]:
    if not contents:
        return EMPTY_RECORD_METADATA
    digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest())
    return f"sha256={digest.rstrip(b'=').decode('ascii')}", str(len(contents))


def copy_member_with_metadata(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: str,
    *,
    metadata: tuple[str, str] | None = None,
) -> tuple[str, str]:
    if member.file_size <= 1024 * 1024:
        contents = archive.read(member)
        with open(destination, "wb") as target:
            target.write(contents)
        if metadata is not None:
            return metadata
        digest = hashlib.sha256(contents).digest()
        encoded = base64.urlsafe_b64encode(digest)
        return f"sha256={encoded.rstrip(b'=').decode('ascii')}", str(len(contents))

    digest = hashlib.sha256()
    size = 0
    with archive.open(member) as source, open(destination, "wb") as target:
        while chunk := source.read(64 * 1024):
            target.write(chunk)
            if metadata is None:
                digest.update(chunk)
            size += len(chunk)
    if metadata is not None:
        return metadata
    encoded = base64.urlsafe_b64encode(digest.digest())
    return f"sha256={encoded.rstrip(b'=').decode('ascii')}", str(size)
