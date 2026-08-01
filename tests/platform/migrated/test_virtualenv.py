from __future__ import annotations

import sys

import cpip.platform.virtualenv as virtual
import pytest


@pytest.mark.parametrize(
    "base_prefix, expected",
    [
        (None, False),  # base_prefix missing, falls back to sys.prefix
        (sys.prefix, False),  # base interpreter
        ("not_sys_prefix", True),  # PEP 405 venv
    ],
)
def test_running_under_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    base_prefix: str | None,
    expected: bool,
) -> None:
    # Use raising=False to prevent AttributeError on missing attribute
    if base_prefix is None:
        monkeypatch.delattr(sys, "base_prefix", raising=False)
    else:
        monkeypatch.setattr(sys, "base_prefix", base_prefix, raising=False)
    assert virtual.running_under_virtualenv() == expected
