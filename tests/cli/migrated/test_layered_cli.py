from cpip.cli.main import main


def test_layered_cli_help(capsys) -> None:
    assert main(["--help"]) == 0

    output = capsys.readouterr().out
    assert "Usage:" in output
    assert "install" in output


def test_layered_cli_version(capsys) -> None:
    assert main(["--version"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("cpip ")
