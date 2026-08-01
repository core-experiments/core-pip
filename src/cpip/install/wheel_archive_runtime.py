"""Runtime wheel-archive adapters used by transactional installation."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from cpip.core.wheel import WheelCandidate


class RawWheelInfo:
    """Small member record returned by the streaming archive adapter."""

    __slots__ = ("external_attr", "file_size", "filename")

    def __init__(self, filename: str, file_size: int, external_attr: int) -> None:
        self.filename = filename
        self.file_size = file_size
        self.external_attr = external_attr

    def is_dir(self) -> bool:
        return self.filename.endswith("/")


class RawWheelArchive:
    """ZipFile-shaped adapter over the fast wheelhouse archive reader."""

    __slots__ = ("NameToInfo", "_archive", "_file", "_infos")

    def __init__(self, file: Any, archive: Any) -> None:
        self._file = file
        self._archive = archive
        self._infos = [
            RawWheelInfo(
                name,
                member[3],
                getattr(archive, "modes", {}).get(name, 0),
            )
            for name, member in archive.members.items()
        ]
        self.NameToInfo = {info.filename: info for info in self._infos}

    def infolist(self) -> list[RawWheelInfo]:
        return self._infos

    def namelist(self) -> list[str]:
        return [info.filename for info in self._infos]

    def read(self, member: str | RawWheelInfo) -> bytes:
        name = member if isinstance(member, str) else member.filename
        return self._archive.read(name)

    def open(self, member: RawWheelInfo) -> io.BytesIO:
        return io.BytesIO(self.read(member))

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> RawWheelArchive:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_wheel_archive(
    path: str | Path,
    candidate: WheelCandidate,
) -> zipfile.ZipFile | RawWheelArchive:
    """Open a fast raw archive when its members fit the streaming contract."""
    from cpip.resolution.engine.sources.wheelhouse.archive import (
        WheelArchive,
        WheelhouseUnavailable,
    )

    layout = getattr(candidate, "wheel_layout", None)
    if layout is not None:
        # The resolver layout predates external mode bits; retain ZipInfo for
        # those candidates so executable members keep their original modes.
        return zipfile.ZipFile(path)
    members = None
    if members is not None and any(
        member[0] not in {0, 8} or member[2] > 1024 * 1024
        for member in members.values()
    ):
        return zipfile.ZipFile(path)
    try:
        file = open(path, "rb")
        archive = WheelArchive(file, members=members)
    except (OSError, ValueError, WheelhouseUnavailable):
        try:
            file.close()
        except UnboundLocalError:
            pass
        return zipfile.ZipFile(path)
    if any(
        member[0] not in {0, 8} or member[3] > 1024 * 1024
        for member in archive.members.values()
    ):
        file.close()
        return zipfile.ZipFile(path)
    return RawWheelArchive(file, archive)
