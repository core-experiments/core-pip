"""Content-addressed storage for downloaded distribution artifacts."""

from __future__ import annotations

import hashlib
import marshal
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping

from cpip.core.errors import HashMismatch
from cpip.core.utils import CACHE_VERSION, CACHE_VERSION_TAG

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Protocol

    class HashDigest(Protocol):
        def update(self, data: bytes, /) -> None: ...

        def hexdigest(self) -> str: ...


ARTIFACT_CACHE_BUCKET = f"artifacts-{CACHE_VERSION_TAG}"
ARTIFACT_CACHE_FORMAT = CACHE_VERSION


class CachedArtifact:
    __slots__ = ("digest", "path", "size")

    def __init__(self, path: str, digest: str, size: int) -> None:
        self.path = path
        self.digest = digest
        self.size = size


class ArtifactCache:
    """Store immutable bodies by digest and index them by normalized URL."""

    __slots__ = ("root",)

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.root = os.path.join(os.fspath(cache_dir), ARTIFACT_CACHE_BUCKET)

    @staticmethod
    def _url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _receipt_path(self, url: str) -> str:
        key = self._url_key(url)
        return os.path.join(self.root, "links", key[:2], f"{key}.bin")

    def _body_path(self, digest: str) -> str:
        return os.path.join(self.root, "sha256", digest[:2], digest, "body")

    def _body(self, digest: str, size: int | None = None) -> CachedArtifact | None:
        if len(digest) != 64:
            return None
        path = self._body_path(digest)
        try:
            actual_size = os.stat(path, follow_symlinks=False).st_size
        except OSError:
            return None
        if size is not None and actual_size != size:
            return None
        return CachedArtifact(path, digest, actual_size)

    def get(
        self,
        url: str,
        expected_hashes: Mapping[str, str] | None = None,
    ) -> CachedArtifact | None:
        expected_sha256 = (expected_hashes or {}).get("sha256")
        if expected_sha256 is not None:
            cached = self._body(expected_sha256.lower())
            if cached is not None:
                return cached

        receipt = self._receipt_path(url)
        try:
            with open(receipt, "rb") as file:
                value = marshal.load(file)
        except (EOFError, OSError, TypeError, ValueError):
            return None
        if not (
            isinstance(value, tuple)
            and len(value) == 5
            and value[0] == ARTIFACT_CACHE_FORMAT
            and value[1] == url
            and isinstance(value[2], str)
            and isinstance(value[3], int)
            and isinstance(value[4], str)
        ):
            return None
        digest = value[2]
        if expected_sha256 is not None and digest != expected_sha256.lower():
            return None
        return self._body(digest, value[3])

    @staticmethod
    def _digests(
        expected_hashes: Mapping[str, str] | None,
    ) -> dict[str, HashDigest]:
        result: dict[str, HashDigest] = {"sha256": hashlib.sha256()}
        for algorithm in expected_hashes or ():
            if algorithm in result:
                continue
            try:
                result[algorithm] = hashlib.new(algorithm)
            except (TypeError, ValueError):
                # Unsupported hashes retain the resolver's existing validation
                # behavior. The artifact store always provides SHA-256.
                continue
        return result

    @staticmethod
    def _validate_hashes(
        url: str,
        digests: Mapping[str, HashDigest],
        expected_hashes: Mapping[str, str] | None,
    ) -> None:
        for algorithm, expected in (expected_hashes or {}).items():
            digest = digests.get(algorithm)
            if digest is None:
                continue
            actual = digest.hexdigest()
            if actual.lower() == expected.lower():
                continue
            raise HashMismatch(
                "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE INDEX.\n"
                f"    {url}:\n"
                f"        Expected {algorithm} {expected}\n"
                f"             Got        {actual}",
            )

    def _publish_receipt(
        self,
        url: str,
        artifact: CachedArtifact,
        filename: str,
    ) -> None:
        path = self._receipt_path(url)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as file:
                marshal.dump(
                    (
                        ARTIFACT_CACHE_FORMAT,
                        url,
                        artifact.digest,
                        artifact.size,
                        filename,
                    ),
                    file,
                )
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _publish_body(
        self,
        temporary: str,
        digest: str,
        size: int,
    ) -> CachedArtifact:
        path = self._body_path(digest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            os.unlink(temporary)
        else:
            try:
                os.chmod(temporary, 0o444)
                os.replace(temporary, path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        artifact = self._body(digest, size)
        if artifact is None:
            raise OSError(f"failed to publish artifact cache body: {path}")
        return artifact

    def store_chunks(
        self,
        url: str,
        filename: str,
        chunks: Iterable[bytes],
        expected_hashes: Mapping[str, str] | None = None,
    ) -> CachedArtifact:
        staging = os.path.join(self.root, ".tmp")
        os.makedirs(staging, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=staging)
        digests = self._digests(expected_hashes)
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as file:
                for chunk in chunks:
                    if not chunk:
                        continue
                    file.write(chunk)
                    size += len(chunk)
                    for digest in digests.values():
                        digest.update(chunk)
            self._validate_hashes(url, digests, expected_hashes)
            sha256 = digests["sha256"].hexdigest()
            artifact = self._publish_body(temporary, sha256, size)
            temporary = ""
            self._publish_receipt(url, artifact, filename)
            return artifact
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def store_path(
        self,
        url: str,
        filename: str,
        source: str,
        expected_hashes: Mapping[str, str] | None = None,
    ) -> CachedArtifact:
        with open(source, "rb") as file:
            return self.store_chunks(
                url,
                filename,
                iter(lambda: file.read(1024 * 1024), b""),
                expected_hashes,
            )


def materialize_cached_artifact(source: str, destination: str) -> None:
    """Expose an immutable cache body under its original artifact filename."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        os.link(source, destination)
        return
    except FileExistsError:
        return
    except OSError:
        pass
    temporary = f"{destination}.{os.getpid()}.tmp"
    try:
        shutil.copyfile(source, temporary)
        try:
            os.chmod(temporary, 0o444)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
