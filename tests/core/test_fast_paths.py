from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pip.cli.commands.fast_install import run as run_fast_install
from pip.resolution.fast_local_wheelhouse import (
    LocalWheelVersion,
    _quote_path,
    _requirement,
    resolve,
)


def test_local_wheel_version_caches_comparison_key() -> None:
    first = LocalWheelVersion((1, 0, 0), "1.0.0")
    second = LocalWheelVersion((1,), "1")

    assert first == second
    assert hash(first) == hash(second)
    assert not first < second


def test_compatible_release_keeps_original_precision() -> None:
    requirement = _requirement("demo~=1.4.5.0")
    assert requirement is not None

    assert requirement.specifier.contains(LocalWheelVersion((1, 4, 5, 9), "1.4.5.9"))
    assert not requirement.specifier.contains(LocalWheelVersion((1, 4, 6), "1.4.6"))


def test_quote_path_escapes_only_unsafe_bytes() -> None:
    assert _quote_path("/tmp/demo wheel.whl") == "/tmp/demo%20wheel.whl"


def _write_wheel(
    path: Path,
    *,
    purelib: bool,
    version: str = "1.0",
    requires_dist: tuple[str, ...] = (),
) -> None:
    name = path.name.split("-", 1)[0]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}/__init__.py",
            "",
        )
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            "Metadata-Version: 2.1\n"
            f"Name: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {dependency}\n" for dependency in requires_dist),
        )
        archive.writestr(
            f"{name}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\n",
        )
        archive.writestr(f"{name}-{version}.dist-info/RECORD", "")


def test_local_resolution_narrows_range_domain(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("1.0", "1.9", "2.0"):
        _write_wheel(
            wheelhouse / f"demo-{version}-py3-none-any.whl",
            purelib=True,
            version=version,
        )

    plan = resolve(
        [str(wheelhouse)],
        ["demo>=1.0,<2.0"],
        cache_dir=str(tmp_path / "cache"),
    )

    assert plan is not None
    assert [str(candidate.version) for candidate in plan.candidates] == ["1.9"]


def test_local_resolution_catalog_cache_invalidates_on_wheel_changes(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    cache = tmp_path / "cache"
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)

    first = resolve([str(wheelhouse)], ["demo"], cache_dir=str(cache))

    assert first is not None
    assert [str(candidate.version) for candidate in first.candidates] == ["1.0"]
    assert (cache / "fast-wheelhouse-catalog-v1.marshal").is_file()

    _write_wheel(wheelhouse / "demo-2.0-py3-none-any.whl", purelib=True, version="2.0")
    second = resolve([str(wheelhouse)], ["demo"], cache_dir=str(cache))

    assert second is not None
    assert [str(candidate.version) for candidate in second.candidates] == ["2.0"]


def test_local_resolution_recovers_from_corrupt_catalog_cache(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    cache = tmp_path / "cache"
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    assert resolve([str(wheelhouse)], ["demo"], cache_dir=str(cache)) is not None

    (cache / "fast-wheelhouse-catalog-v1.marshal").write_bytes(b"invalid")
    recovered = resolve([str(wheelhouse)], ["demo"], cache_dir=str(cache))

    assert recovered is not None
    assert [str(candidate.version) for candidate in recovered.candidates] == ["1.0"]


def test_local_resolution_preflights_exact_dependency_fanout(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(
        wheelhouse / "root-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("left==1.0", "right==1.0"),
    )
    _write_wheel(
        wheelhouse / "root-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("left==2.0", "right==2.0"),
    )
    _write_wheel(
        wheelhouse / "left-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared==1.0",),
    )
    _write_wheel(
        wheelhouse / "left-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared==2.0",),
    )
    _write_wheel(
        wheelhouse / "right-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared>=1.0,<2.0",),
    )
    _write_wheel(
        wheelhouse / "right-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared>=1.0,<2.0",),
    )
    for version in ("1.0", "2.0"):
        _write_wheel(
            wheelhouse / f"shared-{version}-py3-none-any.whl",
            purelib=True,
            version=version,
        )

    plan = resolve(
        [str(wheelhouse)],
        ["root"],
        cache_dir=str(tmp_path / "cache"),
    )

    assert plan is not None
    assert {
        (candidate.name, str(candidate.version)) for candidate in plan.candidates
    } == {
        ("root", "1.0"),
        ("left", "1.0"),
        ("right", "1.0"),
        ("shared", "1.0"),
    }


def test_fast_install_falls_back_for_non_pure_wheels(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=False)
    requirements = tmp_path / "requirements.in"
    requirements.write_text("demo\n", encoding="utf-8")
    target = tmp_path / "target"

    status = run_fast_install(
        [
            "--no-index",
            "--ignore-installed",
            "--no-compile",
            "--quiet",
            "--find-links",
            str(wheelhouse),
            "--target",
            str(target),
            "--cache-dir",
            str(tmp_path / "cache"),
            "-r",
            str(requirements),
        ]
    )

    assert status is None
    assert not target.exists()


def test_fast_install_preserves_normal_output_for_local_wheels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"

    status = run_fast_install(
        [
            "--no-index",
            "--ignore-installed",
            "--no-compile",
            "--find-links",
            str(wheelhouse),
            "--target",
            str(target),
            "demo",
        ]
    )

    assert status == 0
    output = capsys.readouterr().out
    assert f"Looking in links: {wheelhouse}" in output
    assert "Installing collected packages: demo" in output
    assert "Successfully installed demo-1.0" in output


def test_entrypoint_keeps_fallback_modules_out_of_fast_install(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    script = f"""
import sys
from pip.cli.entrypoint import main

assert main([
    "install", "--quiet", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "pip.cli.logging_config",
    "pip.cli._main_fallback",
    "pip.core.temp_dir",
    "pip.core.pip_version",
    "pip.install.runner",
    "pip.cli.commands.registry",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_keeps_fallback_modules_out_of_normal_local_install(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    script = f"""
import sys
from pip.cli.entrypoint import main

assert main([
    "install", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "pip.cli.logging_config",
    "pip.cli._main_fallback",
    "pip.cli.requirements",
    "pip.resolution.resolver",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_falls_back_for_non_pure_local_install(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=False)
    target = tmp_path / "target"
    script = f"""
from pip.cli.entrypoint import main

assert main([
    "install", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
"""

    subprocess.run([sys.executable, "-c", script], check=True)
    assert (target / "demo" / "__init__.py").is_file()


def test_install_command_module_does_not_import_build_stack() -> None:
    script = """
import sys
import pip.cli.commands.install

for module in (
    "pip.build.build",
    "pip.build.check",
    "pip.build.metadata",
    "pip.cli.requirements",
    "pip.core.wheel",
    "pip.index.provider",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_keeps_fallback_modules_out_of_help_and_version() -> None:
    script = """
import sys
from pip.cli.entrypoint import main

assert main(["--help"]) == 0
assert main(["--version"], version="26.2.dev0", location="/tmp/pip/__init__.py") == 0
for module in (
    "pip.cli.logging_config",
    "pip.cli._main_fallback",
    "pip.core.temp_dir",
    "pip.core.pip_version",
    "pip.install.runner",
    "pip.cli.commands.registry",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_command_help_does_not_load_install_runtime() -> None:
    script = """
import sys
from pip.cli.entrypoint import main

assert main(["install", "--help"]) == 0
for module in (
    "pip.build.build",
    "pip.build.check",
    "pip.build.metadata",
    "pip.cli.logging_config",
    "pip.core.temp_dir",
    "pip.index.provider",
    "pip.resolution.resolver",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)
