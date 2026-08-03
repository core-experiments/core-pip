"""Shared serialization helpers for lock-file commands."""

from __future__ import annotations


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_wheel_lock(packages: list[tuple[str, str, str, str, str]]) -> str:
    lines = ['created-by = "cpip"', 'lock-version = "1.0"', ""]
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
