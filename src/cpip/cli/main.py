"""Compatibility entrypoint for callers importing ``cpip.cli.main``."""

from __future__ import annotations


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    from cpip.cli.entrypoint import main as run_entrypoint

    return run_entrypoint(args, version=version, location=location)


def run(
    args: list[str], *, require_virtualenv: bool = False, location: str | None = None
) -> int:
    from cpip.cli._main_fallback import run as run_fallback

    return run_fallback(args, require_virtualenv=require_virtualenv, location=location)
