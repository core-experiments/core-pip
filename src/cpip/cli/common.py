"""Shared command-line utilities, parsing, logging, and target context."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from cpip.platform.locations.sysconfig import get_scheme

if TYPE_CHECKING:
    from typing import NoReturn

# Exit codes owned by the cpip command-line application.
BROKEN_STDOUT = 120
VIRTUALENV_NOT_FOUND = 3

VERBOSE = 15
logging.addLevelName(VERBOSE, "VERBOSE")


class BrokenStdoutLoggingError(BrokenPipeError):
    pass


class CpipFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if not message.startswith("DEPRECATION: "):
            if record.levelno >= logging.ERROR:
                message = f"ERROR: {message}"
            elif record.levelno >= logging.WARNING:
                message = f"WARNING: {message}"
        return message


def configure_logging(log_file: str | None = None) -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "cpip_core_handler", False):
            root.setLevel(logging.INFO)
            break
    else:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, "cpip_core_handler", True)
        handler.setFormatter(CpipFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        setattr(file_handler, "cpip_core_log_file", True)
        file_handler.setFormatter(CpipFormatter())
        root.addHandler(file_handler)
    root.setLevel(logging.INFO)


class HelpFormatter(argparse.HelpFormatter):
    """Keep option metavar placement stable across supported Python versions."""

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action_invocation(action)
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        default_metavar = (
            action.metavar if isinstance(action.metavar, str) else action.dest.upper()
        )
        metavar = self._format_args(action, default_metavar)
        option_strings = [*action.option_strings[:-1], action.option_strings[-1]]
        if metavar:
            option_strings[-1] += f" {metavar}"
        return ", ".join(option_strings)

    def _format_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None,
    ) -> str:
        rendered = super()._format_usage(usage, actions, groups, prefix)
        return re.sub(r"(?m)(^.*\]) -d\n(\s+)DEST", r"\1\n\2-d DEST", rendered)


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            message = message.removeprefix("unrecognized arguments: ")
            message = f"no such option: {message.split()[0]}"
        usage = self.format_usage().replace("usage:", "Usage:", 1)
        self._print_message(usage, sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def target_prefix() -> str | None:
    return os.environ.get("CPIP_TARGET_PREFIX")


def target_paths() -> list[str] | None:
    prefix = target_prefix()
    if prefix is None:
        return None
    scheme = get_scheme("cpip", prefix=prefix)
    return [scheme.purelib, scheme.platlib]
