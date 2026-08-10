from __future__ import annotations

from types import ModuleType

# ``from typing import TYPE_CHECKING`` would work identically for a type
# checker, but the real ``typing`` module costs ~1.8ms to import (it pulls in
# ``re``, ``collections.abc``, and ``enum``) and this module is in
# ``COLD_CORE`` -- every ``cpip`` invocation pays for it.  A local ``False``
# is exactly what ``typing.TYPE_CHECKING`` evaluates to at runtime anyway.
TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable

    from cpip.cli.parser import ArgumentParser

    CommandRunner = Callable[[list[str]], int]

    ParserFactory = Callable[[], ArgumentParser]


class CommandSpec:
    """How to reach one command without importing the rest of them.

    Both module paths are strings, so a spec costs nothing until dispatch
    needs it.  ``parser_module`` is separate from ``module_path`` on purpose:
    ``cpip install --help`` has to build a parser but must not load the
    installer to do it.
    """

    __slots__ = (
        "_module",
        "_parser_module",
        "module_path",
        "name",
        "needs_execution_context",
        "needs_logging",
        "needs_tempdir",
        "parser_factory",
        "parser_module_path",
        "runner",
        "visible",
    )

    def __init__(
        self,
        name: str,
        module_path: str,
        runner: str | None = "run",
        parser_factory: str | None = "create_parser",
        visible: bool = True,
        needs_logging: bool = True,
        needs_tempdir: bool = True,
        needs_execution_context: bool = True,
        parser_module_path: str | None = None,
    ) -> None:
        self.name = name
        self.module_path = module_path
        self.parser_module_path = parser_module_path or f"cpip.cli.parsers.{name}"
        self._module: ModuleType | None = None
        self._parser_module: ModuleType | None = None
        self.runner = runner
        self.parser_factory = parser_factory
        self.visible = visible
        self.needs_logging = needs_logging
        self.needs_tempdir = needs_tempdir
        self.needs_execution_context = needs_execution_context

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            from importlib import import_module

            self._module = import_module(self.module_path)
        return self._module

    @property
    def parser_module(self) -> ModuleType:
        if self._parser_module is None:
            from importlib import import_module

            self._parser_module = import_module(self.parser_module_path)
        return self._parser_module

    def load_runner(self) -> CommandRunner | None:
        if self.runner is None:
            return None

        return getattr(self.module, self.runner)

    def create_parser(self) -> ArgumentParser:
        if self.parser_factory is not None:
            factory: ParserFactory = getattr(self.parser_module, self.parser_factory)

            return factory()

        from cpip.cli.parser import ArgumentParser

        return ArgumentParser(prog=f"cpip {self.name}")


COMMAND_SPECS = (
    CommandSpec("install", "cpip.cli.install", "run_install"),
    CommandSpec("wheel", "cpip.cli.wheel", "run_wheel"),
    CommandSpec("index", "cpip.cli.index", "run_index"),
    CommandSpec("download", "cpip.cli.download", "run_download"),
    CommandSpec("uninstall", "cpip.cli.uninstall", "run_uninstall"),
    CommandSpec(
        "list",
        "cpip.cli.list",
        "run_list",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "freeze",
        "cpip.cli.freeze",
        "run_freeze",
        needs_tempdir=False,
    ),
    CommandSpec(
        "show",
        "cpip.cli.inspect_show",
        "run_show",
        parser_factory="create_show_parser",
        parser_module_path="cpip.cli.parsers.inspect",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "inspect",
        "cpip.cli.inspect",
        "run_inspect",
        parser_factory="create_inspect_parser",
        parser_module_path="cpip.cli.parsers.inspect",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "hash",
        "cpip.cli.inspect_hash",
        "run_hash",
        parser_factory="create_hash_parser",
        parser_module_path="cpip.cli.parsers.inspect",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "check",
        "cpip.cli.inspect_check",
        "run_check",
        parser_factory="create_check_parser",
        parser_module_path="cpip.cli.parsers.inspect",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec("cache", "cpip.cli.cache", "run_cache"),
    # ``lock`` writes a lock file rather than running an installed
    # environment, so it never needs the runner/version execution context.
    CommandSpec(
        "lock",
        "cpip.cli.lock",
        "run_lock",
        needs_execution_context=False,
    ),
    CommandSpec(
        "help",
        "cpip.cli.entrypoint",
        runner=None,
        parser_factory=None,
        visible=False,
        needs_logging=False,
        needs_tempdir=False,
    ),
)


COMMANDS_internal = {spec.name: spec for spec in COMMAND_SPECS}

COMMANDS = tuple(COMMANDS_internal)


def get_command(command: str) -> CommandSpec | None:
    return COMMANDS_internal.get(command)


def get_command_runner(command: str) -> CommandRunner | None:
    spec = get_command(command)

    return spec.load_runner() if spec is not None else None


def parser_for_command(command: str) -> ArgumentParser:
    spec = get_command(command)

    if spec is None:
        raise KeyError(command)

    return spec.create_parser()
