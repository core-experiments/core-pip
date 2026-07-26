from __future__ import annotations

import compileall
import os
import shutil
import sysconfig
import textwrap
import venv as venv_internal
from pathlib import Path
from typing import Literal

import virtualenv as virtualenv_internal

VirtualEnvironmentType = Literal["virtualenv", "venv"]


class VirtualEnvironment:
    """
    An abstraction around virtual environments, currently it only uses
    virtualenv but in the future it could use pyvenv.
    """

    def __init__(
        self,
        location: Path,
        template: VirtualEnvironment | None = None,
        venv_type: VirtualEnvironmentType | None = None,
    ) -> None:
        self.location = location
        assert template is None or venv_type is None
        self.venv_type_internal: VirtualEnvironmentType
        if template is not None:
            self.venv_type_internal = template.venv_type_internal
        elif venv_type is not None:
            self.venv_type_internal = venv_type
        else:
            self.venv_type_internal = "virtualenv"
        self.user_site_packages_internal = False
        self.template_internal = template
        self.sitecustomize_internal: str | None = None
        self.update_paths()
        self.create_internal()

    def update_paths(self) -> None:
        bases = {
            "installed_base": self.location,
            "installed_platbase": self.location,
            "base": self.location,
            "platbase": self.location,
        }
        paths = sysconfig.get_paths(vars=bases)
        self.bin = Path(paths["scripts"])
        self.site = Path(paths["purelib"])
        self.lib = Path(paths["stdlib"])

    def __repr__(self) -> str:
        return f"<VirtualEnvironment {self.location}>"

    def create_internal(self, clear: bool = False) -> None:
        if clear:
            shutil.rmtree(self.location)
        if self.template_internal:
            # Clone virtual environment from template.
            shutil.copytree(
                self.template_internal.location, self.location, symlinks=True
            )
            self.sitecustomize_internal = self.template_internal.sitecustomize
            self.user_site_packages_internal = self.template_internal.user_site_packages
        else:
            # Create a new virtual environment.
            if self.venv_type_internal == "virtualenv":
                virtualenv_internal.cli_run(
                    [
                        "--no-pip",
                        "--no-setuptools",
                        os.fspath(self.location),
                    ],
                )
            elif self.venv_type_internal == "venv":
                builder = venv_internal.EnvBuilder()
                context = builder.ensure_directories(os.fspath(self.location))
                builder.create_configuration(context)
                builder.setup_python(context)
                self.site.mkdir(parents=True, exist_ok=True)
            else:
                raise RuntimeError(f"Unsupported venv type {self.venv_type_internal!r}")
            self.sitecustomize = self.sitecustomize_internal
            self.user_site_packages = self.user_site_packages_internal

    def customize_site(self) -> None:
        # Enable user site (before system).
        contents = textwrap.dedent(f"""
            import os, site, sys
            if not os.environ.get('PYTHONNOUSERSITE', False):
                site.ENABLE_USER_SITE = {self.user_site_packages_internal}
                # First, drop system-sites related paths.
                original_sys_path = sys.path[:]
                # To discover system-sites related paths, clear sys.path
                # and build a new one with only system paths.
                sys.path = []
                known_paths = set()
                for path in site.getsitepackages():
                    site.addsitedir(path, known_paths=known_paths)
                for path in sys.path:
                    if path in original_sys_path:
                        original_sys_path.remove(path)
                sys.path = original_sys_path
                # Second, add user-site.
                if {self.user_site_packages_internal}:
                    site.addsitedir(site.getusersitepackages())
                # Third, add back system-sites related paths.
                for path in site.getsitepackages():
                    site.addsitedir(path)
            """).strip()
        if self.sitecustomize_internal is not None:
            contents += "\n" + self.sitecustomize_internal
        sitecustomize = self.site / "sitecustomize.py"
        sitecustomize.write_text(contents)
        # Make sure bytecode is up-to-date too.
        assert compileall.compile_file(str(sitecustomize), quiet=1, force=True)

    def rewrite_pyvenv_cfg(self, replacements: dict[str, str]) -> None:
        pyvenv_cfg = self.location.joinpath("pyvenv.cfg")
        lines = pyvenv_cfg.read_text(encoding="utf-8").splitlines()

        def maybe_replace_line(line: str) -> str:
            key = line.split("=", 1)[0].strip()
            try:
                value = replacements[key]
            except KeyError:  # No need to replace.
                return line
            return f"{key} = {value}"

        lines = [maybe_replace_line(line) for line in lines]
        pyvenv_cfg.write_text("\n".join(lines), encoding="utf-8")

    def clear(self) -> None:
        self.create_internal(clear=True)

    def move(self, location: Path | str) -> None:
        shutil.move(os.fspath(self.location), location)
        self.location = Path(location)
        self.update_paths()

    @property
    def sitecustomize(self) -> str | None:
        return self.sitecustomize_internal

    @sitecustomize.setter
    def sitecustomize(self, value: str | None) -> None:
        self.sitecustomize_internal = value
        self.customize_site()

    @property
    def user_site_packages(self) -> bool:
        return self.user_site_packages_internal

    @user_site_packages.setter
    def user_site_packages(self, value: bool) -> None:
        self.user_site_packages_internal = value
        self.rewrite_pyvenv_cfg(
            {"include-system-site-packages": str(bool(value)).lower()}
        )
        self.customize_site()
