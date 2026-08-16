"""A minimal, single-pass tar reader for the common sdist shape.

Extracting an sdist calls ``tarfile.getmembers()``, which builds a full
``TarInfo`` for every entry -- decoding uid/gid/uname/gname/devmajor/
devminor no extraction path needs, and computing two independent checksums
(``tarfile.calc_chksums``) via a 504-value ``struct.unpack_from`` per header
-- before a single byte gets extracted. Flamegraph on the many-small-files
sdist benchmark showed that header-parsing machinery (``TarInfo.frombuf``
and friends) as the largest single cost, ahead of the actual file writes.

``fast_untar`` reads and extracts a gzip-compressed or uncompressed tar
archive in one pass, parsing only the header fields the common case needs
(name, typeflag, mode, size, mtime) and validating the same checksum
tarfile does. It only trusts what it reads for the shape every real sdist
takes in practice: plain regular files and directories in ustar/GNU
headers, nothing else. Anything outside that -- a GNU longname/longlink
extension, a PAX extended header, a sparse file, a symlink/hardlink/device,
a bad checksum, a truncated archive, bz2/xz compression, or any other
surprise -- aborts the attempt, deletes whatever this pass already wrote
(the caller only invokes this against a verified-empty destination, so
that's always safe), and returns ``None`` so the caller falls through to
the existing, fully general ``tarfile``-based path unchanged. A ``None``
return only ever costs the speedup, never correctness.

Every member is written under its verbatim archive name -- this module
has no notion of the leading-directory-stripping convention sdist
extraction applies (that's ``unpacking.py``'s ``has_leading_dir``/
``split_leading_dir``, which need the full name list to decide, and stay
there); ``fast_untar`` returns that name list on success precisely so its
caller can apply that decision as a cheap directory move afterward,
without this module needing to know about it at all.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys

from cpip.core.errors import InstallationError

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import IO

    _ReadableStream = IO[bytes] | gzip.GzipFile

BLOCKSIZE = 512

# Matches shutil's own default copy buffer size (not exposed in typeshed).
_COPY_BUFSIZE = 1024 * 1024 if sys.platform == "win32" else 64 * 1024

_REGTYPE = b"0"
_AREGTYPE = b"\x00"
_DIRTYPE = b"5"
_SUPPORTED_TYPES = frozenset((_REGTYPE, _AREGTYPE, _DIRTYPE))

_FAST_MODES = frozenset(("r:gz", "r"))


class _NotFastCompatible(Exception):
    """Internal signal: this archive is outside the fast path's coverage."""


def _nti(field: bytes) -> int:
    # Mirrors tarfile.nti(): a zero/space-padded octal ASCII field, or --
    # when the first byte has its high bit set -- a GNU binary big-endian
    # encoding used for values too large for the octal field (e.g. a
    # multi-gigabyte member's size).
    first = field[0]

    if first in (0o200, 0o377):
        n = int.from_bytes(field[1:], "big")

        if first == 0o377:
            n -= 256 ** (len(field) - 1)

        return n

    text = field.split(b"\x00", 1)[0].strip()

    if not text:
        return 0

    try:
        return int(text, 8)

    except ValueError:
        raise _NotFastCompatible("invalid numeric header field") from None


def _nts(field: bytes) -> str:
    # Mirrors tarfile.nts() with this codebase's own tarfile.open(...,
    # encoding="utf-8") call -- errors="surrogateescape" is tarfile's
    # default and is never overridden there, so this never raises.
    return field.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")


def _checksum_ok(buf: bytes, expected: int) -> bool:
    # Mirrors tarfile.calc_chksums(): sum every byte except the 8-byte
    # chksum field itself (treated as if filled with ASCII spaces), in both
    # unsigned and signed interpretations -- some tar writers (Sun, NeXT)
    # compute it with signed chars. bytes iterates as unsigned ints, so the
    # signed sum subtracts 256 from any byte with its high bit set.
    unsigned = 256 + sum(buf[:148]) + sum(buf[156:BLOCKSIZE])

    if unsigned == expected:
        return True

    signed = (
        256
        + sum(b - 256 if b > 127 else b for b in buf[:148])
        + sum(b - 256 if b > 127 else b for b in buf[156:BLOCKSIZE])
    )

    return signed == expected


def _parse_header(buf: bytes) -> tuple[str, bytes, int, int, int] | None:
    """Parse one 512-byte header block.

    Returns ``(name, typeflag, mode, size, mtime)``, or ``None`` for the
    all-zero end-of-archive marker. Raises ``_NotFastCompatible`` for
    anything this reader doesn't cover.
    """

    if buf.count(b"\x00") == BLOCKSIZE:
        return None

    expected_chksum = _nti(buf[148:156])

    if not _checksum_ok(buf, expected_chksum):
        raise _NotFastCompatible("bad checksum")

    typeflag = buf[156:157]

    if typeflag not in _SUPPORTED_TYPES:
        raise _NotFastCompatible(f"unsupported typeflag {typeflag!r}")

    name = _nts(buf[0:100])

    mode = _nti(buf[100:108])

    size = _nti(buf[124:136])

    mtime = _nti(buf[136:148])

    prefix = _nts(buf[345:500])

    # Old V7 tar represents a directory as a regular file with a trailing
    # slash.
    if typeflag == _AREGTYPE and name.endswith("/"):
        typeflag = _DIRTYPE

    if typeflag == _DIRTYPE:
        name = name.rstrip("/")

        if size:
            # A directory shouldn't carry a data payload. The extraction
            # loop doesn't consume one for _DIRTYPE, so a crafted header
            # claiming one here would otherwise desync the stream and parse
            # that payload as the next header.
            raise _NotFastCompatible("directory member has data")

    # Reconstruct a ustar longname.
    if prefix:
        name = f"{prefix}/{name}"

    if not name or size < 0 or "\\" in name:
        # A backslash in the name is either a literal POSIX filename
        # character or (per split_leading_dir()) a directory separator on
        # Windows-authored archives -- ambiguous in a way this reader can't
        # resolve without unpacking.py's own leading-dir-stripping logic,
        # so it declines rather than risk writing it as a literal filename
        # when the caller would have treated it as a path.
        raise _NotFastCompatible("malformed header field")

    return name, typeflag, mode, size, mtime


def _read_exact(stream: _ReadableStream, size: int) -> bytes | None:
    """Read exactly `size` bytes, or None if the stream ends first."""

    if size == 0:
        return b""

    chunks = []

    remaining = size

    read = stream.read

    while remaining > 0:
        chunk = read(remaining)

        if not chunk:
            return None

        chunks.append(chunk)

        remaining -= len(chunk)

    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def _extract_exact(stream: _ReadableStream, path: str, size: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)

    try:
        remaining = size

        read = stream.read

        while remaining > 0:
            chunk = read(min(_COPY_BUFSIZE, remaining))

            if not chunk:
                raise _NotFastCompatible("truncated member data")

            view = memoryview(chunk)

            while view:
                written = os.write(fd, view)

                if written <= 0:
                    raise OSError("could not write extracted member data")

                view = view[written:]

            remaining -= len(chunk)

    finally:
        os.close(fd)


def _open_stream(filename: str, mode: str) -> _ReadableStream:
    if mode == "r:gz":
        return gzip.open(filename, "rb")

    return open(filename, "rb")  # noqa: SIM115


def _discard_destination_contents(location: str) -> None:
    # The caller only reaches here after verifying `location` was empty, so
    # anything inside it now was written by this attempt -- safe to nuke
    # outright rather than track individual paths to undo.
    try:
        with os.scandir(location) as entries:
            children = list(entries)

    except OSError:
        return

    for entry in children:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)

            else:
                os.remove(entry.path)

        except OSError:
            pass


def fast_untar(filename: str, location: str, mode: str) -> list[str] | None:
    """Try a fast single-pass extraction; None means "use tarfile instead".

    Extracts every member under its verbatim archive name (no leading-
    directory stripping -- that's the caller's business, using the
    returned name list against its own `has_leading_dir`/
    `split_leading_dir`). On success, returns the extracted names in
    archive order.

    `location` must already exist. On any decline this only ever discards
    files this call itself wrote, so it verifies `location` is empty
    up front rather than trusting the caller to have checked -- a
    non-empty destination declines immediately, before opening anything.
    """

    if mode not in _FAST_MODES:
        return None

    try:
        with os.scandir(location) as entries:
            if next(entries, None) is not None:
                return None

    except OSError:
        return None

    try:
        stream = _open_stream(filename, mode)

    except OSError:
        return None

    absolute_location = os.path.abspath(location)

    created_directories = {absolute_location}

    extracted_names: list[str] = []

    umask_mask = os.umask(0)

    os.umask(umask_mask)

    executable_mode = 0o777 & ~umask_mask | 0o111

    try:
        while True:
            header_buf = _read_exact(stream, BLOCKSIZE)

            if header_buf is None:
                raise _NotFastCompatible("missing end-of-archive marker")

            parsed = _parse_header(header_buf)

            if parsed is None:
                break

            name, typeflag, member_mode, size, mtime = parsed

            path = os.path.join(location, name)

            absolute_path = os.path.abspath(path)

            if not (
                absolute_path == absolute_location
                or absolute_path.startswith(absolute_location + os.sep)
            ):
                raise InstallationError(
                    f"{name!r} is outside the destination in {filename}",
                )

            extracted_names.append(name)

            if typeflag == _DIRTYPE:
                if path and path not in created_directories:
                    os.makedirs(path, exist_ok=True)

                    created_directories.add(path)

                continue

            parent = os.path.dirname(path)

            if parent not in created_directories:
                os.makedirs(parent, exist_ok=True)

                created_directories.add(parent)

            _extract_exact(stream, path, size)

            padding = (-size) % BLOCKSIZE

            if padding and _read_exact(stream, padding) is None:
                raise _NotFastCompatible("truncated archive")

            os.utime(path, (mtime, mtime))

            if member_mode & 0o111:
                os.chmod(path, executable_mode)

    except _NotFastCompatible:
        stream.close()

        _discard_destination_contents(location)

        return None

    except BaseException:
        stream.close()

        _discard_destination_contents(location)

        raise

    stream.close()

    return extracted_names
