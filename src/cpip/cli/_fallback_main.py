"""Normal cpip CLI orchestration, imported only after bootstrap dispatch."""

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
    fast_install_attempted: bool = False,
) -> int:
    spec = None
    if argv:
        from cpip.cli.commands.registry import get_command

        spec = get_command(argv[0])

    if (version is not None or location is not None) and (
        not argv or (argv[0] != "lock" and (spec is None or spec.needs_tempdir))
    ):
        from cpip.cli._execution_context import configure

        configure(
            version=version,
            runner=(
                os.path.join(os.path.dirname(location), "__cpip-runner__.py")
                if location is not None
                else None
            ),
        )

    needs_logging = spec is None or spec.needs_logging
    if needs_logging and not quiet_fast_command and not os.environ.get("CPIP_QUIET"):
        from cpip.cli.logging_config import configure_logging

        configure_logging(log_file)
    from cpip.cli._main_fallback import run as run_fallback

    if argv and argv[0] == "lock":
        return run_fallback(
            argv,
            require_virtualenv=require_virtualenv,
            location=location,
        )
    if argv and argv[0] == "install" and not fast_install_attempted:
        from cpip.cli.fast_install import run as run_fast_install

        status = run_fast_install(argv[1:])
        if status is not None:
            return status
    if spec is not None and not spec.needs_tempdir:
        return run_fallback(
            argv,
            require_virtualenv=require_virtualenv,
            location=location,
        )
    from cpip.core.temp_dir import global_tempdir_manager

    with global_tempdir_manager():
        return run_fallback(
            argv,
            require_virtualenv=require_virtualenv,
            location=location,
        )
