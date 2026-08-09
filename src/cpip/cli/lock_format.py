"""Shared serialization and output for the lock commands.

Imported by both ``cli.lock`` and the ``cli.fast.lock`` fast path, so this
module deliberately imports nothing.
"""

from __future__ import annotations

LOCK_HEADER = ('created-by = "cpip"', 'lock-version = "1.0"', "")


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_lock_output(output: str, rendered: str) -> None:
    """Write a rendered lock to ``output``, or to stdout for ``-``."""

    if output == "-":
        print(rendered, end="")

    else:
        with open(output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered)


def render_wheel_lock(packages: list[tuple[str, str, str, str, str]]) -> str:
    lines = list(LOCK_HEADER)
    for name, version, wheel_name, wheel_url, digest in packages:
        lines.extend(
            (
                "[[packages]]",
                f"name = {toml_string(name)}",
                f"version = {toml_string(version)}",
                "[[packages.wheels]]",
                f"name = {toml_string(wheel_name)}",
                f"url = {toml_string(wheel_url)}",
                "[packages.wheels.hashes]",
                f"sha256 = {toml_string(digest)}",
                "",
            ),
        )
    return "\n".join(lines)
