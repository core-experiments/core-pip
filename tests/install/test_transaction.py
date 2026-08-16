import os
from pathlib import Path

import cpip.install.transaction as transaction_module
import pytest
from cpip.core.errors import InstallationError
from cpip.install.transaction import InstallTransaction
from cpip.install.wheel_state import discover_installed_wheels


def test_lightweight_installed_wheel_inventory_reads_dist_info(tmp_path: Path) -> None:
    metadata = tmp_path / "Demo_Pkg-1.2.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: Demo_Pkg\nVersion: 1.2\n\n",
        encoding="utf-8",
    )
    (metadata / "RECORD").write_text("demo.py,,\n", encoding="utf-8")

    installed = discover_installed_wheels(
        (str(tmp_path),),
        names={"demo-pkg"},
    )

    assert installed is not None
    distribution = installed["demo-pkg"]
    assert distribution.version == "1.2"
    assert distribution.read_text("RECORD") == "demo.py,,\n"


@pytest.mark.parametrize("metadata_name", ["demo.egg-info", "demo.egg-link"])
def test_lightweight_installed_wheel_inventory_defers_legacy_metadata(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    path = tmp_path / metadata_name
    path.mkdir() if metadata_name.endswith(".egg-info") else path.touch()

    assert discover_installed_wheels((str(tmp_path),), names={"demo"}) is None


def test_lightweight_installed_wheel_inventory_defers_malformed_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "demo-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: demo\n", encoding="utf-8")

    assert discover_installed_wheels((str(tmp_path),), names={"demo"}) is None


def test_lightweight_installed_wheel_inventory_skips_unrelated_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "demo-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Name: demo\nVersion: 1.0\n\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.egg-link").touch()
    (tmp_path / "broken-1.0.dist-info").mkdir()

    installed = discover_installed_wheels((str(tmp_path),), names={"demo"})

    assert installed is not None
    assert set(installed) == {"demo"}


def test_transaction_commits_and_replaces_owned_file(tmp_path: Path) -> None:
    destination = tmp_path / "site" / "demo.py"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    with InstallTransaction(owned_paths=[destination]) as transaction:
        transaction.add(source, destination)
        transaction.commit()

    assert destination.read_text(encoding="utf-8") == "new"


def test_transaction_commits_staged_contents(tmp_path: Path) -> None:
    destination = tmp_path / "site" / "demo.py"

    with InstallTransaction() as transaction:
        transaction.add_contents(destination, b"new")
        transaction.commit()

    assert destination.read_bytes() == b"new"


def test_transaction_clones_without_consuming_cache_source(tmp_path: Path) -> None:
    source = tmp_path / "cache" / "source.txt"
    source.parent.mkdir()
    source.write_text("immutable")
    destination = tmp_path / "target" / "destination.txt"

    with InstallTransaction() as transaction:
        transaction.add_clone(str(source), str(destination))
        transaction.commit()

    assert source.read_text() == "immutable"
    assert destination.read_text() == "immutable"
    destination.write_text("changed")
    assert source.read_text() == "immutable"


def test_transaction_rejects_unowned_collision(tmp_path: Path) -> None:
    destination = tmp_path / "demo.py"
    destination.write_text("unrelated", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    transaction = InstallTransaction()
    transaction.add(source, destination)
    with pytest.raises(InstallationError, match="unrelated file"):
        transaction.commit()

    assert destination.read_text(encoding="utf-8") == "unrelated"


def test_transaction_validation_does_not_recheck_destination_file_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "demo.py"
    destination.write_text("unrelated", encoding="utf-8")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")
    original_isfile = transaction_module.os.path.isfile
    checked: list[str] = []

    def counting_isfile(path: str | os.PathLike[str]) -> bool:
        checked.append(os.fspath(path))
        return original_isfile(path)

    monkeypatch.setattr(transaction_module.os.path, "isfile", counting_isfile)
    transaction = InstallTransaction()
    transaction.add(source, destination)

    with pytest.raises(InstallationError, match="unrelated file"):
        transaction.commit()

    assert os.fspath(source) in checked
    assert os.fspath(destination) not in checked


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_transaction_replaces_broken_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "demo.py"
    destination.symlink_to(tmp_path / "missing.py")
    source = tmp_path / "stage.py"
    source.write_text("new", encoding="utf-8")

    transaction = InstallTransaction()
    transaction.add(source, destination)
    transaction.commit()

    assert destination.read_text(encoding="utf-8") == "new"


def test_transaction_rejects_duplicate_destination(tmp_path: Path) -> None:
    transaction = InstallTransaction()
    destination = tmp_path / "demo.py"

    transaction.add(tmp_path / "first.py", destination)
    with pytest.raises(InstallationError, match="duplicate installation destination"):
        transaction.add(tmp_path / "second.py", destination)


def test_transaction_rolls_back_previous_changes_on_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    first.write_text("old", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("new", encoding="utf-8")

    transaction = InstallTransaction(owned_paths=[first])
    transaction.add(source, first)
    transaction.add(tmp_path / "missing.py", tmp_path / "second.py")

    with pytest.raises(InstallationError, match="staged file"):
        transaction.commit()

    assert first.read_text(encoding="utf-8") == "old"


def test_transaction_rolls_back_staged_contents_on_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    first.write_text("old", encoding="utf-8")

    transaction = InstallTransaction(owned_paths=[first])
    transaction.add_contents(first, b"new")
    transaction.add(tmp_path / "missing.py", tmp_path / "second.py")

    with pytest.raises(InstallationError, match="staged file"):
        transaction.commit()

    assert first.read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_staged_contents_mode_exact_under_permissive_umask(
    tmp_path: Path,
    mode: int,
) -> None:
    """With umask 022 neither 0o644 nor 0o755 loses any bits at creation,
    so commit's chmod-skip path is the one that must still produce the
    exact requested mode on disk.
    """
    destination = tmp_path / "demo.py"
    old_umask = os.umask(0o022)
    try:
        with InstallTransaction() as transaction:
            transaction.add_contents(str(destination), b"payload", mode=mode)
            transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == mode


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_staged_contents_mode_exact_under_stripping_umask(tmp_path: Path) -> None:
    """umask 077 strips group/other bits at creation, so the follow-up
    chmod must run to restore the full requested mode.
    """
    destination = tmp_path / "demo.sh"
    old_umask = os.umask(0o077)
    try:
        with InstallTransaction() as transaction:
            transaction.add_contents(str(destination), b"#!/bin/sh\n", mode=0o755)
            transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="os.chmod ignores mode bits on Windows")
def test_staged_contents_default_mode_matches_open_builtin(tmp_path: Path) -> None:
    """mode=None must produce the same permissions the old open(path, "wb")
    write produced: 0o666 masked by the umask.
    """
    destination = tmp_path / "demo.txt"
    old_umask = os.umask(0o022)
    try:
        with InstallTransaction() as transaction:
            transaction.add_contents(str(destination), b"payload")
            transaction.commit()
    finally:
        os.umask(old_umask)

    assert stat_mode(destination) == 0o666 & ~0o022


def test_staged_contents_short_os_write_calls_are_all_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = os.write

    def short_write(fd: int, data) -> int:  # noqa: ANN001
        return original_write(fd, bytes(data)[:3])

    monkeypatch.setattr(transaction_module.os, "write", short_write)
    destination = tmp_path / "demo.bin"
    payload = bytes(range(256)) * 40

    with InstallTransaction() as transaction:
        transaction.add_contents(str(destination), payload)
        transaction.commit()

    assert destination.read_bytes() == payload


def test_staged_contents_zero_write_raises_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def zero_write(fd: int, data) -> int:  # noqa: ANN001
        return 0

    monkeypatch.setattr(transaction_module.os, "write", zero_write)
    destination = tmp_path / "demo.bin"

    transaction = InstallTransaction()
    transaction.add_contents(str(destination), b"payload")
    with pytest.raises(OSError, match="could not write staged file contents"):
        transaction.commit()

    assert not destination.exists()


def test_validate_live_symlink_to_identical_file_is_tolerated(tmp_path: Path) -> None:
    """A destination that is a live symlink to a byte-identical file must
    keep passing validation (the follow-the-link stat path), exactly as
    the old always-follow os.stat call behaved.
    """
    real = tmp_path / "real.py"
    real.write_bytes(b"same contents")
    destination = tmp_path / "demo.py"
    destination.symlink_to(real)

    transaction = InstallTransaction()
    transaction.add_contents(str(destination), b"same contents")
    transaction.validate()


def test_validate_live_symlink_to_different_file_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.py"
    real.write_bytes(b"other contents")
    destination = tmp_path / "demo.py"
    destination.symlink_to(real)

    transaction = InstallTransaction()
    transaction.add_contents(str(destination), b"new contents")
    with pytest.raises(InstallationError, match="an unrelated file already exists"):
        transaction.validate()


def test_validate_fresh_destination_uses_a_single_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install's absent destination must be answered by one lstat,
    not the old stat-then-lexists pair.
    """
    calls = {"lstat": 0, "stat": 0}
    original_lstat = os.lstat
    original_stat = os.stat

    def counting_lstat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["lstat"] += 1
        return original_lstat(path, *args, **kwargs)

    def counting_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls["stat"] += 1
        return original_stat(path, *args, **kwargs)

    destination = str(tmp_path / "absent.py")
    transaction = InstallTransaction()
    transaction.add_contents(str(destination), b"payload")
    monkeypatch.setattr(transaction_module.os, "lstat", counting_lstat)
    monkeypatch.setattr(transaction_module.os, "stat", counting_stat)
    transaction.validate()
    monkeypatch.undo()

    assert calls == {"lstat": 1, "stat": 0}


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
