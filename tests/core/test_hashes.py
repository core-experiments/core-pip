from __future__ import annotations

import hashlib

import pytest
from cpip.core.errors import HashMismatch, HashMissing, InstallationError
from cpip.core.hashes import Hashes, MissingHashes


def test_hashes_validate_path(tmp_path) -> None:
    content = b"cpip hash test"
    digest = hashlib.sha256(content).hexdigest()
    file_path = tmp_path / "archive.whl"
    file_path.write_bytes(content)

    Hashes({"sha256": [digest.upper()]}).check_against_path(str(file_path))
    with pytest.raises(HashMismatch):
        Hashes({"sha256": ["0" * 64]}).check_against_path(str(file_path))


def test_hashes_intersection() -> None:
    left = Hashes({"sha256": ["a", "b"]})
    right = Hashes({"sha256": ["b", "c"]})

    assert (left & right).is_hash_allowed("sha256", "b")
    assert not (left & right).is_hash_allowed("sha256", "a")


def test_missing_hashes_reports_sha256(tmp_path) -> None:
    file_path = tmp_path / "archive.whl"
    file_path.write_bytes(b"cpip hash test")

    with pytest.raises(HashMissing):
        MissingHashes().check_against_path(str(file_path))


def test_unknown_hash_algorithm_is_rejected() -> None:
    with pytest.raises(InstallationError, match="Unknown hash name"):
        Hashes({"not-a-hash": ["digest"]}).check_against_chunks([b"data"])
