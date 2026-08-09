"""Observe what a ``cpip`` invocation imports.

Import cost is behavior in cpip: a command that loads the resolver to print
its own help has regressed even when every assertion about its output still
passes.  These helpers are the measuring instrument the startup budgets in
``test_import_budgets.py`` assert against.

Three properties matter and each is easy to lose:

- **The child must be hermetic.** A developer's ``CPIP_CACHE_DIR`` or
  ``CPIP_QUIET`` can send an invocation down a different route, so the child
  environment is built from an explicit passthrough list rather than inherited.
- **The dump must not collide with the command's own output.** ``cpip list
  --format=json`` writes JSON to stdout, so the module list goes to the file
  named by ``CPIP_IMPORT_DUMP`` instead.
- **The harness must not inflate what it measures.** The child script imports
  only ``os`` and ``sys`` (plus ``runpy`` for the ``python -m cpip`` shape),
  and :func:`baseline_modules` runs the same scaffolding with the cpip call
  removed, so a delta counts only what cpip itself pulled in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# Everything else is dropped so the child cannot inherit a CPIP_* override.
PASSTHROUGH_ENV = (
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
)

_PROLOGUE = """\
import os
import sys


def _dump():
    with open(os.environ["CPIP_IMPORT_DUMP"], "w", encoding="utf-8") as handle:
        handle.write("\\n".join(sorted(sys.modules)))


def _argv():
    raw = os.environ["CPIP_IMPORT_ARGV"]
    return ["cpip", *(raw.split("\\n") if raw else [])]
"""

# ``python -m cpip``: the shape a user gets from an interpreter invocation.
_RUNPY_SCRIPT = f"""{_PROLOGUE}
import runpy

sys.argv = _argv()
try:
    runpy.run_module("cpip", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
finally:
    _dump()
"""

# The console script: ``cpip = "cpip.cli.entrypoint:main"`` in pyproject.toml.
_DIRECT_SCRIPT = f"""{_PROLOGUE}
sys.argv = _argv()
from cpip.cli.entrypoint import main

try:
    main()
except SystemExit:
    pass
finally:
    _dump()
"""

# The same scaffolding with the cpip call removed, so subtracting it leaves
# exactly what cpip was responsible for importing.
_BASELINE_SCRIPTS = {
    False: f"{_PROLOGUE}\nimport runpy\n\n_dump()\n",
    True: f"{_PROLOGUE}\n_dump()\n",
}


@dataclass(frozen=True)
class ImportSnapshot:
    """Every module a single ``cpip`` invocation left in ``sys.modules``."""

    argv: tuple[str, ...]
    direct: bool
    modules: frozenset[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def launcher(self) -> str:
        return "console script" if self.direct else "python -m cpip"

    @property
    def first_party(self) -> frozenset[str]:
        return frozenset(name for name in self.modules if name.startswith("cpip"))

    def new_modules(self) -> frozenset[str]:
        """Modules this invocation added on top of the empty-harness baseline."""

        return self.modules - baseline_modules(direct=self.direct)

    def describe(self) -> str:
        return (
            f"argv={list(self.argv)} launcher={self.launcher} "
            f"exit={self.returncode}\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )


def child_env(
    dump_path: Path,
    args: list[str],
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal, explicit environment for a measured child process."""

    env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
    env["PYTHONPATH"] = str(SRC)
    env["CPIP_IMPORT_DUMP"] = str(dump_path)
    env["CPIP_IMPORT_ARGV"] = "\n".join(args)
    if extra:
        env.update(extra)
    return env


def _run_child(
    script: str,
    args: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    dump_path: Path,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd or ROOT,
        env=child_env(dump_path, args, extra=env),
        text=True,
        capture_output=True,
        check=False,
    )
    if not dump_path.is_file():
        raise AssertionError(
            "the measured child never wrote its module dump, so it died before "
            "reaching the harness epilogue.\n"
            f"argv={args} exit={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
        )
    dumped = dump_path.read_text(encoding="utf-8")
    return result, dumped.split("\n") if dumped else []


def import_snapshot(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct: bool = False,
) -> ImportSnapshot:
    """Run ``cpip <args>`` in a child process and report what it imported."""

    with tempfile.TemporaryDirectory(prefix="cpip-import-") as scratch:
        dump_path = Path(scratch) / "imported-modules.txt"
        result, modules = _run_child(
            _DIRECT_SCRIPT if direct else _RUNPY_SCRIPT,
            args,
            cwd=cwd,
            env=env,
            dump_path=dump_path,
        )
    return ImportSnapshot(
        argv=tuple(args),
        direct=direct,
        modules=frozenset(modules),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def imported_modules(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct: bool = False,
) -> set[str]:
    """Return the module names a ``cpip`` invocation left in ``sys.modules``."""

    return set(import_snapshot(args, cwd=cwd, env=env, direct=direct).modules)


@lru_cache(maxsize=2)
def baseline_modules(*, direct: bool = False) -> frozenset[str]:
    """Modules the harness itself costs, for the given launcher shape.

    Budgets assert on the delta from this rather than on an absolute count,
    because the absolute number moves with the Python version and the CI
    matrix spans several.
    """

    with tempfile.TemporaryDirectory(prefix="cpip-baseline-") as scratch:
        dump_path = Path(scratch) / "baseline-modules.txt"
        _, modules = _run_child(
            _BASELINE_SCRIPTS[direct],
            [],
            cwd=None,
            env=None,
            dump_path=dump_path,
        )
    return frozenset(modules)


def import_timings(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct: bool = False,
) -> list[tuple[int, str, int]]:
    """Run the invocation under ``-X importtime`` as ``(depth, name, self_us)``.

    Depth comes from the indentation of the module column, which is how
    ``importtime`` expresses the import nesting.
    """

    with tempfile.TemporaryDirectory(prefix="cpip-importtime-") as scratch:
        dump_path = Path(scratch) / "imported-modules.txt"
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "importtime",
                "-c",
                _DIRECT_SCRIPT if direct else _RUNPY_SCRIPT,
            ],
            cwd=cwd or ROOT,
            env=child_env(dump_path, args, extra=env),
            text=True,
            capture_output=True,
            check=False,
        )
    timings: list[tuple[int, str, int]] = []
    for line in result.stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        _, _, payload = line.partition("import time:")
        parts = payload.split("|")
        if len(parts) != 3 or "self" in parts[0]:
            continue
        column = parts[2]
        name = column.strip()
        depth = (len(column) - len(column.lstrip())) // 2
        try:
            self_us = int(parts[0].strip())
        except ValueError:
            continue
        timings.append((depth, name, self_us))
    return timings


def import_chain_report(
    offenders: set[str],
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct: bool = False,
) -> str:
    """Explain who imported each offending module, and what it cost.

    ``importtime`` prints children before their parent, so the importer of a
    line is the next line at one lower depth.  That is exact for a module's
    first import, which is the only one that can be responsible for the cost.
    """

    timings = import_timings(args, cwd=cwd, env=env, direct=direct)
    lines: list[str] = []
    for index, (depth, name, self_us) in enumerate(timings):
        if name not in offenders:
            continue
        chain = [name]
        wanted = depth - 1
        for parent_depth, parent_name, _ in timings[index + 1 :]:
            if parent_depth == wanted:
                chain.append(parent_name)
                wanted -= 1
                if wanted < 0:
                    break
        lines.append(f"  {name}  (+{self_us / 1000:.1f} ms self)")
        lines.append(f"    imported by: {' -> '.join(reversed(chain))}")
        offenders = offenders - {name}
    for name in sorted(offenders):
        lines.append(f"  {name}  (no importtime entry; imported before the trace)")
    return "\n".join(lines)


def run_cpip(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m cpip <args>`` for behavior (not import) assertions."""

    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(SRC)
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "cpip", *args],
        cwd=cwd or ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
