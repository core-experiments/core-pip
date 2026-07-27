"""Configure logging for the CLI lifecycle."""

from __future__ import annotations

import logging
import sys

VERBOSE = 15
logging.addLevelName(VERBOSE, "VERBOSE")


class BrokenStdoutLoggingError(BrokenPipeError):
    pass


class PipFormatter(logging.Formatter):
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
        if getattr(handler, "pip_core_handler", False):
            root.setLevel(logging.INFO)
            break
    else:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, "pip_core_handler", True)
        handler.setFormatter(PipFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        setattr(file_handler, "pip_core_log_file", True)
        file_handler.setFormatter(PipFormatter())
        root.addHandler(file_handler)
    root.setLevel(logging.INFO)
