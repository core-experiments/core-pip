"""PEP 517 backend invocation for source builds."""

from __future__ import annotations

import json
import os
import subprocess

from cpip.core.errors import InstallationError

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


class ConfiguredBuildBackend:
    def __init__(
        self,
        *,
        source_dir: str | os.PathLike[str],
        backend: str,
        backend_path: tuple[str, ...],
        python_executable: str | os.PathLike[str],
    ) -> None:
        self.source_dir = source_dir

        self.backend = backend

        self.backend_path = backend_path

        self.python_executable = python_executable

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def build_wheel(
        self,
        wheel_directory: str,
        config_settings: dict[str, object] | None = None,
        metadata_directory: str | None = None,
    ) -> object:
        return self.call_hook(
            "build_wheel",
            wheel_directory,
            config_settings,
            metadata_directory,
        )

    def call_hook(self, hook: str, *args: object) -> object:
        payload = {
            "backend": self.backend,
            "hook": hook,
            "args": args,
        }

        env = os.environ.copy()

        source_text = os.fspath(self.source_dir)

        pythonpath = [source_text]

        pythonpath.extend(
            os.path.realpath(os.path.join(source_text, path))
            for path in self.backend_path
        )

        existing = env.get("PYTHONPATH")

        if existing:
            pythonpath.extend(existing.split(os.pathsep))

        if pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath)

        process = subprocess.run(
            [os.fspath(self.python_executable), "-c", BACKEND_CALLER],
            cwd=self.source_dir,
            env=env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )

        if process.returncode != 0:
            details = process.stderr.strip() or process.stdout.strip()

            raise InstallationError(details or f"Build backend hook failed: {hook}")

        output = process.stdout.strip().splitlines()

        if not output:
            return None

        return json.loads(output[-1])["result"]


BACKEND_CALLER = r"""

import importlib

import json

import sys



payload = json.loads(sys.stdin.read())

backend = payload["backend"]

module_name, _, object_path = backend.partition(":")

target = importlib.import_module(module_name)

if object_path:

    for part in object_path.split("."):

        target = getattr(target, part)

hook = getattr(target, payload["hook"])

result = hook(*payload["args"])

print(json.dumps({"result": result}))

"""
