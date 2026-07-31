from __future__ import annotations

import os

from cpip.cli.main import main


def test_main_reports_malformed_python_option(capsys) -> None:
    assert main(["--python"]) == 1
    assert "ERROR: --python requires a path" in capsys.readouterr().err


def test_main_restores_managed_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CPIP_RESOLVER_DEBUG", raising=False)
    monkeypatch.delenv("CPIP_TARGET_PREFIX", raising=False)

    assert main(["-vv", "--python", "/tmp/example-prefix", "--help"]) == 0
    capsys.readouterr()

    assert "CPIP_RESOLVER_DEBUG" not in os.environ
    assert "CPIP_TARGET_PREFIX" not in os.environ
