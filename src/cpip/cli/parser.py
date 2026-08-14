"""The argparse subclasses every command parser is built from.

Split out of ``cli.common`` so that importing it is a decision a command
makes, not a toll the entrypoint pays: ``argparse`` costs several
milliseconds and no route that only resolves a command name needs it.

The base classes here are evaluated at class-creation time, so this module
cannot be made lazy -- separating it is the only way to keep its cost off the
startup path.
"""

from __future__ import annotations

import argparse
import re
import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, NoReturn


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

    @property
    def _get_validation_formatter(self) -> NoReturn:
        """Skip 3.14's eager metavar/help-string validation on every ``add_argument``.

        ``_ActionsContainer.add_argument`` gates two validation-only
        ``HelpFormatter`` builds on ``hasattr(self, "_get_validation_formatter")``:
        one checks a tuple ``metavar`` against ``nargs``, the other expands
        the help string. The auto-added ``-h``/``--help`` action always has
        help text, so stock ``argparse`` pays for both on every parser
        construction -- building that formatter's ``_set_color`` step
        unconditionally does ``from _colorize import ...``, which drags in
        ``dataclasses`` and, with it,
        ``inspect``/``dis``/``tokenize``/``ast``/``annotationlib``. That's a
        few milliseconds on every cpip invocation to validate strings that
        get the same validation for real the moment help is actually
        formatted. Shadowing the attribute with a property that raises
        ``AttributeError`` makes ``hasattr`` false, so both checks defer to
        that real formatting -- a malformed metavar or ``%`` in a help
        string still raises, just from ``format_help``/``print_help``
        instead of from ``add_argument``.

        A property rather than deleting the base method: this attribute may
        not exist on older Python versions cpip supports, in which case
        nothing consults ``hasattr`` here and this override is simply
        unused.
        """
        raise AttributeError("_get_validation_formatter")

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            message = message.removeprefix("unrecognized arguments: ")
            message = f"no such option: {message.split()[0]}"
        usage = self.format_usage().replace("usage:", "Usage:", 1)
        self._print_message(usage, sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")
