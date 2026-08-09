"""``cpip download`` must actually reach the caches it advertises.

``--no-cache-dir`` was accepted and then never read: ``run_download`` passed no
cache directory to anything, so downloads never cached and the flag happened to
describe the behavior by accident. Now that the caches are on by default the
flag is load-bearing, so these tests pin the wiring rather than the outcome --
where the directory ends up is the part that silently regresses.
"""

from __future__ import annotations

from typing import Any

import pytest
from cpip.cli import download as download_module


class Stop(Exception):
    """Abort ``run_download`` once the wiring under test has been observed."""


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what ``run_download`` hands its collaborators, then bail out."""
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        download_module,
        "load_source_config",
        lambda _name: None,
    )
    monkeypatch.setattr(
        download_module,
        "resolve_sources",
        lambda options, _config: type(
            "Sources",
            (),
            {
                "find_links": [],
                "index_url": None,
                "extra_index_urls": [],
                "no_index": True,
            },
        )(),
    )
    monkeypatch.setattr(download_module, "apply_proxy_environment", lambda _proxy: None)
    monkeypatch.setattr(
        download_module,
        "parse_dependency_groups",
        lambda _groups: [],
    )

    def fake_collect(**kwargs: Any) -> Any:
        seen["collect_cache_dir"] = kwargs.get("cache_dir")
        raise Stop

    monkeypatch.setattr(download_module, "collect_requirements", fake_collect)
    return seen


def run(args: list[str]) -> None:
    with pytest.raises(Stop):
        download_module.run_download([*args, "--dest", "unused"])


def test_cache_is_enabled_by_default(captured: dict[str, Any]) -> None:
    run(["somepackage"])

    assert captured["collect_cache_dir"]


def test_no_cache_dir_disables_it(captured: dict[str, Any]) -> None:
    run(["somepackage", "--no-cache-dir"])

    assert captured["collect_cache_dir"] is None


def test_cache_dir_is_honored(captured: dict[str, Any], tmp_path: Any) -> None:
    run(["somepackage", "--cache-dir", str(tmp_path)])

    assert captured["collect_cache_dir"] == str(tmp_path)


def test_no_cache_dir_wins_over_an_explicit_directory(
    captured: dict[str, Any],
    tmp_path: Any,
) -> None:
    """Asking for no cache is a refusal, not a location."""
    run(["somepackage", "--cache-dir", str(tmp_path), "--no-cache-dir"])

    assert captured["collect_cache_dir"] is None
