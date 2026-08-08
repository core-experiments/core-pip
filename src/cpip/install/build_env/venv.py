from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from collections.abc import Iterable
from types import TracebackType
from typing import TYPE_CHECKING, Any

from cpip.core.errors import DiagnosticCpipError
from cpip.core.temp_dir import TempDirectory, tempdir_kinds
from cpip.install.build_env.base import (
    BuildEnvironment,
    BuildEnvironmentInstaller,
    Prefix,
)

if TYPE_CHECKING:
    from cpip.resolution.req_install import InstallRequirement


class VenvImportError(DiagnosticCpipError):
    reference = "venv-import-error"

    def __init__(self) -> None:
        hint_stmt = None
        if sys.platform == "linux":
            hint_stmt = (
                "If this is an OS-provided Python, it's likely that your OS "
                "package maintainers have split Python's standard library across "
                "multiple OS packages."
            )
        super().__init__(
            message="Cannot import the 'venv' module of the Python standard library",
            context=(
                "This is a symptom of a broken/modified Python, which cannot be used with cpip."
            ),
            note_stmt="This is an issue with the Python installation itself, not cpip.",
            hint_stmt=hint_stmt,
        )


class VenvCreationError(DiagnosticCpipError):
    reference = "venv-creation-error"

    def __init__(self, context: str) -> None:
        hint_stmt = (
            "This may be caused by running antivirus software." if os.name == "nt" else None
        )
        super().__init__(
            message="Cannot create a virtual environment",
            context=context,
            hint_stmt=hint_stmt,
        )


def get_venv_path_from_sysconfig(name: str, env_dir: str) -> str:
    vars = {
        "base": env_dir,
        "platbase": env_dir,
    }
    return sysconfig.get_path(name, scheme="venv", vars=vars)


class VenvBuildEnvironment(BuildEnvironment):
    """A venv-based build environment."""

    def __init__(self, installer: BuildEnvironmentInstaller) -> None:
        # We defer these imports because certain distributions of Python do not
        # include a functional venv out of the box.
        self.temp_dir_internal = TempDirectory(
            kind=tempdir_kinds.BUILD_ENV,
            globally_managed=True,
        )
        self.env_path_internal = self.temp_dir_internal.path
        context: Any = None
        try:
            import virtualenv
        except ImportError:
            try:
                import venv
            except ImportError:
                raise VenvImportError

            env = venv.EnvBuilder(symlinks=(os.name != "nt"), with_pip=False)
            try:
                context = env.ensure_directories(self.env_path_internal)
                env.create(self.env_path_internal)
                bootstrap_environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("CPIP_") and key != "PYTHONPATH"
                }
                subprocess.run(
                    [
                        context.env_exec_cmd,
                        "-m",
                        "ensurepip",
                        "--upgrade",
                        "--default-pip",
                    ],
                    check=True,
                    cwd=self.env_path_internal,
                    env=bootstrap_environment,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as e:
                detail = str(e)
                if isinstance(e, subprocess.CalledProcessError):
                    output = "\n".join(part for part in (e.stdout, e.stderr) if part)
                    if output:
                        detail = f"{detail}: {output}"
                raise VenvCreationError(detail)
        else:
            try:
                virtualenv.cli_run([self.env_path_internal, "--no-download", "--clear"])
            except (OSError, RuntimeError) as e:
                raise VenvCreationError(str(e))

        if sys.version_info >= (3, 12) and context is not None:
            # The context object was only documented in Python 3.12.  The
            # virtualenv backend does not return one from ``cli_run``.
            self.lib_dirs = [context.lib_path]
            self.bin_path_internal = context.bin_path
        elif sys.version_info >= (3, 12):
            # Derive the same paths when virtualenv created the environment.
            # This also keeps cpip compatible with virtualenv versions that
            # intentionally return no context from their CLI entry point.
            self.lib_dirs = [
                get_venv_path_from_sysconfig("purelib", self.env_path_internal),
            ]
            self.bin_path_internal = get_venv_path_from_sysconfig(
                "scripts",
                self.env_path_internal,
            )
        elif sys.version_info[:2] == (3, 11):
            # On Python 3.11, we can use sysconfig.
            self.lib_dirs = [
                get_venv_path_from_sysconfig("purelib", self.env_path_internal),
            ]
            self.bin_path_internal = get_venv_path_from_sysconfig(
                "scripts",
                self.env_path_internal,
            )
        else:
            # Otherwise, we need to manually construct all the paths... sigh.
            if sys.platform == "win32":
                libpath = os.path.join(self.env_path_internal, "Lib", "site-packages")
            else:
                python = "pypy" if sys.implementation.name == "pypy" else "python"
                libpath = os.path.join(
                    self.env_path_internal,
                    "lib",
                    f"{python}{sys.version_info.major}.{sys.version_info.minor}",
                    "site-packages",
                )
            self.lib_dirs = [libpath]
            # Same reasoning for try-except as for python_executable below.
            try:
                self.bin_path_internal = context.bin_path
            except AttributeError:
                scripts_dir = "Scripts" if os.name == "nt" else "bin"
                self.bin_path_internal = os.path.join(
                    self.env_path_internal,
                    scripts_dir,
                )

        # There are enough ways trying to construct the Python executable path can go
        # wrong that we're better off assuming that the context object has the right
        # attributes, and only when they don't exist do we try to guess.
        #
        # These attributes seem to exist in every CPython version after 3.10.1 and
        # are documented to exist on 3.12 and higher.
        try:
            self.python_executable = context.env_exec_cmd
        except AttributeError:
            try:
                self.python_executable = context.env_exe
            except AttributeError:
                executable_name = "python.exe" if os.name == "nt" else "python"
                self.python_executable = os.path.join(
                    self.bin_path_internal,
                    executable_name,
                )

        self.save_env: dict[str, str | None] = {}
        self.installer_internal = installer

        if not os.path.exists(self.python_executable):
            # This error is only likely on Windows due to interference from AV software.
            raise VenvCreationError(
                f"Python executable failed to copy to {self.python_executable}",
            )

    def __enter__(self) -> None:
        # We want backend calls to be able to use binaries installed as if this
        # virtual environment was "activated".
        self.save_env = {name: os.environ.get(name, None) for name in ("PATH", "PYTHONPATH")}

        new_path = [self.bin_path_internal]
        if old_path := self.save_env["PATH"]:
            new_path.extend(old_path.split(os.pathsep))
        # However, we don't want a pre-existing PYTHONPATH to influence the
        # backend calls.
        os.environ.update({"PATH": os.pathsep.join(new_path), "PYTHONPATH": ""})

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        self.temp_dir_internal.cleanup()

    def install_requirements(
        self,
        requirements: Iterable[str],
        prefix_as_string: str,
        *,
        kind: str,
        for_req: InstallRequirement | None = None,
    ) -> None:
        if not requirements:
            return

        # TODO: when better support for installing to arbitrary Python environments
        # is added, replace this prefix hack with that.
        prefix = Prefix(
            self.env_path_internal,
            python_executable=self.python_executable,
        )
        self.installer_internal.install(
            requirements,
            prefix,
            kind=kind,
            for_req=for_req,
        )
