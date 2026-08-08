"""Command-line application for cpip."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cpip.core.lazy import install_lazy_module_exports

if TYPE_CHECKING:
    from cpip.cli import (
        _main_fallback,
        cache,
        config,
        context,
        dependency_groups,
        entrypoint,
        fast_install,
        fast_install_cache,
        fast_list,
        freeze,
        logging_config,
        main,
        parser,
        requirements,
        status_codes,
    )

_EXPORTS = {
    "_main_fallback": ("cpip.cli._main_fallback", None),
    "cache": ("cpip.cli.cache", None),
    "config": ("cpip.cli.config", None),
    "context": ("cpip.cli.context", None),
    "dependency_groups": ("cpip.cli.dependency_groups", None),
    "entrypoint": ("cpip.cli.entrypoint", None),
    "fast_install": ("cpip.cli.fast_install", None),
    "fast_install_cache": ("cpip.cli.fast_install_cache", None),
    "fast_list": ("cpip.cli.fast_list", None),
    "freeze": ("cpip.cli.freeze", None),
    "logging_config": ("cpip.cli.logging_config", None),
    "main": ("cpip.cli.main", None),
    "parser": ("cpip.cli.parser", None),
    "requirements": ("cpip.cli.requirements", None),
    "status_codes": ("cpip.cli.status_codes", None),
}

install_lazy_module_exports(globals(), _EXPORTS)

__all__ = tuple(_EXPORTS)
