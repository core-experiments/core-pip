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


def test_help_dash_help_prints_top_level_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``help --help`` asks for help about help, not about a command.

    This used to reach ``getattr(module, "")`` through the ``help`` command
    spec and die with an AttributeError traceback.
    """
    assert main(["help", "--help"]) == 0

    output = capsys.readouterr().out
    assert "Usage:" in output
    assert "install" in output


def test_help_help_reports_unknown_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["help", "help"]) == 1
    assert "Unknown command: help" in capsys.readouterr().err
