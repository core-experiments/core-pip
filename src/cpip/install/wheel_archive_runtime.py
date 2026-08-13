"""Runtime wheel-archive adapters used by transactional installation."""

from __future__ import annotations

import io
import os
import zipfile

from cpip.install.wheel_archive_cache import CachedWheelArchive
from cpip.resolution.archive import (
    WheelArchive,
    WheelhouseUnavailable,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
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


class CachedWheelInfo:
    """ZipInfo-shaped member backed by an immutable unpacked wheel tree."""

    __slots__ = (
        "external_attr",
        "file_size",
        "filename",
        "record_metadata",
        "source_path",
    )

    def __init__(
        self,
        tree: str,
        filename: str,
        digest: str,
        size: str,
        mode: int,
    ) -> None:
        self.filename = filename

        self.file_size = int(size)

        self.external_attr = mode << 16

        self.record_metadata = (digest, size)

        self.source_path = os.path.join(tree, *filename.split("/"))

    def is_dir(self) -> bool:
        return False


class CachedWheelTreeArchive:
    """ZipFile-shaped adapter over a :class:`CachedWheelArchive`."""

    __slots__ = ("NameToInfo", "_infos")

    def __init__(self, layout: Any) -> None:
        self._infos = [
            CachedWheelInfo(layout.tree, relative, digest, size, mode)
            for relative, digest, size, mode in layout.entries
        ]

        self.NameToInfo = {info.filename: info for info in self._infos}

    def infolist(self) -> list[CachedWheelInfo]:
        return self._infos

    def namelist(self) -> list[str]:
        return [info.filename for info in self._infos]

    def read(self, member: str | CachedWheelInfo) -> bytes:
        info = self.NameToInfo[member] if isinstance(member, str) else member

        with open(info.source_path, "rb") as file:
            return file.read()

    def open(self, member: CachedWheelInfo):
        return open(member.source_path, "rb")

    def close(self) -> None:
        pass

    def __enter__(self) -> CachedWheelTreeArchive:
        return self

    def __exit__(self, *_: object) -> None:
        pass


def open_wheel_archive(
    path: str,
    candidate: WheelCandidate,
) -> zipfile.ZipFile | RawWheelArchive | CachedWheelTreeArchive:
    """Open a fast raw archive when its members fit the streaming contract."""

    layout = getattr(candidate, "wheel_layout", None)

    if layout is not None:
        if isinstance(layout, CachedWheelArchive):
            return CachedWheelTreeArchive(layout)

    if layout is not None:
        # The resolver layout predates external mode bits; retain ZipInfo for

        # those candidates so executable members keep their original modes.

        return zipfile.ZipFile(path)

    try:
        file = open(path, "rb")

        archive = WheelArchive(file)

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
