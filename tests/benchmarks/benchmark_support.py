"""Deterministic workload builders shared by the CodSpeed benchmarks."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import tempfile
import zipfile
from pathlib import Path

from cpip.core import packaging as packaging_module
from cpip.core import wheel as wheel_module
from cpip.core.packaging import canonicalize_name, parse_requirement
from cpip.index import metadata_cache as metadata_cache_module

SHA256_PLACEHOLDER = "a" * 64
METADATA_PLACEHOLDER = "b" * 64


def make_wheel(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    requires: list[str] | None = None,
    payload_files: int = 0,
    requires_python: str = ">=3.9",
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
            f"Requires-Python: {requires_python}\n"
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


def make_source_tree(root: Path, project: str = "bench-sdist") -> Path:
    """Write a tiny PEP 517 source tree for metadata/build benchmarks."""
    source = root / project
    package = source / project.replace("-", "_")
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = []\n"
        'build-backend = "cpip.build.build_backend"\n\n'
        "[project]\n"
        f'name = "{project}"\n'
        'version = "1.0.0"\n',
        encoding="utf-8",
    )
    return source


def make_isolated_source_tree(root: Path) -> Path:
    """Write a source tree with a local, dependency-free PEP 517 backend."""
    source = make_source_tree(root, "bench-isolated")
    (source / "backend.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
        "    dist_info = 'bench_isolated-1.0.0.dist-info'\n"
        "    target = Path(metadata_directory) / dist_info\n"
        "    target.mkdir(parents=True, exist_ok=True)\n"
        "    (target / 'METADATA').write_text(\n"
        "        'Metadata-Version: 2.1\\nName: bench-isolated\\nVersion: 1.0.0\\n'\n"
        "    )\n"
        "    (target / 'WHEEL').write_text(\n"
        "        'Wheel-Version: 1.0\\nGenerator: benchmark\\n'\n"
        "    )\n"
        "    return dist_info\n",
        encoding="utf-8",
    )
    (source / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = []\n"
        'build-backend = "backend"\n'
        'backend-path = ["."]\n\n'
        "[project]\n"
        'name = "bench-isolated"\n'
        'version = "1.0.0"\n',
        encoding="utf-8",
    )
    return source


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


def make_wrong_package_graph(
    wheelhouse: Path,
    prefix: str,
    *,
    versions: int = 64,
) -> None:
    """Build a uv-style wrong-package/backtracking workload.

    Each root release selects a matching ``left`` release and the preceding
    ``right`` release.  Those releases disagree about ``shared`` until the
    resolver reaches the oldest root, making candidate ordering significant.
    """
    for index in range(1, versions + 1):
        make_wheel(wheelhouse, f"{prefix}-shared", f"1.{index}.0")
        make_wheel(
            wheelhouse,
            f"{prefix}-left",
            f"1.{index}.0",
            requires=[f"{prefix}-shared==1.{index}.0"],
        )
        right_index = max(1, index - 1)
        make_wheel(
            wheelhouse,
            f"{prefix}-right",
            f"1.{right_index}.0",
            requires=[f"{prefix}-shared>=1.{right_index}.0,<1.{right_index + 1}.0"],
        )
        make_wheel(
            wheelhouse,
            f"{prefix}-root",
            f"1.{index}.0",
            requires=[
                f"{prefix}-left==1.{index}.0",
                f"{prefix}-right==1.{right_index}.0",
            ],
        )


def make_nab_smoke_fixture(wheelhouse: Path) -> None:
    """Build the explicit packages from nab's offline deterministic smoke suite.

    Ported from ``nab-python/benchmarks/smoke/fixture.toml`` in
    https://github.com/notatallshaw/nab -- the upstream project
    ``cpip._vendor.nab_resolver`` is vendored from. Only the packages reached
    by the scenarios cpip can actually run are included (see
    ``test_benchmark_nab_smoke.py`` for which of nab's scenarios have no cpip
    equivalent and were dropped).
    """
    make_wheel(wheelhouse, "nab-smoke-basic", "1.0.0", requires=["nab-smoke-basic-leaf>=1.0.0"])
    make_wheel(wheelhouse, "nab-smoke-basic-leaf", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-basic-leaf", "2.0.0")

    make_wheel(wheelhouse, "nab-smoke-constrained", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-constrained", "2.0.0")
    make_wheel(wheelhouse, "nab-smoke-constrained", "3.0.0")

    make_wheel(
        wheelhouse,
        "nab-smoke-extra-app",
        "1.0.0",
        requires=[
            "nab-smoke-extra-base==1.0.0",
            'nab-smoke-extra-speed==1.0.0; extra == "speed"',
            'nab-smoke-marker-leaf==1.0.0; python_version < "3.12"',
            'nab-smoke-marker-leaf==2.0.0; python_version >= "3.12"',
        ],
    )
    make_wheel(wheelhouse, "nab-smoke-extra-base", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-extra-speed", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-marker-leaf", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-marker-leaf", "2.0.0")

    make_wheel(
        wheelhouse,
        "nab-smoke-strategy-app",
        "1.0.0",
        requires=["nab-smoke-strategy-transitive>=1.0.0"],
    )
    make_wheel(wheelhouse, "nab-smoke-strategy-direct", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-strategy-direct", "2.0.0")
    make_wheel(wheelhouse, "nab-smoke-strategy-transitive", "1.0.0")
    make_wheel(wheelhouse, "nab-smoke-strategy-transitive", "2.0.0")


def make_nab_pip_backtracking_family(
    wheelhouse: Path,
    prefix: str,
    size: int,
    *,
    unsatisfiable: bool = False,
) -> None:
    """Build pip's deep-backtracking graph, ported from nab's ``_pip_backtracking_family``.

    For each version N of ``<prefix>-a``, ``a`` wants ``b==N`` and ``c==N-1``
    while ``b==N`` wants ``c==N``, so every candidate above the first pins
    ``c`` to two versions at once. The resolver has to reject all of them
    before reaching ``a==1.0.0``. The unsatisfiable variant points that last
    candidate at a version of ``c`` the fixture does not publish, so no
    candidate survives.

    Every conflict here names the decision one level up, so the resolver's
    backjumps all travel a single level --
    :func:`make_nab_deep_backjump_family` is what exercises the
    non-chronological case.

    Source: ``nab-python/benchmarks/deterministic_smoke.py`` in
    https://github.com/notatallshaw/nab.
    """
    for number in range(1, size + 1):
        version = f"{number}.0.0"
        dependencies = [f"{prefix}-b=={version}"]
        if number > 1:
            dependencies.append(f"{prefix}-c=={number - 1}.0.0")
        elif unsatisfiable:
            dependencies.append(f"{prefix}-c==0.0.0")
        make_wheel(wheelhouse, f"{prefix}-a", version, requires=dependencies)
        make_wheel(wheelhouse, f"{prefix}-b", version, requires=[f"{prefix}-c=={version}"])
        make_wheel(wheelhouse, f"{prefix}-c", version)


def make_nab_deep_backjump_family(wheelhouse: Path, prefix: str, size: int) -> None:
    """Build a graph whose conflicts sit many decision levels below the culprit.

    ``pivot`` is decided early, then a chain of ``link`` packages is walked
    one level at a time, each discovering the next. Only the last link
    reveals ``zgate``, which demands the pivot's oldest version. The conflict
    therefore names a decision made ``size`` levels earlier, and a resolver
    that backjumps chronologically has to re-derive every level in between.

    Source: ``nab-python/benchmarks/deterministic_smoke.py`` in
    https://github.com/notatallshaw/nab (``_deep_backjump_family``).
    """
    for number in (1, 2, 3):
        make_wheel(wheelhouse, f"{prefix}-pivot", f"{number}.0.0")
    for index in range(1, size + 1):
        successor = f"{prefix}-zgate" if index == size else f"{prefix}-link-{index + 1}"
        dependencies = [successor, f"{prefix}-alt-{index}"]
        for number in (1, 2, 3, 4):
            make_wheel(
                wheelhouse,
                f"{prefix}-link-{index}",
                f"{number}.0.0",
                requires=dependencies,
            )
        for number in (1, 2, 3, 4, 5):
            make_wheel(wheelhouse, f"{prefix}-alt-{index}", f"{number}.0.0")
    make_wheel(
        wheelhouse,
        f"{prefix}-zgate",
        "1.0.0",
        requires=[f"{prefix}-pivot==1.0.0"],
    )


def make_stress_graph(wheelhouse: Path, *, roots: int = 88) -> None:
    """Build many independently resolvable roots, like a large requirements file."""
    for index in range(roots):
        for version in range(3):
            make_wheel(
                wheelhouse,
                f"stress-{index}",
                f"1.{version}.0",
                requires=[f"stress-leaf-{index}>=1.1.0"],
            )
        make_wheel(wheelhouse, f"stress-leaf-{index}", "1.0.0")
        make_wheel(wheelhouse, f"stress-leaf-{index}", "1.1.0")


def make_failing_source_tree(root: Path) -> Path:
    """Write a deterministic PEP 517 backend failure for error-path timing."""
    source = root / "bench-failing"
    source.mkdir()
    (source / "backend.py").write_text(
        "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
        "    raise RuntimeError('intentional benchmark build failure')\n",
        encoding="utf-8",
    )
    (source / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = 'backend'\n"
        "backend-path = ['.']\n\n[project]\nname = 'bench-failing'\nversion = '1.0.0'\n",
        encoding="utf-8",
    )
    return source


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
                f"https://example.invalid/wheels/package_{index}-1.0-py3-none-any.whl",
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
                f"{filename}</a><br/>",
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
            },
        )
    return json.dumps(
        {"meta": {"api-version": "1.1"}, "name": "package", "files": files},
    )


_COLD_CACHE_ROOT: Path | None = None
_COLD_CACHE_SEQUENCE = itertools.count()


def cold_metadata_cache_dir() -> str:
    """A never-before-used metadata cache directory.

    Production always configures one, so a benchmark that leaves it unset
    measures a materializer with no persistent cache at all -- a code path no
    install takes. Handing out a fresh directory each call keeps the
    production path exercised while the measurement stays cold, which is what
    the ``reset_caches`` at the top of every benchmark is asking for.
    """
    global _COLD_CACHE_ROOT
    if _COLD_CACHE_ROOT is None:
        _COLD_CACHE_ROOT = Path(tempfile.mkdtemp(prefix="cpip-bench-metadata-"))
    directory = _COLD_CACHE_ROOT / str(next(_COLD_CACHE_SEQUENCE))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def reset_caches() -> None:
    """Drop the memoization that would otherwise hide parsing work.

    Every process-global cache on the resolution path belongs here.  One that
    is missed does not fail anything -- it quietly turns the second and later
    iterations of a benchmark into warm runs, so the reported figure measures
    a steady state the first call never sees and a regression in the cold path
    cannot show up at all.
    """
    canonicalize_name.cache_clear()
    parse_requirement.cache_clear()
    packaging_module._marker_applies_cached.cache_clear()
    packaging_module.Version.from_cache_state.cache_clear()
    packaging_module.Requirement.from_cache_state.cache_clear()
    wheel_module._parse_wheel_filename.cache_clear()
    wheel_module.parsed_wheel_version.cache_clear()
    wheel_module.parsed_wheel_tags.cache_clear()
    wheel_module.wheel_tag_rank.cache_clear()
    wheel_module.wheel_metadata_cache.clear()
    wheel_module.wheel_dependency_cache.clear()
    # Persistent wheel metadata caches are one instance per directory per
    # process, so they outlive the provider that opened them.
    metadata_cache_module._CACHE_INSTANCES.clear()
