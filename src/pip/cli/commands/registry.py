"""Lazy command registration and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Callable

from pip.cli.parser import ArgumentParser

CommandRunner = Callable[[list[str]], int]
ParserFactory = Callable[[], ArgumentParser]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    module: str | None = None
    runner: str | None = None
    parser_factory: str | None = None
    visible: bool = True

    def load_runner(self) -> CommandRunner | None:
        if self.module is None or self.runner is None:
            return None
        module = import_module(self.module)
        return getattr(module, self.runner)

    def create_parser(self) -> ArgumentParser:
        if self.module is not None and self.parser_factory is not None:
            module = import_module(self.module)
            factory: ParserFactory = getattr(module, self.parser_factory)
            return factory()
        return ArgumentParser(prog=f"pip {self.name}")


COMMAND_SPECS = (
    CommandSpec(
        "install",
        "pip.cli.commands.install",
        "run_install",
        "create_parser",
    ),
    CommandSpec("wheel", "pip.cli.commands.wheel", "run_wheel", "create_parser"),
    CommandSpec("index", "pip.cli.commands.index", "run_index", "create_parser"),
    CommandSpec(
        "download", "pip.cli.commands.download", "run_download", "create_parser"
    ),
    CommandSpec(
        "uninstall", "pip.cli.commands.uninstall", "run_uninstall", "create_parser"
    ),
    CommandSpec("list", "pip.cli.commands.list", "run_list", "create_parser"),
    CommandSpec("freeze", "pip.cli.commands.freeze", "run_freeze", "create_parser"),
    CommandSpec("show", "pip.cli.commands.show", "run_show", "create_parser"),
    CommandSpec("inspect", "pip.cli.commands.inspect", "run_inspect", "create_parser"),
    CommandSpec("hash", "pip.cli.commands.hash", "run_hash", "create_parser"),
    CommandSpec("check", "pip.cli.commands.check", "run_check", "create_parser"),
    CommandSpec("cache", "pip.cli.commands.cache", "run_cache", "create_parser"),
    CommandSpec("lock", "pip.cli.commands.lock", "run_lock", "create_parser"),
    CommandSpec("help", visible=False),
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
