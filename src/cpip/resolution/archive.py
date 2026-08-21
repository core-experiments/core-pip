"""Low-level wheel archive reading for the local wheelhouse resolver."""

from __future__ import annotations

import io
import struct
import zlib

END_OF_CENTRAL_DIRECTORY = struct.Struct("<4s4H2LH")
CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")

# Headroom for read_member's combined read: enough for any realistic member
# name plus local extra field (zipfile-written wheels have no local extra at
# all). A longer name only costs the fallback read, never correctness.
_LOCAL_HEADER_HEADROOM = 512


class WheelhouseUnavailable(Exception):
    pass


class WheelArchive:
    __slots__ = ("_tail", "_tail_start", "file", "members", "modes")

    def __init__(self, file, members=None, modes=None) -> None:
        self.file = file
        self.members: dict[str, tuple[int, int, int, int, int]] = (
            {} if members is None else members
        )
        # External attributes (mode bits) per member, filled alongside
        # members by read_central_directory() or pre-supplied with them.
        self.modes: dict[str, int] = {} if modes is None else modes
        # Populated by read_central_directory() with the same bytes its
        # end-of-central-directory scan already had to read from the tail of
        # the file. Members whose local header falls inside that region
        # (the common case for a wheel small enough that the tail scan
        # covers the whole file) can be read straight out of it in
        # read_member()/read_many() instead of a second file seek+read.
        # Empty/zero when unset (a pre-supplied `members` cache skips the
        # scan), which read_member()/read_many() treat as "nothing cached".
        self._tail = b""
        self._tail_start = 0
        if members is None:
            self.read_central_directory()

    def read_central_directory(self) -> None:
        self.file.seek(0, 2)
        size = self.file.tell()
        tail_size = min(size, 22 + 65535)
        tail_start = size - tail_size
        self.file.seek(tail_start)
        tail = self.file.read(tail_size)
        self._tail = tail
        self._tail_start = tail_start
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
        if directory_offset + directory_size > size:
            # The declared directory range must fit inside the file --
            # checked before reading so a lying directory_size (the field
            # can claim up to 4 GiB) declines instead of attempting the
            # allocation.
            raise WheelhouseUnavailable
        if directory_offset >= tail_start:
            # Already sitting in `tail` from the scan above -- slice the
            # central directory out of it instead of paying for a second
            # seek+read round trip to fetch bytes already in hand.
            start = directory_offset - tail_start
            directory = tail[start : start + directory_size]
        else:
            self.file.seek(directory_offset)
            directory = self.file.read(directory_size)
        if len(directory) != directory_size:
            raise WheelhouseUnavailable
        # Parse the records in place with a running offset. The obvious
        # loop -- two stream read()s and a seek() per record -- costs three
        # method calls and a 46-byte allocation for every member, tens of
        # thousands of pure overhead calls on a many-file wheel;
        # unpack_from reads the buffer directly. Bounding the parse by the
        # end-of-central-directory record's own directory_size also matches
        # what zipfile itself does.
        directory_end = len(directory)
        unpack_record = CENTRAL_DIRECTORY_HEADER.unpack_from
        offset = 0
        for _ in range(entries):
            if offset + 46 > directory_end:
                raise WheelhouseUnavailable
            (
                signature,
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
                external_attr,
                local_offset,
            ) = unpack_record(directory, offset)
            if signature != b"PK\x01\x02":
                raise WheelhouseUnavailable
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise WheelhouseUnavailable
            name_end = offset + 46 + name_size
            record_end = name_end + extra_size + comment_size
            if record_end > directory_end:
                # The whole declared record body -- name, extra field, and
                # comment -- must fit inside the declared directory, or a
                # final record with an oversized extra field would be
                # accepted despite claiming bytes past the boundary.
                raise WheelhouseUnavailable
            name_bytes = directory[offset + 46 : name_end]
            offset = record_end
            member = (
                compression,
                crc,
                compressed_size,
                uncompressed_size,
                local_offset,
            )
            try:
                name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError as exc:
                raise WheelhouseUnavailable from exc
            if name in self.members:
                # self.members is keyed by name, so a second record for the
                # same name would silently overwrite the first -- including
                # its independent compressed data at a different offset,
                # which then never gets read or CRC-checked at all. A real
                # archive never has two central-directory records for the
                # same name; something crafted enough to have one shouldn't
                # get the abbreviated trust this reader extends everything
                # else.
                raise WheelhouseUnavailable
            self.members[name] = member
            self.modes[name] = external_attr

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
        if self._tail and local_offset >= self._tail_start:
            # Already in memory: slice the header and data straight out of
            # the tail buffer, with no stream object or read calls at all.
            base = local_offset - self._tail_start
            tail = self._tail
            header = tail[base : base + 30]
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            _, _, _, _, _, _, _, _, _, name_size, extra_size = LOCAL_FILE_HEADER.unpack(
                header,
            )
            start = base + 30 + name_size + extra_size
            data = tail[start : start + compressed_size]
        else:
            self.file.seek(local_offset)
            # One combined over-read: header, the (nearly always short)
            # name and extra field, and the member data together -- sliced
            # apart afterward. The obvious sequence pays a header read, a
            # relative seek, and a data read: three syscalls per member on
            # the unbuffered file this reader is handed, per member
            # extracted from a many-file wheel.
            blob = self.file.read(
                30 + _LOCAL_HEADER_HEADROOM + compressed_size,
            )
            header = blob[:30]
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            _, _, _, _, _, _, _, _, _, name_size, extra_size = LOCAL_FILE_HEADER.unpack(
                header,
            )
            start = 30 + name_size + extra_size
            end = start + compressed_size
            if end <= len(blob):
                data = blob[start:end]
            else:
                # The name plus extra field exceeded the headroom (or the
                # blob was cut short) -- one exact positioned read instead.
                self.file.seek(local_offset + start)
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
        if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
            raise WheelhouseUnavailable
        return result

    def read_many(
        self,
        names: list[str],
        *,
        ordered_input: bool = False,
    ) -> list[bytes]:
        """Read members while returning the requested order."""
        members = [self.members[name] for name in names]
        in_archive_order = ordered_input or all(
            left[4] <= right[4] for left, right in zip(members, members[1:])
        )
        if in_archive_order:
            ordered_names = names
            ordered_members = members
        else:
            ordered = sorted(zip(members, names), key=lambda item: item[0][4])
            ordered_members = [member for member, _ in ordered]
            ordered_names = [name for _, name in ordered]
        ordered_results: list[bytes] = []
        unordered_results: dict[str, bytes] | None = None if in_archive_order else {}
        tail_source = io.BytesIO(self._tail) if self._tail else None
        position = -1
        for name, member in zip(ordered_names, ordered_members):
            compression, crc, compressed_size, uncompressed_size, local_offset = member
            if tail_source is not None and local_offset >= self._tail_start:
                source = tail_source
                source.seek(local_offset - self._tail_start)
            else:
                source = self.file
                if local_offset != position:
                    source.seek(local_offset)
            header = source.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            (_, _, _, _, _, _, _, _, _, name_size, extra_size) = (
                LOCAL_FILE_HEADER.unpack(header)
            )
            source.seek(name_size + extra_size, 1)
            data = source.read(compressed_size)
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
            if in_archive_order:
                ordered_results.append(result)
            else:
                assert unordered_results is not None
                unordered_results[name] = result
            if source is self.file:
                position = local_offset + 30 + name_size + extra_size + compressed_size
        if in_archive_order:
            return ordered_results
        assert unordered_results is not None
        return [unordered_results[name] for name in names]
