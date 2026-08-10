"""Per-route ceilings on what a ``cpip`` invocation is allowed to import.

Import cost is behavior: ``cpip install --help`` that loads the resolver has
regressed even though its output is perfect.  Nothing else in the suite can
see that, so these budgets are the only thing standing between a startup
improvement and its quiet reversal.

Every route asserts three ways, because each catches what the others miss:

- an **exact allow-list** over ``cpip.*``, where the set is small enough to
  read.  First-party growth is deliberate, so a diff that adds a name here is
  a conversation rather than a silent regression.
- a **prefix deny-list** over the expensive stdlib and vendored modules.  This
  one is stable under refactoring and does not grow with the codebase.
- a **ceiling on the module-count delta** from a baseline measured in the same
  child interpreter, which catches the long tail nobody thought to name.

The numbers are today's measurements, so the suite is green the moment it
lands and every later change may only lower them.  ``test_budgets_are_slack``
enforces that: leave more than ``SLACK_ALLOWANCE`` modules of headroom and it
fails, so an improvement has to be banked rather than spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from import_harness import ImportSnapshot, import_chain_report, import_snapshot

# Expensive modules that no route should reach by accident.  Matched as
# prefixes, so "cpip.index" also covers "cpip.index.provider".
EXPENSIVE = (
    "cpip._vendor.nab_resolver",
    "cpip._vendor.requests",
    "cpip._vendor.typing_extensions",
    "cpip.build",
    "cpip.cli.fast",
    "cpip.cli.install",
    "cpip.core.metadata",
    "cpip.core.packaging",
    "cpip.core.wheel",
    "cpip.index",
    "cpip.install",
    "cpip.network",
    "cpip.resolution",
    "cpip.vcs",
    "csv",
    "email",
    "hashlib",
    "importlib.metadata",
    "json",
    "platform",
    "ssl",
    "subprocess",
    "urllib.request",
    "zipfile",
)

# The bootstrap set: what a process pays before it even knows the command.
# Deliberately spelled out, because this is the boundary that matters most and
# a one-line diff here should be impossible to miss in review.
COLD_CORE = frozenset(
    {
        "cpip",
        "cpip.cli",
        "cpip.cli.entrypoint",
        "cpip.cli.exit_codes",
        "cpip.cli.registry",
    },
)

# A route may sit at most this many modules under its ceiling before the
# ceiling is considered stale.
SLACK_ALLOWANCE = 15


@dataclass(frozen=True)
class Route:
    """One measured way to enter ``cpip``."""

    id: str
    argv: tuple[str, ...]
    max_new_modules: int
    forbidden: tuple[str, ...] = EXPENSIVE
    allowed_first_party: frozenset[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    needs_empty_dir: bool = False

    def resolve(self, tmp_path: Path) -> list[str]:
        return [token.format(tmp=tmp_path) for token in self.argv]


BOOTSTRAP_ROUTES = (
    Route(
        id="top-level-help",
        argv=("--help",),
        max_new_modules=20,
        allowed_first_party=COLD_CORE,
    ),
    Route(
        id="version",
        argv=("--version",),
        max_new_modules=20,
        allowed_first_party=COLD_CORE,
    ),
    Route(
        id="unknown-command",
        argv=("definitely-not-a-command",),
        max_new_modules=20,
        allowed_first_party=COLD_CORE,
    ),
)

# ``<cmd> --help`` builds an argparse parser and prints it, and that is all
# it should cost.  The parser factories live in ``cpip.cli.parsers``, so none
# of these routes loads the command module that would run the command.
HELP_ALLOWED = COLD_CORE | {
    "cpip.cli.parser",
    "cpip.cli.parsers",
}


def _help_route(
    command: str, *, parser_module: str | None = None, **kwargs: object
) -> Route:
    return Route(
        id=f"{command}-help",
        argv=(command, "--help"),
        allowed_first_party=HELP_ALLOWED
        | {f"cpip.cli.parsers.{parser_module or command}"},
        **kwargs,  # type: ignore[arg-type]
    )


COMMAND_HELP_ROUTES = (
    _help_route("install", max_new_modules=40),
    _help_route("wheel", max_new_modules=40),
    _help_route("index", max_new_modules=40),
    _help_route("download", max_new_modules=40),
    _help_route("uninstall", max_new_modules=40),
    _help_route("list", max_new_modules=40),
    _help_route("freeze", max_new_modules=40),
    _help_route("show", parser_module="inspect", max_new_modules=40),
    _help_route("inspect", parser_module="inspect", max_new_modules=40),
    # ``hash`` enumerates digest algorithms to build its --algorithm choices,
    # so it alone pays for hashlib.
    _help_route(
        "hash",
        parser_module="inspect",
        max_new_modules=43,
        forbidden=tuple(name for name in EXPENSIVE if name != "hashlib"),
    ),
    _help_route("check", parser_module="inspect", max_new_modules=40),
    _help_route("cache", max_new_modules=40),
    _help_route("lock", max_new_modules=40),
)

WORK_ROUTES = (
    # The fast list path reads dist-info directly and reaches nothing in the
    # metadata, index, or resolution layers.
    Route(
        id="list-json-empty",
        argv=("list", "--format=json", "--path", "{tmp}"),
        max_new_modules=25,
        allowed_first_party=COLD_CORE
        | {
            "cpip.cli.fast",
            "cpip.cli.lock_format",
            "cpip.core",
            "cpip.core.appdirs",
            "cpip.core.names",
        },
        forbidden=tuple(name for name in EXPENSIVE if name != "cpip.cli.fast"),
        needs_empty_dir=True,
    ),
    # ``hash`` digests files and needs only hashlib to do it. It now lives in
    # its own module (cli/inspect_hash.py) instead of sharing cli/inspect.py
    # with check/inspect/show, so it no longer pays for their metadata stack.
    Route(
        id="hash-file",
        argv=("hash", "pyproject.toml"),
        max_new_modules=46,
        allowed_first_party=COLD_CORE
        | {
            "cpip.cli.fast",
            "cpip.cli.inspect_hash",
            "cpip.cli.lock_format",
            "cpip.cli.parser",
            "cpip.cli.parsers",
            "cpip.cli.parsers.inspect",
            "cpip.core",
            "cpip.core.appdirs",
            "cpip.core.names",
        },
        forbidden=tuple(
            name for name in EXPENSIVE if name not in {"cpip.cli.fast", "hashlib"}
        ),
    ),
    # check/show/inspect/freeze all read installed metadata through
    # core.light_metadata instead of core.metadata now, so none of them
    # should reach importlib.metadata, email, or cpip.core.wheel (except
    # check, which genuinely needs cpip.core.wheel for WheelTag compatibility
    # checking). Points at an empty directory so the result does not depend
    # on what happens to be installed (an editable install would otherwise
    # pull in cpip.vcs).
    Route(
        id="freeze-plain",
        argv=("freeze", "--path", "{tmp}"),
        max_new_modules=71,
        allowed_first_party=COLD_CORE
        | {
            "cpip.cli.fast",
            "cpip.cli.freeze",
            "cpip.cli.lock_format",
            "cpip.cli.logging_config",
            "cpip.cli.parser",
            "cpip.cli.parsers",
            "cpip.cli.parsers.freeze",
            "cpip.core",
            "cpip.core.appdirs",
            "cpip.core.cpip_version",
            "cpip.core.direct_url",
            "cpip.core.errors",
            "cpip.core.light_metadata",
            "cpip.core.names",
            "cpip.core.packaging",
            "cpip.core.urls",
            "cpip.core.utils",
        },
        forbidden=tuple(
            name
            for name in EXPENSIVE
            if name not in {"cpip.cli.fast", "cpip.core.packaging", "json"}
        ),
        needs_empty_dir=True,
    ),
    # ``show``/``check`` have no ``--path`` option, so these run against the
    # ambient environment rather than an isolated empty directory. That's
    # safe here (unlike freeze/list): neither code path imports anything
    # conditional on *what* is installed, only on the CLI flags given, so the
    # module set doesn't depend on which packages happen to be present.
    Route(
        id="show-nonexistent",
        argv=("show", "definitely-not-a-real-package-xyz"),
        max_new_modules=71,
        allowed_first_party=COLD_CORE
        | {
            "cpip.build",
            "cpip.build.query",
            "cpip.cli.fast",
            "cpip.cli.inspect_show",
            "cpip.cli.lock_format",
            "cpip.cli.parser",
            "cpip.cli.parsers",
            "cpip.cli.parsers.inspect",
            "cpip.core",
            "cpip.core.appdirs",
            "cpip.core.cpip_version",
            "cpip.core.direct_url",
            "cpip.core.light_metadata",
            "cpip.core.names",
            "cpip.core.packaging",
            "cpip.core.urls",
            "cpip.core.utils",
        },
        forbidden=tuple(
            name
            for name in EXPENSIVE
            if name
            not in {"cpip.cli.fast", "cpip.build", "cpip.core.packaging", "json"}
        ),
    ),
    Route(
        id="inspect-empty",
        argv=("inspect", "--path", "{tmp}"),
        max_new_modules=71,
        allowed_first_party=COLD_CORE
        | {
            "cpip.cli.fast",
            "cpip.cli.inspect",
            "cpip.cli.lock_format",
            "cpip.cli.parser",
            "cpip.cli.parsers",
            "cpip.cli.parsers.inspect",
            "cpip.core",
            "cpip.core.appdirs",
            "cpip.core.cpip_version",
            "cpip.core.direct_url",
            "cpip.core.light_metadata",
            "cpip.core.names",
            "cpip.core.packaging",
            "cpip.core.urls",
            "cpip.core.utils",
        },
        forbidden=tuple(
            name
            for name in EXPENSIVE
            if name
            not in {
                "cpip.cli.fast",
                "cpip.core.packaging",
                "json",
                "platform",
                "subprocess",
            }
        ),
        needs_empty_dir=True,
    ),
    Route(
        id="check-ambient",
        argv=("check",),
        max_new_modules=127,
        forbidden=tuple(
            name
            for name in EXPENSIVE
            if name
            not in {
                "cpip.cli.fast",
                "cpip.build",
                "cpip.core.packaging",
                "cpip.core.wheel",
                "email",
                "json",
                "platform",
                "subprocess",
                "zipfile",
            }
        ),
    ),
    # ``cache`` only lists/removes files under the cache directory -- it
    # never logs or needs a temp directory, so its CommandSpec now says so.
    Route(
        id="cache-dir",
        argv=("cache", "dir"),
        max_new_modules=54,
        forbidden=tuple(name for name in EXPENSIVE if name != "cpip.cli.fast"),
    ),
)

ROUTES = BOOTSTRAP_ROUTES + COMMAND_HELP_ROUTES + WORK_ROUTES


def _reached(modules: frozenset[str], prefix: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in modules)


def _snapshot(route: Route, direct: bool, tmp_path: Path) -> ImportSnapshot:
    return import_snapshot(
        route.resolve(tmp_path),
        cwd=tmp_path if route.needs_empty_dir else None,
        env=route.env or None,
        direct=direct,
    )


def _explain(route: Route, snapshot: ImportSnapshot, offenders: set[str]) -> str:
    chains = import_chain_report(
        offenders,
        list(snapshot.argv),
        direct=snapshot.direct,
    )
    return (
        f"\nroute {route.id!r} via {snapshot.launcher} imported "
        f"{len(offenders)} module(s) outside its budget:\n"
        f"{chains}\n\n"
        "If this is intentional, update the route in "
        "tests/core/test_import_budgets.py and say why in the commit message. "
        "If it is not, the sanctioned way to defer an import is described in "
        'docs/architecture.md under "Performance boundaries".'
    )


@pytest.mark.parametrize("direct", [False, True], ids=["python-m", "console-script"])
@pytest.mark.parametrize("route", ROUTES, ids=lambda route: route.id)
def test_route_import_budget(route: Route, direct: bool, tmp_path: Path) -> None:
    snapshot = _snapshot(route, direct, tmp_path)
    new = snapshot.new_modules()

    forbidden = {prefix for prefix in route.forbidden if _reached(new, prefix)}
    assert not forbidden, _explain(
        route,
        snapshot,
        {name for name in new for prefix in forbidden if name.startswith(prefix)},
    )

    if route.allowed_first_party is not None:
        unexpected = snapshot.first_party - route.allowed_first_party
        assert not unexpected, _explain(route, snapshot, set(unexpected))

    assert len(new) <= route.max_new_modules, (
        f"route {route.id!r} via {snapshot.launcher} imported {len(new)} modules, "
        f"budget is {route.max_new_modules}.\n{snapshot.describe()}"
    )


def test_cold_core_is_exactly_the_bootstrap_set(tmp_path: Path) -> None:
    """The one boundary worth spelling out twice."""

    snapshot = _snapshot(BOOTSTRAP_ROUTES[0], False, tmp_path)

    assert snapshot.first_party == COLD_CORE


def test_every_visible_command_has_a_help_budget() -> None:
    """Adding a command must mean adding a budget for it."""

    from cpip.cli.registry import COMMAND_SPECS

    visible = {spec.name for spec in COMMAND_SPECS if spec.visible}
    budgeted = {route.argv[0] for route in COMMAND_HELP_ROUTES}

    assert visible == budgeted


@pytest.mark.parametrize("route", ROUTES, ids=lambda route: route.id)
def test_budgets_are_not_slack(route: Route, tmp_path: Path) -> None:
    """An improvement has to be banked in the budget, not left as headroom."""

    worst = max(
        len(_snapshot(route, direct, tmp_path).new_modules())
        for direct in (False, True)
    )

    assert route.max_new_modules - worst <= SLACK_ALLOWANCE, (
        f"route {route.id!r} now imports {worst} modules but its budget is still "
        f"{route.max_new_modules}. Lower max_new_modules to {worst} so the "
        "improvement cannot be silently spent."
    )
