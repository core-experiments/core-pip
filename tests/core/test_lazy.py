"""The lazy-import proxy must defer, cache, and fail transparently."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from cpip.core.lazy import LazyModule, lazy_module

DEFERRED = "cpip.index.provider"


def test_binding_does_not_import() -> None:
    proxy = lazy_module("cpip.core.errors")

    assert isinstance(proxy, LazyModule)
    assert proxy._lazy_module is None


def test_repr_does_not_trigger_the_import() -> None:
    proxy = lazy_module("cpip.core.errors")

    assert repr(proxy) == "<lazy module 'cpip.core.errors' (not imported)>"
    assert proxy._lazy_module is None

    proxy.CpipError

    assert repr(proxy) == "<lazy module 'cpip.core.errors' (imported)>"


def test_attribute_access_matches_the_real_module() -> None:
    from cpip.core import errors

    assert lazy_module("cpip.core.errors").CpipError is errors.CpipError


def test_module_is_resolved_once() -> None:
    proxy = lazy_module("cpip.core.errors")

    proxy.CpipError
    resolved = proxy._lazy_module
    proxy.InstallationError

    assert proxy._lazy_module is resolved


def test_missing_module_raises_the_original_error() -> None:
    proxy = lazy_module("cpip.does_not_exist")

    with pytest.raises(ModuleNotFoundError, match="cpip.does_not_exist"):
        proxy.anything


def test_missing_attribute_raises_from_the_real_module() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        lazy_module("cpip.core.errors").no_such_name


def test_nothing_reaches_sys_modules_until_first_use() -> None:
    """A LazyLoader-style placeholder would make the budget tests blind."""

    script = textwrap.dedent(
        f"""
        import sys
        from cpip.core.lazy import lazy_module

        proxy = lazy_module({DEFERRED!r})
        assert {DEFERRED!r} not in sys.modules, "bound name should not import"

        proxy.CandidateProvider
        assert {DEFERRED!r} in sys.modules, "first use should import"
        """,
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_dir_reflects_the_real_module() -> None:
    from cpip.core import errors

    assert dir(lazy_module("cpip.core.errors")) == dir(errors)
