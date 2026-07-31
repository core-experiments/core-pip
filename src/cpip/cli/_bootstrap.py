"""Dependency-light parsing shared by cpip's process entrypoints."""

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

VIRTUALENV_OPTIONS = frozenset(("--require-virtualenv", "--require-venv"))


def extract_python_option(args: list[str]) -> tuple[list[str], str | None]:
    filtered: list[str] = []
    target_prefix: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in COMMAND_NAMES:
            filtered.extend(args[index:])
            break
        if token == "--python":
            if index + 1 >= len(args):
                from cpip.core.errors import CommandError

                raise CommandError("--python requires a path")
            target_prefix = args[index + 1]
            index += 2
            continue
        if token.startswith("--python="):
            target_prefix = token.partition("=")[2]
            index += 1
            continue
        filtered.append(token)
        index += 1
    return filtered, target_prefix


def extract_global_options(
    args: list[str],
) -> tuple[list[str], int, bool, str | None]:
    filtered: list[str] = []
    log_file: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--log":
            if index + 1 < len(args):
                log_file = args[index + 1]
            index += 2
            continue
        if token.startswith("--log="):
            log_file = token.partition("=")[2]
            index += 1
            continue
        filtered.append(token)
        index += 1

    result: list[str] = []
    verbosity = 0
    require_virtualenv = False
    index = 0
    while index < len(filtered):
        token = filtered[index]
        if token in VIRTUALENV_OPTIONS:
            require_virtualenv = True
            index += 1
            continue
        if token == "--verbose":
            verbosity += 1
            index += 1
            continue
        if token.startswith("-") and set(token[1:]) == {"v"}:
            verbosity += len(token) - 1
            index += 1
            continue
        result.extend(filtered[index:])
        break
    return result, verbosity, require_virtualenv, log_file
