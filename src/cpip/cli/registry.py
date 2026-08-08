from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING

from cpip.cli.common import ArgumentParser

if TYPE_CHECKING:
    from collections.abc import Callable

    CommandRunner = Callable[[list[str]], int]

    ParserFactory = Callable[[], ArgumentParser]


class CommandSpec:
    __slots__ = (
        "_module",
        "module_path",
        "name",
        "needs_execution_context",
        "needs_logging",
        "needs_tempdir",
        "parser_factory",
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
    ) -> None:
        self.name = name
        self.module_path = module_path
        self._module: ModuleType | None = None
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

    def load_runner(self) -> CommandRunner | None:
        if self.runner is None:
            return None

        return getattr(self.module, self.runner)

    def create_parser(self) -> ArgumentParser:
        if self.parser_factory is not None:
            factory: ParserFactory = getattr(self.module, self.parser_factory)

            return factory()

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
        "cpip.cli.inspect",
        "run_show",
        parser_factory="create_show_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "inspect",
        "cpip.cli.inspect",
        "run_inspect",
        parser_factory="create_inspect_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "hash",
        "cpip.cli.inspect",
        "run_hash",
        parser_factory="create_hash_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "check",
        "cpip.cli.inspect",
        "run_check",
        parser_factory="create_check_parser",
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
