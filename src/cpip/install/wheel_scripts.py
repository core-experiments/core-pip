"""Entry-point script generation for installed wheels."""

from __future__ import annotations

import io
import os
import sys
import zipfile
from importlib.resources import files
from pathlib import Path


def rewrite_shebang(path: Path, executable: str | None) -> None:
    contents = path.read_bytes()
    if contents.startswith(b"#!python\n"):
        path.write_bytes(
            f"#!{executable or sys.executable}\n".encode()
            + contents[len(b"#!python\n") :]
        )


def entry_point_scripts(path: Path) -> dict[str, tuple[str, bool]]:
    if not path.is_file():
        return {}
    active = False
    result: dict[str, tuple[str, bool]] = {}
    gui = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            active = section in {"console_scripts", "gui_scripts"}
            gui = section == "gui_scripts"
        elif active and "=" in line and not line.startswith("#"):
            name, target = line.split("=", 1)
            result[name.strip()] = (target.strip(), gui)
    return result


def script_text(target_ref: str, executable: str | None) -> str:
    module, _, attribute = target_ref.partition(":")
    entry = attribute or "main"
    return (
        f"#!{executable or sys.executable}\n"
        "import re\nimport sys\n"
        f"from {module} import {entry}\n\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({entry}())\n"
    )


def write_windows_script(path: Path, script: str, *, gui: bool) -> None:
    """Create a distlib-compatible Windows launcher without importing distlib."""
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    suffix = "-arm" if "arm" in machine else ""
    bits = "64" if sys.maxsize > 2**32 else "32"
    launcher_name = f"{'w' if gui else 't'}{bits}{suffix}.exe"
    launcher = (files("cpip._vendor.launchers") / launcher_name).read_bytes()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("__main__.py", script.encode("utf-8"))
    path.write_bytes(launcher + archive.getvalue())


def script_matches(path: Path, scripts: dict[str, tuple[str, bool]]) -> bool:
    is_executable = path.suffix.lower() == ".exe"
    name = path.stem if is_executable else path.name
    script = scripts.get(name)
    if script is None:
        return False
    target_ref, _ = script
    module, _, attribute = target_ref.partition(":")
    entry = attribute or "main"
    try:
        if is_executable:
            with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
                text = archive.read("__main__.py").decode("utf-8")
        else:
            text = path.read_text(encoding="utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return False
    return f"from {module} import {entry}" in text
