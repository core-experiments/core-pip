"""Hash collections and validation used across cpip packages."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from cpip.core.errors import HashMissing, HashMismatch, InstallationError

Hash = Any


def file_hashes(path: str | Path) -> dict[str, str]:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        buffer = bytearray(max(1, min(size, 1024 * 1024)))
        view = memoryview(buffer)
        while read := stream.readinto(buffer):
            digest.update(view[:read])
    return {"sha256": digest.hexdigest()}


def read_chunks(file: BinaryIO, size: int = 1024 * 1024):
    while True:
        chunk = file.read(size)
        if not chunk:
            return
        yield chunk


def hash_file(path: str, blocksize: int = 1 << 20) -> tuple[Hash, int]:
    digest = hashlib.sha256()
    length = 0
    with open(path, "rb") as file:
        for chunk in read_chunks(file, blocksize):
            length += len(chunk)
            digest.update(chunk)
    return digest, length


# The recommended hash algorithm of the moment.
FAVORITE_HASH = "sha256"

# Algorithms allowed by the --hash option and ``cpip hash``.
STRONG_HASHES = ["sha256", "sha384", "sha512"]


class Hashes:
    """Build multiple hashes at once and check them against known values."""

    def __init__(self, hashes: dict[str, list[str]] | None = None) -> None:
        self.allowed_internal = {
            algorithm: [digest.lower() for digest in sorted(digests)]
            for algorithm, digests in (hashes or {}).items()
        }

    def __and__(self, other: Hashes) -> Hashes:
        if not other:
            return self
        if not self:
            return other
        return Hashes(
            {
                algorithm: [
                    digest
                    for digest in digests
                    if digest in self.allowed_internal.get(algorithm, [])
                ]
                for algorithm, digests in other.allowed_internal.items()
            }
        )

    @property
    def digest_count(self) -> int:
        return sum(len(digests) for digests in self.allowed_internal.values())

    @property
    def allowed_digests(self) -> frozenset[str]:
        return frozenset(
            digest for digests in self.allowed_internal.values() for digest in digests
        )

    def is_hash_allowed(self, hash_name: str, hex_digest: str) -> bool:
        return hex_digest.lower() in self.allowed_internal.get(hash_name, [])

    def check_against_chunks(self, chunks: Iterable[bytes]) -> None:
        gots = {}
        for hash_name in self.allowed_internal:
            try:
                gots[hash_name] = hashlib.new(hash_name)
            except (ValueError, TypeError) as exc:
                raise InstallationError(f"Unknown hash name: {hash_name}") from exc

        for chunk in chunks:
            for digest in gots.values():
                digest.update(chunk)

        for hash_name, digest in gots.items():
            if digest.hexdigest() in self.allowed_internal[hash_name]:
                return
        self.raise_internal(gots)

    def raise_internal(self, gots: dict[str, Hash]) -> NoReturn:
        raise HashMismatch(self.allowed_internal, gots)

    def check_against_file(self, file: BinaryIO) -> None:
        self.check_against_chunks(read_chunks(file))

    def check_against_path(self, path: str) -> None:
        with open(path, "rb") as file:
            self.check_against_file(file)

    def has_one_of(self, hashes: Mapping[str, str]) -> bool:
        return any(
            self.is_hash_allowed(hash_name, hex_digest)
            for hash_name, hex_digest in hashes.items()
        )

    def __bool__(self) -> bool:
        return bool(self.allowed_internal)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hashes):
            return NotImplemented
        return self.allowed_internal == other.allowed_internal

    def __hash__(self) -> int:
        return hash(
            ",".join(
                sorted(
                    ":".join((algorithm, digest))
                    for algorithm, digest_list in self.allowed_internal.items()
                    for digest in digest_list
                )
            )
        )


class MissingHashes(Hashes):
    """Hash checker that reports the computed favorite hash when missing."""

    def __init__(self) -> None:
        super().__init__(hashes={FAVORITE_HASH: []})

    def raise_internal(self, gots: dict[str, Hash]) -> NoReturn:
        raise HashMissing(gots[FAVORITE_HASH].hexdigest())
