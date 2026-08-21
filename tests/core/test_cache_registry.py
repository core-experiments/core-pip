"""reset_caches() empties every process-global cache in the core modules.

A cache the reset misses does not fail anything on its own: it silently
turns the later iterations of a cold benchmark into warm ones. This test
enumerates the caches by introspection -- every ``lru_cache`` wrapper and
every mutable ``dict``/``set`` at module (or class) level -- so that adding
a cache without registering it for reset is a test failure, not a quiet
change in what the benchmarks measure.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cpip.core import names, packaging, wheel
from cpip.core.packaging import Requirement, Version, marker_applies, parse_requirement
from cpip.core.wheel import (
    parsed_wheel_tags,
    parsed_wheel_version,
    supported_wheel_tags,
    wheel_tag_rank,
)

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import reset_caches  # noqa: E402

MODULES = (packaging, wheel, names)


def _caches(module: types.ModuleType) -> Iterator[tuple[str, Any]]:
    for name, value in vars(module).items():
        if name.startswith("__") or name.isupper():
            continue  # dunders and constants
        if hasattr(value, "cache_info"):
            yield f"{module.__name__}.{name}", value
        elif isinstance(value, (dict, set)):
            yield f"{module.__name__}.{name}", value
        elif isinstance(value, type) and value.__module__ == module.__name__:
            for attribute, member in vars(value).items():
                inner = getattr(member, "__func__", member)
                if hasattr(inner, "cache_info"):
                    yield f"{module.__name__}.{name}.{attribute}", inner


def _size(cache: Any) -> int:
    if hasattr(cache, "cache_info"):
        return cache.cache_info().currsize
    return len(cache)


def _warm_everything() -> None:
    requirement = parse_requirement('pkg[extra]>=1.0,<2; python_version >= "3.8"')
    assert requirement.specifier.contains(Version("1.5"))
    assert marker_applies(requirement.marker, extras=("extra",))

    # Ordering against a foreign operand runs the coercion path, which
    # records the str() forms that fail to parse.
    class Sentinel:
        def __str__(self) -> str:
            return "+inf"

    with pytest.raises(TypeError):
        assert Version("1.2.3") < Sentinel()
    assert Version.from_cache_state(Version("1.2.3").cache_state_internal()) == Version(
        "1.2.3"
    )
    assert (
        Requirement.from_cache_state(requirement.cache_state_internal()) == requirement
    )
    assert names.canonicalize_name("Some_Project") == "some-project"
    assert parsed_wheel_version("1.0") == Version("1.0")
    assert wheel.parse_wheel_filename("pkg-1.0-py3-none-any.whl") is not None
    tags = parsed_wheel_tags("py3", "none", "any")
    assert wheel_tag_rank(tags, supported_wheel_tags()) is not None
    assert wheel._dist_info_match_key("Some_Project") == "someproject"


def test_the_core_modules_expose_caches() -> None:
    found = [name for module in MODULES for name, _ in _caches(module)]
    assert "cpip.core.packaging.parse_requirement" in found
    assert "cpip.core.wheel.wheel_metadata_cache" in found
    assert "cpip.core.packaging.Version.from_cache_state" in found


def test_reset_caches_empties_every_core_cache() -> None:
    _warm_everything()
    warmed = {
        name for module in MODULES for name, cache in _caches(module) if _size(cache)
    }
    assert "cpip.core.packaging.parse_requirement" in warmed
    assert "cpip.core.packaging._uncoercible_strings" in warmed
    reset_caches()
    still_full = {
        name: _size(cache)
        for module in MODULES
        for name, cache in _caches(module)
        if _size(cache)
    }
    assert not still_full, f"reset_caches() missed: {still_full}"


@pytest.mark.parametrize("module", MODULES, ids=lambda module: module.__name__)
def test_every_cache_is_a_known_kind(module: types.ModuleType) -> None:
    """Only lru_cache wrappers and plain dict/set tables: anything else
    (a custom memo class, an OrderedDict LRU) would dodge the enumeration."""
    for name, cache in _caches(module):
        assert hasattr(cache, "cache_info") or type(cache) in (dict, set), name
