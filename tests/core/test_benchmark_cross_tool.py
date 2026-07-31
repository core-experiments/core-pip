from __future__ import annotations

import sys
from pathlib import Path

import pytest

from benchmarks.cross_tool import (
    Benchmark,
    Tool,
    _command,
    _normalize_output,
)
from benchmarks.uv_scenarios import create_offline_scenarios


def test_normalize_pylock_and_requirements_outputs(tmp_path: Path) -> None:
    pylock = tmp_path / "pylock.toml"
    pylock.write_text(
        'lock-version = "1.0"\n\n[[packages]]\nname = "Demo_Pkg"\nversion = "1.2"\n',
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo-pkg==1.2\n# comment\n", encoding="utf-8")

    assert _normalize_output(pylock) == {("demo-pkg", "1.2")}
    assert _normalize_output(requirements) == {("demo-pkg", "1.2")}


def test_commands_use_distinct_state_and_matching_interpreters(tmp_path: Path) -> None:
    scenario = next(iter(create_offline_scenarios(tmp_path).values()))
    pip = _command(
        scenario,
        Tool.CORE_PIP,
        Benchmark.RESOLVE_COLD,
        tmp_path / "pip",
        uv_path="/opt/uv",
    )
    uv = _command(
        scenario,
        Tool.UV_PIP,
        Benchmark.RESOLVE_COLD,
        tmp_path / "uv",
        uv_path="/opt/uv",
    )

    assert pip.name == "core-pip"
    assert uv.name == "uv-pip"
    assert str(tmp_path / "pip") in pip.prepare
    assert str(tmp_path / "uv") in uv.prepare
    assert pip.command[0] == "env"
    assert pip.command[4:7] == [sys.executable, "-m", "pip"]
    assert uv.command[:2] == ["/opt/uv", "pip"]
    assert "--find-links" in pip.command
    assert "--find-links" in uv.command


@pytest.mark.parametrize(
    "benchmark",
    [
        Benchmark.STARTUP_VERSION,
        Benchmark.STARTUP_HELP,
        Benchmark.STARTUP_FAST_INSTALL,
        Benchmark.STARTUP_FALLBACK_INSTALL,
        Benchmark.STARTUP_FULL_FALLBACK_INSTALL,
    ],
)
def test_startup_commands_have_expected_shape(
    tmp_path: Path, benchmark: Benchmark
) -> None:
    scenario = next(iter(create_offline_scenarios(tmp_path).values()))
    pip = _command(
        scenario, Tool.CORE_PIP, benchmark, tmp_path / "pip", uv_path="/opt/uv"
    )
    uv = _command(
        scenario, Tool.UV_PIP, benchmark, tmp_path / "uv", uv_path="/opt/uv"
    )

    if benchmark is Benchmark.STARTUP_VERSION:
        assert pip.command[-1] == "--version"
        assert uv.command == ["/opt/uv", "--version"]
        assert pip.prepare == uv.prepare == "true"
    elif benchmark is Benchmark.STARTUP_HELP:
        assert pip.command[-1] == "--help"
        assert uv.command == ["/opt/uv", "pip", "--help"]
        assert pip.prepare == uv.prepare == "true"
    else:
        assert "install" in pip.command
        assert "install" in uv.command
        assert "target" in pip.prepare
        if benchmark is Benchmark.STARTUP_FAST_INSTALL:
            assert "--quiet" in pip.command
            assert "--quiet" in uv.command
        else:
            assert "--quiet" not in pip.command
            assert "--quiet" not in uv.command


@pytest.mark.parametrize(
    "benchmark, cache_fragment, output_fragment",
    [
        (Benchmark.RESOLVE_COLD, "cache", "pylock.toml"),
        (Benchmark.RESOLVE_WARM, "pylock.toml", "pylock.toml"),
        (Benchmark.INSTALL_COLD, "cache", "target"),
        (Benchmark.INSTALL_WARM, "target", "target"),
    ],
)
def test_cache_modes_have_explicit_prepare_commands(
    tmp_path: Path,
    benchmark: Benchmark,
    cache_fragment: str,
    output_fragment: str,
) -> None:
    scenario = next(iter(create_offline_scenarios(tmp_path).values()))
    command = _command(
        scenario,
        Tool.CORE_PIP,
        benchmark,
        tmp_path / "state",
        uv_path=None,
    )

    assert cache_fragment in command.prepare
    assert output_fragment in command.prepare
