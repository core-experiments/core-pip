from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO

import pytest
from cpip.cli._help import COMMAND_HELP_TEXT
from cpip.cli.commands.registry import parser_for_command
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


@pytest.mark.parametrize("command", tuple(COMMAND_HELP_TEXT))
def test_pregenerated_command_help_matches_parser(command: str) -> None:
    output = StringIO()
    with pytest.raises(SystemExit) as exc_info, redirect_stdout(output):
        parser_for_command(command).parse_args(["--help"])

    assert exc_info.value.code == 0
    assert output.getvalue() == COMMAND_HELP_TEXT[command]
