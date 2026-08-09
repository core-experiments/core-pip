from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from cpip.cli.config import SourceConfig, load_source_config, resolve_sources


@pytest.fixture(autouse=True)
def clean_source_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CPIP_FIND_LINKS",
        "CPIP_INDEX_URL",
        "CPIP_EXTRA_INDEX_URL",
        "CPIP_NO_INDEX",
        "CPIP_CONFIG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cpip.conf"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("CPIP_CONFIG_FILE", str(path))


def test_blank_find_links_is_no_find_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only value configures nothing.

    The install fast path used to test the raw config string for truthiness,
    so "   " read as "a wheelhouse is configured" and made it decline. Both
    readers now agree that it configures no find-links.
    """
    write_config(tmp_path, "[global]\nfind-links =    \n", monkeypatch)

    assert load_source_config("install").find_links == []


def test_command_section_overrides_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(
        tmp_path,
        "[global]\nindex-url = https://global.example/simple\n"
        "[install]\nindex-url = https://install.example/simple\n",
        monkeypatch,
    )

    assert load_source_config("install").index_url == "https://install.example/simple"
    assert load_source_config("list").index_url == "https://global.example/simple"


def test_environment_overrides_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, "[global]\nno-index = false\n", monkeypatch)
    monkeypatch.setenv("CPIP_NO_INDEX", "yes")
    monkeypatch.setenv("CPIP_FIND_LINKS", "/one /two")

    config = load_source_config("install")

    assert config.no_index is True
    assert config.find_links == ["/one", "/two"]


def test_resolve_sources_prefers_command_line() -> None:
    config = SourceConfig(
        ["/configured"],
        "https://configured.example/simple",
        ["https://configured.example/extra"],
        False,
    )
    options = argparse.Namespace(
        find_links=["/given"],
        index_url="https://given.example/simple",
        extra_index_url=[],
        no_index=True,
    )

    resolved = resolve_sources(options, config)

    assert resolved.find_links == ["/given"]
    assert resolved.index_url == "https://given.example/simple"
    assert resolved.extra_index_urls == ["https://configured.example/extra"]
    assert resolved.no_index is True


def test_resolve_sources_without_find_links_option() -> None:
    """``cpip index`` has no --find-links; the configured value survives."""
    config = SourceConfig(["/configured"], None, [], False)
    options = argparse.Namespace(index_url=None, extra_index_url=[], no_index=False)

    assert resolve_sources(options, config).find_links == ["/configured"]
