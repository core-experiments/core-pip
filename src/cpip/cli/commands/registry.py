"""Lazy command registration and dispatch."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from cpip.cli.parser import ArgumentParser

    CommandRunner = Callable[[list[str]], int]
    ParserFactory = Callable[[], ArgumentParser]


class CommandSpec:
    __slots__ = (
        "module",
        "name",
        "needs_logging",
        "needs_tempdir",
        "parser_factory",
        "runner",
        "visible",
    )

    def __init__(
        self,
        name: str,
        module: str | None = None,
        runner: str | None = None,
        parser_factory: str | None = None,
        visible: bool = True,
        needs_logging: bool = True,
        needs_tempdir: bool = True,
    ) -> None:
        self.name = name
        self.module = module
        self.runner = runner
        self.parser_factory = parser_factory
        self.visible = visible
        self.needs_logging = needs_logging
        self.needs_tempdir = needs_tempdir

    name: str
    module: str | None
    runner: str | None
    parser_factory: str | None
    visible: bool
    needs_logging: bool
    needs_tempdir: bool

    def load_runner(self) -> CommandRunner | None:
        if self.module is None or self.runner is None:
            return None
        module = import_module(self.module)
        return getattr(module, self.runner)

    def create_parser(self) -> ArgumentParser:
        from cpip.cli.parser import ArgumentParser

        if self.module is not None and self.parser_factory is not None:
            module = import_module(self.module)
            factory: ParserFactory = getattr(module, self.parser_factory)
            return factory()
        return ArgumentParser(prog=f"cpip {self.name}")


COMMAND_SPECS = (
    CommandSpec(
        "install",
        "cpip.cli.commands.install",
        "run_install",
        "create_parser",
    ),
    CommandSpec("wheel", "cpip.cli.commands.wheel", "run_wheel", "create_parser"),
    CommandSpec("index", "cpip.cli.commands.index", "run_index", "create_parser"),
    CommandSpec(
        "download",
        "cpip.cli.commands.download",
        "run_download",
        "create_parser",
    ),
    CommandSpec(
        "uninstall",
        "cpip.cli.commands.uninstall",
        "run_uninstall",
        "create_parser",
    ),
    CommandSpec(
        "list",
        "cpip.cli.commands.list",
        "run_list",
        "create_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "freeze",
        "cpip.cli.commands.freeze",
        "run_freeze",
        "create_parser",
        needs_tempdir=False,
    ),
    CommandSpec(
        "show",
        "cpip.cli.commands.show",
        "run_show",
        "create_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "inspect",
        "cpip.cli.commands.inspect",
        "run_inspect",
        "create_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "hash",
        "cpip.cli.commands.hash",
        "run_hash",
        "create_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "check",
        "cpip.cli.commands.check",
        "run_check",
        "create_parser",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec("cache", "cpip.cli.commands.cache", "run_cache", "create_parser"),
    CommandSpec("lock", "cpip.cli.commands.lock", "run_lock", "create_parser"),
    CommandSpec("help", visible=False, needs_logging=False, needs_tempdir=False),
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
