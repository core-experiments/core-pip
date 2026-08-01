"""Deterministic workload builders shared by the CodSpeed benchmarks."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cpip.core import wheel as wheel_module
from cpip.core.packaging import canonicalize_name, parse_requirement

SHA256_PLACEHOLDER = "a" * 64
METADATA_PLACEHOLDER = "b" * 64


def make_wheel(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    requires: list[str] | None = None,
    payload_files: int = 0,
) -> Path:
    """Write a metadata-only wheel with an optional synthetic payload."""
    distribution = project.replace("-", "_")
    path = wheelhouse / f"{distribution}-{version}-py3-none-any.whl"
    dist_info = f"{distribution}-{version}.dist-info"
    requires_metadata = "".join(
        f"Requires-Dist: {requirement}\n" for requirement in requires or []
    )
    files = {
        f"{distribution}/__init__.py": f"NAME = {project!r}\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {project}\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.9\n"
            f"{requires_metadata}"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: core-pip-benchmarks\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    for index in range(payload_files):
        files[f"{distribution}/module_{index}.py"] = (
            f"VALUE = {index}\n\n\ndef compute() -> int:\n    return VALUE * 2\n"
        )
    rows = []
    for name, data in files.items():
        raw = data.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        rows.append((name, f"sha256={digest.decode()}", str(len(raw))))
    rows.append((f"{dist_info}/RECORD", "", ""))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(
            f"{dist_info}/RECORD",
            "\n".join(",".join(row) for row in rows) + "\n",
        )
    return path


def make_dependency_graph(wheelhouse: Path) -> None:
    """Build a wheelhouse shaped like a small application dependency tree."""
    for leaf in range(20):
        for minor in range(4):
            make_wheel(wheelhouse, f"leaf-{leaf}", f"1.{minor}.0")
    for middle in range(10):
        for minor in range(4):
            make_wheel(
                wheelhouse,
                f"middle-{middle}",
                f"2.{minor}.0",
                requires=[
                    f"leaf-{(middle * 2 + offset) % 20}>=1.1.0" for offset in range(5)
                ],
            )
    make_wheel(
        wheelhouse,
        "application",
        "1.0.0",
        requires=[f"middle-{index}>=2.1.0" for index in range(10)],
    )


def make_backtracking_graph(wheelhouse: Path) -> None:
    """Build a wheelhouse that forces the resolver to backtrack."""
    for minor in range(12):
        make_wheel(wheelhouse, "shared", f"1.{minor}.0")
    for minor in range(12):
        make_wheel(
            wheelhouse,
            "left",
            f"3.{minor}.0",
            requires=[f"shared=={1 if minor < 10 else 0}.{minor}.0"],
        )
    for minor in range(12):
        make_wheel(
            wheelhouse,
            "right",
            f"4.{minor}.0",
            # Only the oldest ``right`` release agrees with any ``left``
            # release, so every newer combination has to be rejected first.
            requires=[f"shared==1.{11 - minor}.0", "left>=3.0.0"],
        )
    make_wheel(
        wheelhouse,
        "conflicting",
        "1.0.0",
        requires=["left>=3.5.0", "right>=4.0.0"],
    )


def requirement_lines(count: int = 300) -> list[str]:
    """Return realistic requirement strings covering the parser's branches."""
    lines: list[str] = []
    for index in range(count):
        if index % 5 == 0:
            lines.append(f"package-{index}[socks,security]>=1.{index},<2.0")
        elif index % 5 == 1:
            lines.append(f'package-{index}==2.{index}.0 ; python_version >= "3.9"')
        elif index % 5 == 2:
            lines.append(f"package_{index}~=3.{index}.0")
        elif index % 5 == 3:
            lines.append(
                f"Package.{index} @ "
                f"https://example.invalid/wheels/package_{index}-1.0-py3-none-any.whl"
            )
        else:
            lines.append(f"package-{index}!=1.0,!=1.1,>=0.9,<9.9")
    return lines


def version_strings(count: int = 400) -> list[str]:
    """Return version strings covering release, pre, post, dev and local parts."""
    versions: list[str] = []
    for index in range(count):
        remainder = index % 4
        if remainder == 0:
            versions.append(f"1.{index}.0")
        elif remainder == 1:
            versions.append(f"2.{index}.0rc{index % 7}")
        elif remainder == 2:
            versions.append(f"3.{index}.0.post{index % 5}")
        else:
            versions.append(f"4.{index}.0.dev{index}+local.{index}")
    return versions


def wheel_filenames(count: int = 400) -> list[str]:
    """Return wheel filenames with a mix of supported and foreign tags."""
    tags = (
        "py3-none-any",
        "py2.py3-none-any",
        "cp312-cp312-manylinux_2_17_x86_64",
        "cp39-abi3-macosx_11_0_arm64",
    )
    return [
        f"package-1.{index}.0-{tags[index % len(tags)]}.whl" for index in range(count)
    ]


def simple_index_html(count: int = 400) -> str:
    """Return a PEP 503 HTML page with ``count`` distribution links."""
    rows = []
    for index in range(count):
        for suffix, tag in (("whl", "py3-none-any"), ("tar.gz", None)):
            filename = (
                f"package-1.{index}.0.tar.gz"
                if tag is None
                else f"package-1.{index}.0-{tag}.whl"
            )
            rows.append(
                f'    <a href="../../packages/{filename}'
                f'#sha256={SHA256_PLACEHOLDER}" '
                'data-requires-python="&gt;=3.9" '
                f'data-core-metadata="sha256={METADATA_PLACEHOLDER}">'
                f"{filename}</a><br/>"
            )
    body = "\n".join(rows)
    return (
        "<!DOCTYPE html><html><head><title>Links for package</title></head>"
        f"<body><h1>Links for package</h1>\n{body}\n</body></html>"
    )


def simple_index_json(count: int = 400) -> str:
    """Return a PEP 691 JSON page with ``count`` distribution files."""
    files = []
    for index in range(count):
        filename = f"package-1.{index}.0-py3-none-any.whl"
        files.append(
            {
                "filename": filename,
                "url": f"https://example.invalid/packages/{filename}",
                "hashes": {"sha256": SHA256_PLACEHOLDER},
                "requires-python": ">=3.9",
                "yanked": index % 50 == 0,
                "core-metadata": {"sha256": METADATA_PLACEHOLDER},
            }
        )
    return json.dumps(
        {"meta": {"api-version": "1.1"}, "name": "package", "files": files}
    )


def reset_caches() -> None:
    """Drop the memoization that would otherwise hide parsing work."""
    canonicalize_name.cache_clear()
    parse_requirement.cache_clear()
    wheel_module.parsed_wheel_version.cache_clear()
    wheel_module.parsed_wheel_tags.cache_clear()
    wheel_module.wheel_tag_rank.cache_clear()
    wheel_module.wheel_metadata_cache.clear()
    wheel_module.wheel_dependency_cache.clear()
