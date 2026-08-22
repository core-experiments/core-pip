from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cpip.core.errors import HashMismatch
from cpip.index.artifacts import ArtifactLocator
from cpip.index.artifact_cache import ARTIFACT_CACHE_BUCKET
from cpip.network.cache import SafeFileCache
from cpip.core.appdirs import http_cache_path


class FakeResponse:
    status_code = 200
    reason = "OK"

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        del chunk_size
        return [self.body]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, cache: SafeFileCache, body: bytes) -> None:
        self.cache = cache
        self.body = body
        self.requests = 0

    def get(self, url: str, *, stream: bool) -> FakeResponse:
        assert url.endswith("demo-1.0-py3-none-any.whl")
        assert stream is True
        self.requests += 1
        return FakeResponse(self.body)


def test_artifacts_are_reused_from_http_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cpip.index import artifacts

    monkeypatch.setattr(artifacts, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    cache = SafeFileCache(str(tmp_path / "cache"))
    url = "https://example.test/packages/demo-1.0-py3-none-any.whl#sha256=abc"
    first_session = FakeSession(cache, b"wheel payload")

    first = ArtifactLocator(first_session).ensure_local(url)
    assert Path(first).read_bytes() == b"wheel payload"
    assert first_session.requests == 1

    second_session = FakeSession(cache, b"unexpected network payload")
    second = ArtifactLocator(second_session).ensure_local(url)
    assert Path(second).read_bytes() == b"wheel payload"
    assert second_session.requests == 0


def test_artifacts_are_stored_once_by_sha256(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cpip.index import artifacts

    monkeypatch.setattr(artifacts, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    body = b"content addressed wheel"
    digest = hashlib.sha256(body).hexdigest()
    cache_root = tmp_path / "cache"
    http_cache = SafeFileCache(http_cache_path(str(cache_root)))
    url = f"https://example.test/demo-1.0-py3-none-any.whl#sha256={digest}"

    first_session = FakeSession(http_cache, body)
    first = ArtifactLocator(first_session, cache_root).ensure_local_text(
        url,
        hashes={"sha256": digest},
    )

    cache_body = (
        cache_root / ARTIFACT_CACHE_BUCKET / "sha256" / digest[:2] / digest / "body"
    )
    assert Path(first).read_bytes() == body
    assert cache_body.read_bytes() == body
    assert first_session.requests == 1
    assert http_cache.get_body_path(f"artifact:{url.split('#', 1)[0]}") is None

    Path(first).unlink()
    second_session = FakeSession(http_cache, b"unexpected")
    second = ArtifactLocator(second_session, cache_root).ensure_local_text(
        url,
        hashes={"sha256": digest},
    )
    assert Path(second).read_bytes() == body
    assert second_session.requests == 0


def test_unhashed_artifact_uses_url_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cpip.index import artifacts

    monkeypatch.setattr(artifacts, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    cache_root = tmp_path / "cache"
    http_cache = SafeFileCache(http_cache_path(str(cache_root)))
    url = "https://example.test/demo-1.0-py3-none-any.whl"
    body = b"wheel without an index hash"

    first_session = FakeSession(http_cache, body)
    first = ArtifactLocator(first_session, cache_root).ensure_local(url)
    digest = hashlib.sha256(body).hexdigest()
    assert Path(first).read_bytes() == body
    assert (
        cache_root / ARTIFACT_CACHE_BUCKET / "sha256" / digest[:2] / digest / "body"
    ).is_file()

    Path(first).unlink()
    second_session = FakeSession(http_cache, b"unexpected")
    second = ArtifactLocator(second_session, cache_root).ensure_local(url)
    assert Path(second).read_bytes() == body
    assert second_session.requests == 0


def test_hash_mismatch_is_not_published(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cpip.index import artifacts

    monkeypatch.setattr(artifacts, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    cache_root = tmp_path / "cache"
    session = FakeSession(
        SafeFileCache(http_cache_path(str(cache_root))),
        b"wrong body",
    )
    url = "https://example.test/demo-1.0-py3-none-any.whl"

    with pytest.raises(HashMismatch, match="Expected sha256"):
        ArtifactLocator(session, cache_root).ensure_local_text(
            url,
            hashes={"sha256": "0" * 64},
        )

    sha_root = cache_root / ARTIFACT_CACHE_BUCKET / "sha256"
    assert not sha_root.exists() or list(sha_root.rglob("body")) == []


def test_artifact_cache_write_failure_falls_back_to_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from cpip.index import artifacts
    from cpip.index.artifact_cache import ArtifactCache

    monkeypatch.setattr(artifacts, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(
        ArtifactCache,
        "store_chunks",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only cache")),
    )
    cache_root = tmp_path / "cache"
    body = b"uncached fallback"
    session = FakeSession(SafeFileCache(http_cache_path(str(cache_root))), body)

    path = ArtifactLocator(session, cache_root).ensure_local(
        "https://example.test/demo-1.0-py3-none-any.whl",
    )

    assert Path(path).read_bytes() == body
    assert session.requests == 2
