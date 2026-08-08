"""Module import utilities."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Any


def install_lazy_module_exports(
    module_globals: dict[str, Any],
    exports: Mapping[str, tuple[str, str | None]],
) -> None:
    """Install module ``__getattr__``/``__dir__`` that lazily resolve public exports.

    ``exports`` maps each public attribute name to ``(module, attribute)``.
    If ``attribute`` is None, the imported target module itself is returned.
    The target value is imported and cached in ``module_globals`` on first
    access so repeated lookups do not re-import the source module.
    """
    module_name = module_globals.get("__name__", "?")

    def __getattr__(name: str) -> Any:
        try:
            import_path, attribute_name = exports[name]
        except KeyError:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}") from None
        mod = import_module(import_path)
        value = mod if attribute_name is None else getattr(mod, attribute_name)
        module_globals[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted((*module_globals, *exports))

    module_globals["__getattr__"] = __getattr__
    module_globals["__dir__"] = __dir__
