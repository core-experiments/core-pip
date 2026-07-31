"""Normal pip CLI orchestration, imported only after bootstrap dispatch."""

from __future__ import annotations

import os


def run(
    argv: list[str],
    *,
    require_virtualenv: bool,
    log_file: str | None,
    version: str | None,
    location: str | None,
    quiet_fast_command: bool,
) -> int:
    if (version is not None or location is not None) and (not argv or argv[0] != "lock"):
        from pip.cli._execution_context import configure

        configure(
            version=version,
            runner=(
                os.path.join(os.path.dirname(location), "__pip-runner__.py")
                if location is not None
                else None
            ),
        )

    if not quiet_fast_command:
        from pip.cli.logging_config import configure_logging

        configure_logging(log_file)
    from pip.cli._main_fallback import run as run_fallback

    if argv and argv[0] == "lock":
        return run_fallback(
            argv, require_virtualenv=require_virtualenv, location=location
        )
    if argv and argv[0] == "install":
        from pip.cli.commands.fast_install import run as run_fast_install

        status = run_fast_install(argv[1:])
        if status is not None:
            return status
    from pip.core.temp_dir import global_tempdir_manager

    with global_tempdir_manager():
        return run_fallback(
            argv, require_virtualenv=require_virtualenv, location=location
        )
