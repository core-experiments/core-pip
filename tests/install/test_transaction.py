import os
from pathlib import Path

import pytest
import cpip.install.transaction as transaction_module
from cpip.core.errors import InstallationError
from cpip.install.transaction import InstallTransaction


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
