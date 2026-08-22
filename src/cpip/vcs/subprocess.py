from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence

from cpip.core.errors import InstallationError
from cpip.core.subprocess import CommandArg, CommandArgs, command_args_to_argv

from .support import HiddenText

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any, Literal, Protocol

    class SpinnerInterface(Protocol):
        def spin(self) -> None: ...
        def finish(self, final_status: str) -> None: ...


logger = logging.getLogger("cpip.vcs.subprocess")


def make_command(*args: str | HiddenText | Sequence[CommandArg]) -> CommandArgs:
    result: CommandArgs = []
    for arg in args:
        result.extend(arg if isinstance(arg, list) else [arg])
    return result


def call_subprocess(
    cmd: CommandArgs,
    show_stdout: bool = False,
    cwd: str | None = None,
    on_returncode: Literal["raise", "warn", "ignore"] = "raise",
    extra_ok_returncodes: Iterable[int] | None = None,
    extra_environ: Mapping[str, Any] | None = None,
    unset_environ: Iterable[str] | None = None,
    spinner: SpinnerInterface | None = None,
    log_failed_cmd: bool | None = True,
    stdout_only: bool | None = False,
    *,
    command_desc: str,
) -> str:
    import subprocess as subprocess_internal

    env = os.environ.copy()
    if extra_environ:
        env.update({key: str(value) for key, value in extra_environ.items()})
    for name in unset_environ or ():
        env.pop(name, None)
    logger.log(
        logging.INFO if show_stdout else logging.DEBUG,
        "Running command %s",
        command_desc,
    )
    try:
        process = subprocess_internal.Popen(
            command_args_to_argv(cmd),
            stdin=subprocess_internal.PIPE,
            stdout=subprocess_internal.PIPE,
            stderr=subprocess_internal.PIPE
            if stdout_only
            else subprocess_internal.STDOUT,
            cwd=cwd,
            env=env,
            encoding="locale",
            errors="backslashreplace",
        )
    except OSError:
        raise
    output, error = process.communicate()
    output = output or ""
    error = error or ""
    result = output if stdout_only else output + error
    for line in result.splitlines():
        logger.log(logging.INFO if show_stdout else logging.DEBUG, "%s", line)
        if spinner is not None:
            spinner.spin()
    allowed = {0, *(extra_ok_returncodes or ())}
    if process.returncode not in allowed:
        if spinner is not None:
            spinner.finish("error")
        if on_returncode == "warn":
            logger.warning(
                "Command %r had error code %s in %s",
                command_desc,
                process.returncode,
                cwd,
            )
        elif on_returncode == "raise":
            raise InstallationError(
                f"{command_desc} exited with code {process.returncode}: {result}",
            )
    elif spinner is not None:
        spinner.finish("done")
    return result
