from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field


def shell_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def env_prefix(env: dict[str, str] | None) -> str:
    """A POSIX shell env-assignment prefix, e.g. ``FOO=bar BAZ=qux ``.

    Quotes only the values, so the leading tokens still parse as shell
    assignments (a fully-quoted ``'FOO=bar'`` token would not). This lets a
    single command carry per-invocation env vars without spawning a second
    process just to set them.
    """
    if not env:
        return ""
    return "".join(f"{name}={shlex.quote(value)} " for name, value in env.items())


@dataclass(frozen=True)
class Command:
    name: str
    prepare: str | None
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Hyperfine:
    name: str
    commands: list[Command]
    setup: str | None
    warmup: int | None
    min_runs: int | None
    runs: int | None
    verbose: bool
    json: bool
    ignore_failure: bool = False

    def args(self) -> list[str]:
        args = ["hyperfine"]
        if self.json:
            args.extend(["--export-json", f"{self.name}.json"])
        if self.verbose:
            args.append("--show-output")
        if self.ignore_failure:
            args.append("--ignore-failure")
        if self.setup is not None:
            args.extend(["--setup", self.setup])
        if self.warmup is not None:
            args.extend(["--warmup", str(self.warmup)])
        if self.min_runs is not None:
            args.extend(["--min-runs", str(self.min_runs)])
        if self.runs is not None:
            args.extend(["--runs", str(self.runs)])
        for command in self.commands:
            args.extend(["--command-name", command.name])
        for command in self.commands:
            args.extend(["--prepare", command.prepare or ""])
        args.extend(
            env_prefix(command.env) + shell_command(command.command)
            for command in self.commands
        )
        return args

    def run(self) -> None:
        subprocess.check_call(self.args())
