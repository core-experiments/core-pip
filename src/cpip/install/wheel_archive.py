"""Wheel archive validation and RECORD metadata helpers."""

from __future__ import annotations

import base64
import hashlib
import stat
import zipfile
from pathlib import Path, PurePosixPath

from cpip.core.errors import InstallationError
from cpip.install.target import InstallTarget


def validate_member(name: str) -> PurePosixPath:
    if "\\" in name:
        raise InstallationError(f"wheel member uses an invalid separator: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise InstallationError(
            f"wheel member is outside the install destination: {name!r}"
        )
    return relative


def destination_internal(
    target: InstallTarget,
    relative: PurePosixPath,
    *,
    resolved_directories: dict[tuple[Path, PurePosixPath], Path] | None = None,
    resolved_roots: dict[Path, Path] | None = None,
) -> Path:
    parts = relative.parts
    if not parts or not parts[0].endswith(".data"):
        return safe_destination(
            target.purelib,
            relative,
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
        raise InstallationError(f"invalid wheel data path: {relative}")
    base = getattr(target, parts[1])
    return safe_destination(
                    base,
                    PurePosixPath(*parts[2:]),
                    resolved_directories=resolved_directories,
                    resolved_roots=resolved_roots,
    )


def safe_destination(
    root: Path,
    relative: PurePosixPath,
    *,
    resolved_directories: dict[tuple[Path, PurePosixPath], Path] | None = None,
    resolved_roots: dict[Path, Path] | None = None,
) -> Path:
    parent = relative.parent
    cache_key = (root, parent)
    resolved_parent = (
        resolved_directories.get(cache_key)
        if resolved_directories is not None
        else None
    )
    if resolved_parent is None:
        resolved_root = (
            resolved_roots.get(root) if resolved_roots is not None else None
        )
        if resolved_root is None:
            resolved_root = root.resolve(strict=False)
            if resolved_roots is not None:
                resolved_roots[root] = resolved_root
        resolved_parent = (root / Path(*parent.parts)).resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_root)
        except ValueError as exc:
            raise InstallationError(
                f"wheel member escapes installation root: {relative}"
            ) from exc
        if resolved_directories is not None:
            resolved_directories[cache_key] = resolved_parent
    return resolved_parent / relative.name


def zip_mode(info: zipfile.ZipInfo) -> int | None:
    mode = info.external_attr >> 16
    return mode if mode and stat.S_ISREG(mode) else None


def record_metadata_internal(contents: bytes) -> tuple[str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(contents).digest())
    return f"sha256={digest.rstrip(b'=').decode('ascii')}", str(len(contents))


def copy_member_with_metadata(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: Path
) -> tuple[str, str]:
    if member.file_size <= 1024 * 1024:
        contents = archive.read(member)
        with open(destination, "wb") as target:
            target.write(contents)
        digest = hashlib.sha256(contents).digest()
        encoded = base64.urlsafe_b64encode(digest)
        return f"sha256={encoded.rstrip(b'=').decode('ascii')}", str(len(contents))

    digest = hashlib.sha256()
    size = 0
    with archive.open(member) as source, open(destination, "wb") as target:
        while chunk := source.read(64 * 1024):
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    encoded = base64.urlsafe_b64encode(digest.digest())
    return f"sha256={encoded.rstrip(b'=').decode('ascii')}", str(size)


def is_script_member(relative: PurePosixPath) -> bool:
    return len(relative.parts) >= 2 and relative.parts[-2] == "scripts"
