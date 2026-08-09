"""Defer an expensive module import to its first use.

Import cost is behavior in cpip: a command that loads the resolver to answer
a cache hit has regressed even when its output is perfect.  Most of the fix is
structural -- split a module so the light half can be imported alone -- but
some modules genuinely need a heavy dependency on a branch that most
invocations skip.  That is what this is for.

The idiom keeps the dependency declared at the top of the file, where a
reviewer looks for it, instead of scattering imports through function bodies::

    from cpip.core.lazy import lazy_module

    if TYPE_CHECKING:
        from cpip.index import provider
    else:
        provider = lazy_module("cpip.index.provider")

    ...
        if options.outdated:
            candidates = provider.CandidateProvider.from_options(...)

The ``TYPE_CHECKING`` half is what keeps type checking and editor navigation
working on the lazily bound name; the ``else`` half is what makes it lazy.

**A plain ``from x import y`` at module scope defeats this entirely.**  The
statement resolves the attribute at import time, so the module is loaded
before any deferral can intervene.  The use site has to become an attribute
access -- that is the cost of the mechanism, and the only one.

Some things can never be deferred, because they are evaluated while the
module is executing: base classes, decorators applied at ``def``/``class``
time, default argument values, anything referenced in a class body, and
exception types named in an ``except`` clause on a cold path.  Split the
module instead.

``importlib.util.LazyLoader`` is deliberately not used here.  It registers an
unexecuted placeholder in ``sys.modules``, which is exactly what the startup
budgets in ``tests/core/test_import_budgets.py`` read -- every deferred module
would be reported as imported and the budgets would stop catching anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType


class LazyModule:
    """A stand-in that imports its module on first attribute access.

    Special-method lookup bypasses ``__getattr__``, so the proxy stays inert
    -- including under ``repr()`` -- until real attribute access happens.
    """

    __slots__ = ("_lazy_module", "_lazy_name")

    def __init__(self, name: str) -> None:
        self._lazy_name = name
        self._lazy_module: ModuleType | None = None

    def _lazy_resolve(self) -> ModuleType:
        module = self._lazy_module
        if module is None:
            from importlib import import_module

            # import_module holds a per-module lock, so racing threads get the
            # same object back and the duplicate assignment is harmless. A
            # lock here would buy nothing.
            module = import_module(self._lazy_name)
            self._lazy_module = module
        return module

    def __getattr__(self, attribute: str) -> Any:
        # No try/except: a failed import should raise exactly what it would
        # have raised eagerly, with its original traceback and cause.
        return getattr(self._lazy_resolve(), attribute)

    def __dir__(self) -> list[str]:
        return dir(self._lazy_resolve())

    def __repr__(self) -> str:
        state = "imported" if self._lazy_module is not None else "not imported"
        return f"<lazy module {self._lazy_name!r} ({state})>"


def lazy_module(name: str) -> Any:
    """Bind ``name`` now, import it on first attribute access.

    Returns ``Any`` rather than ``LazyModule`` so the runtime binding stays
    compatible with the real module imported under ``TYPE_CHECKING``.
    """

    return LazyModule(name)
