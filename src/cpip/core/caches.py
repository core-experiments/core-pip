"""Process-global caches: one eviction policy, one registry, one reset.

Two kinds of cache exist in the core modules and each has one spelling:

* a **table** (``dict``) interns values by text inside a constructor --
  ``Version(text)``, ``parse_requirement(text)`` -- and is bounded with
  :func:`bounded_put`;
* a **memo** (``functools.lru_cache``) caches a pure derived function.

Both are registered where they are defined, with :func:`register_table` or
:func:`register_clear`, so :func:`clear_all` empties every one of them. The
benchmarks call it between iterations; a cache that is not registered here
silently turns a cold benchmark into a warm one, which is why
``tests/core/test_cache_registry.py`` enumerates the core modules and fails
on an unregistered cache.

Eviction is a clear-all sweep at the limit rather than LRU bookkeeping:
the tables are read on hot paths where a plain ``dict`` lookup is the whole
point, and a sweep costs one rebuild of the working set every few thousand
distinct texts.

Thread safety: none of this is synchronised. Every cached value is an
immutable value type that compares equal to whatever a concurrent caller
would have built in its place, so a lost store or a sweep that races with a
lookup is a cache miss, never a wrong answer.
"""

from __future__ import annotations

from collections.abc import Callable

from functools import lru_cache

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

_CLEARERS: list[Callable[[], None]] = []


def bounded_put(table: dict, key: object, value: object, limit: int) -> None:
    """Store ``value`` under ``key``, sweeping the table first once it is full."""
    if len(table) >= limit:
        table.clear()
    table[key] = value


def register_table(table: dict) -> dict:
    """Register an intern table for :func:`clear_all`; returns it for inline use."""
    _CLEARERS.append(table.clear)
    return table


def register_clear(clear: Callable[[], None]) -> None:
    """Register a ``cache_clear``-style callable for :func:`clear_all`."""
    _CLEARERS.append(clear)


def clear_all() -> None:
    """Empty every registered cache."""
    for clear in _CLEARERS:
        clear()


def memoized(maxsize: int) -> Callable[[Callable[..., Any]], Any]:
    """``functools.lru_cache`` that registers itself for :func:`clear_all`."""

    def decorate(function: Callable[..., Any]) -> Any:
        wrapped = lru_cache(maxsize=maxsize)(function)
        _CLEARERS.append(wrapped.cache_clear)
        return wrapped

    return decorate
