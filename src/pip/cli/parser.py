"""Shared command-line parser behavior."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import NoReturn


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            message = message.removeprefix("unrecognized arguments: ")
            message = f"no such option: {message.split()[0]}"
        usage = self.format_usage().replace("usage:", "Usage:", 1)
        self._print_message(usage, sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")
