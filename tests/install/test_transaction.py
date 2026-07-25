from pathlib import Path

import pytest
from pip.core.errors import InstallationError
from pip.install.transaction import InstallTransaction


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
