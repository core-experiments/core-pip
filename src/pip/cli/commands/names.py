"""Command names needed before the lazy command registry is loaded."""

from __future__ import annotations

VISIBLE_COMMAND_NAMES = (
    "install",
    "wheel",
    "index",
    "download",
    "uninstall",
    "list",
    "freeze",
    "show",
    "inspect",
    "hash",
    "check",
    "cache",
    "lock",
)
COMMAND_NAMES = frozenset((*VISIBLE_COMMAND_NAMES, "help"))
