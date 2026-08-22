from __future__ import annotations

import locale
import logging
import os
import shlex
from os import PathLike

from .errors import DiagnosticCpipError

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

VERBOSE = 15
logging.addLevelName(VERBOSE, "VERBOSE")


class InstallationSubprocessError(DiagnosticCpipError):
    reference = "subprocess-exited-with-error"

    def __init__(
        self,
        *,
        command_description: str,
        exit_code: int,
        output: str,
    ) -> None:
        super().__init__(
            message=f"{command_description} exited with exit code: {exit_code}",
            context=output or None,
            note_stmt="This error originates from a subprocess, and is likely not a problem with cpip.",
        )


subprocess_logger = logging.getLogger("cpip.subprocessor")
CommandArg = str | bytes | PathLike[str] | PathLike[bytes] | object
CommandArgs = list[CommandArg]


def format_command_args(args: CommandArgs) -> str:
    rendered: list[str] = []
    for arg in args:
        value = getattr(arg, "redacted", arg)
        rendered.append(shlex.quote(str(value)))
    return " ".join(rendered)


def command_args_to_argv(
    args: CommandArgs,
) -> list[str | bytes | PathLike[Any]]:
    return [_argv_item(arg) for arg in args]


def _argv_item(arg: CommandArg) -> str | bytes | PathLike[Any]:
    # A HiddenText argument exposes its real value as ``secret``.
    secret = getattr(arg, "secret", None)
    if isinstance(secret, str):
        return secret
    if isinstance(arg, (str, bytes, PathLike)):
        return arg
    raise TypeError(
        f"expected str, bytes or os.PathLike object, not {type(arg).__name__}"
    )


def decode_output(data: bytes) -> str:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return data.decode(encoding, errors="backslashreplace")


def call_subprocess(
    cmd: CommandArgs,
    *,
    command_desc: str,
    stdout_only: bool = False,
    show_stdout: bool = False,
    extra_ok_returncodes: tuple[int, ...] | None = None,
    cwd: str | None = None,
    extra_environ: dict[str, str] | None = None,
) -> str:
    # Deferred: the stdlib module is needed only when a process is run.
    import subprocess

    log_level = logging.INFO if show_stdout else VERBOSE
    subprocess_logger.log(log_level, "Running command %s", format_command_args(cmd))
    command = command_args_to_argv(cmd)
    environment = None
    if extra_environ is not None:
        environment = os.environ.copy()
        environment.update(extra_environ)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=environment,
        text=False,
    )
    stdout, stderr = proc.communicate()
    out_text = decode_output(stdout)
    err_text = decode_output(stderr)
    combined = out_text if stdout_only else out_text + err_text

    for line in out_text.splitlines():
        subprocess_logger.log(log_level, line)
    for line in err_text.splitlines():
        subprocess_logger.log(log_level, line)

    ok_returncodes = {0}
    if extra_ok_returncodes:
        ok_returncodes.update(extra_ok_returncodes)
    if proc.returncode not in ok_returncodes:
        subprocess_logger.error("Got subprocess error exited with %s", proc.returncode)
        raise InstallationSubprocessError(
            command_description=command_desc,
            exit_code=proc.returncode,
            output=combined,
        )
    return combined
