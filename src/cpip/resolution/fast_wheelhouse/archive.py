"""Low-level wheel archive reading for the local wheelhouse resolver."""

from __future__ import annotations

import struct

END_OF_CENTRAL_DIRECTORY = struct.Struct("<4s4H2LH")
CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")


class WheelhouseUnavailable(Exception):
    pass


class WheelArchive:
    __slots__ = ("file", "members")

    def __init__(self, file, members=None, target: str | None = None) -> None:
        self.file = file
        self.members: dict[str, tuple[int, int, int, int, int]] = (
            {} if members is None else members
        )
        if members is None:
            self.read_central_directory(target)

    def read_central_directory(self, target: str | None = None) -> None:
        self.file.seek(0, 2)
        size = self.file.tell()
        tail_size = min(size, 22 + 65535)
        self.file.seek(size - tail_size)
        tail = self.file.read(tail_size)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or marker + 22 > len(tail):
            raise WheelhouseUnavailable
        _, _, _, _, entries, directory_size, directory_offset, _ = (
            END_OF_CENTRAL_DIRECTORY.unpack_from(tail, marker)
        )
        if (
            entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise WheelhouseUnavailable
        self.file.seek(directory_offset)
        target_bytes = target.encode("utf-8") if target is not None else None
        undecoded: list[tuple[bytes, int, tuple[int, int, int, int, int]]] = []
        for _ in range(entries):
            header = self.file.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise WheelhouseUnavailable
            (
                _,
                _,
                _,
                flags,
                compression,
                _,
                _,
                crc,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
                comment_size,
                _,
                _,
                _,
                local_offset,
            ) = CENTRAL_DIRECTORY_HEADER.unpack(header)
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise WheelhouseUnavailable
            name_bytes = self.file.read(name_size)
            self.file.seek(extra_size + comment_size, 1)
            member = (
                compression,
                crc,
                compressed_size,
                uncompressed_size,
                local_offset,
            )
            if target_bytes is not None and name_bytes == target_bytes:
                assert target is not None
                self.members[target] = member
                return
            if target_bytes is not None:
                undecoded.append((name_bytes, flags, member))
                continue
            try:
                name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError as exc:
                raise WheelhouseUnavailable from exc
            self.members[name] = member
        for name_bytes, flags, member in undecoded:
            try:
                name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError as exc:
                raise WheelhouseUnavailable from exc
            self.members[name] = member

    def namelist(self) -> list[str]:
        return list(self.members)

    def read(self, name: str) -> bytes:
        try:
            member = self.members[name]
        except KeyError as exc:
            raise WheelhouseUnavailable from exc
        return self.read_member(member)

    def read_member(self, member: tuple[int, int, int, int, int]) -> bytes:
        compression, crc, compressed_size, uncompressed_size, local_offset = member
        self.file.seek(local_offset)
        header = self.file.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise WheelhouseUnavailable
        _, _, _, _, _, _, _, _, _, name_size, extra_size = LOCAL_FILE_HEADER.unpack(
            header
        )
        self.file.seek(name_size + extra_size, 1)
        data = self.file.read(compressed_size)
        if len(data) != compressed_size:
            raise WheelhouseUnavailable
        import zlib

        if compression == 0:
            result = data
        elif compression == 8:
            try:
                result = zlib.decompress(data, -15)
            except zlib.error as exc:
                raise WheelhouseUnavailable from exc
        else:
            raise WheelhouseUnavailable
        if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
            raise WheelhouseUnavailable
        return result

    def read_many(self, names: list[str]) -> list[bytes]:
        """Read members in archive order while returning the requested order."""
        ordered = sorted(
            ((self.members[name][4], name) for name in names),
            key=lambda item: item[0],
        )
        results: dict[str, bytes] = {}
        position = -1
        import zlib

        for local_offset, name in ordered:
            compression, crc, compressed_size, uncompressed_size, _ = self.members[name]
            if local_offset != position:
                self.file.seek(local_offset)
            header = self.file.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            (_, _, _, _, _, _, _, _, _, name_size, extra_size) = (
                LOCAL_FILE_HEADER.unpack(header)
            )
            self.file.seek(name_size + extra_size, 1)
            data = self.file.read(compressed_size)
            if len(data) != compressed_size:
                raise WheelhouseUnavailable
            if compression == 0:
                result = data
            elif compression == 8:
                try:
                    result = zlib.decompress(data, -15)
                except zlib.error as exc:
                    raise WheelhouseUnavailable from exc
            else:
                raise WheelhouseUnavailable
            if (
                len(result) != uncompressed_size
                or zlib.crc32(result) & 0xFFFFFFFF != crc
            ):
                raise WheelhouseUnavailable
            results[name] = result
            position = local_offset + 30 + name_size + extra_size + compressed_size
        return [results[name] for name in names]
