from __future__ import annotations

import pytest

from cpip.cli.main import main


@pytest.mark.parametrize(
    "command, expected",
    [
        ("install", "--target"),
        ("wheel", "--wheel-dir"),
        ("index", "--json"),
        ("download", "--dest"),
        ("uninstall", "--yes"),
        ("list", "--outdated"),
        ("freeze", "--exclude-editable"),
        ("show", "--files"),
        ("inspect", "--local"),
        ("hash", "--algorithm"),
        ("check", "usage: cpip check"),
        ("cache", "--cache-dir"),
        ("lock", "--output"),
    ],
)
def test_command_help_uses_registered_parser(
    command: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["help", command]) == 0
    assert expected in capsys.readouterr().out
