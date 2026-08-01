"""Process-local context supplied by the cpip command entrypoint."""

from __future__ import annotations


class ExecutionContext:
    __slots__ = ("runner", "version")

    def __init__(self) -> None:
        self.version: str | None = None
        self.runner: str | None = None


context = ExecutionContext()


def configure(*, version: str | None = None, runner: str | None = None) -> None:
    if version is not None:
        context.version = version
    if runner is not None:
        context.runner = runner


def current_version() -> str | None:
    return context.version


def current_runner() -> str | None:
    return context.runner
