"""Entry-point script generation for installed wheels."""

from __future__ import annotations

import io
import os
import sys
import zipfile
from importlib.resources import files


def rewrite_shebang(path: str, executable: str | None) -> None:
    with open(path, "rb") as file:
        contents = file.read()
    if contents.startswith(b"#!python\n"):
        with open(path, "wb") as file:
            file.write(
                f"#!{executable or sys.executable}\n".encode()
                + contents[len(b"#!python\n") :],
            )


def entry_point_scripts(path: str) -> dict[str, tuple[str, bool]]:
    try:
        with open(path, encoding="utf-8") as file:
            lines = file.read().splitlines()
    except (FileNotFoundError, IsADirectoryError):
        return {}
    active = False
    result: dict[str, tuple[str, bool]] = {}
    gui = False
    for raw_line in lines:
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


def write_windows_script(path: str, script: str, *, gui: bool) -> None:
    """Create a distlib-compatible Windows launcher without importing distlib."""
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
    suffix = "-arm" if "arm" in machine else ""
    bits = "64" if sys.maxsize > 2**32 else "32"
    launcher_name = f"{'w' if gui else 't'}{bits}{suffix}.exe"
    launcher = (files("cpip._vendor.launchers") / launcher_name).read_bytes()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("__main__.py", script.encode("utf-8"))
    with open(path, "wb") as file:
        file.write(launcher + archive.getvalue())


def script_matches(
    path: str,
    scripts: dict[str, tuple[str, bool]],
) -> bool:
    path_text = os.fspath(path)
    basename = os.path.basename(path_text)
    is_executable = basename.lower().endswith(".exe")
    name = os.path.splitext(basename)[0] if is_executable else basename
    script = scripts.get(name)
    if script is None:
        return False
    target_ref, _ = script
    module, _, attribute = target_ref.partition(":")
    entry = attribute or "main"
    try:
        if is_executable:
            with open(path, "rb") as file:
                contents = file.read()
            with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                text = archive.read("__main__.py").decode("utf-8")
        else:
            with open(path, encoding="utf-8") as file:
                text = file.read()
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return False
    return f"from {module} import {entry}" in text
