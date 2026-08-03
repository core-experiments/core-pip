from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cpip.cli.fast_install import is_safe_member, run_cached_remote
from cpip.cli.fast_install import run as run_fast_install
from cpip.resolution.engine import ResolutionEngine
from cpip.resolution.engine.sources.wheelhouse.catalog import build_catalog_indexes
from cpip.resolution.engine.sources.wheelhouse.metadata import (
    parse_requirement,
    quote_path,
    read_wheel_metadata,
)
from cpip.resolution.engine.sources.wheelhouse.models import LocalWheelVersion


def test_local_wheel_version_caches_comparison_key() -> None:
    first = LocalWheelVersion((1, 0, 0), "1.0.0")
    second = LocalWheelVersion((1,), "1")

    assert first == second
    assert hash(first) == hash(second)
    assert not first < second


def test_catalog_indexes_reuse_the_same_records_snapshot() -> None:
    records = {
        "demo": [("/tmp/demo-1.0-py3-none-any.whl", LocalWheelVersion((1,), "1.0"))],
    }

    first = build_catalog_indexes(records)
    second = build_catalog_indexes(records)

    assert second is first


def test_compatible_release_keeps_original_precision() -> None:
    requirement = parse_requirement("demo~=1.4.5.0")
    assert requirement is not None

    assert requirement.specifier.contains(LocalWheelVersion((1, 4, 5, 9), "1.4.5.9"))
    assert not requirement.specifier.contains(LocalWheelVersion((1, 4, 6), "1.4.6"))


def test_quote_path_escapes_only_unsafe_bytes() -> None:
    assert quote_path("/tmp/demo wheel.whl") == "/tmp/demo%20wheel.whl"


def test_metadata_falls_back_when_filename_dist_info_differs(tmp_path: Path) -> None:
    wheel = tmp_path / "renamed-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "actual-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: actual\nVersion: 1.0\n",
        )

    metadata = read_wheel_metadata(str(wheel))

    assert metadata["name"] == ["actual"]


@pytest.mark.parametrize(
    "member, safe",
    [
        ("package/module.py", True),
        ("package/../module.py", False),
        ("../module.py", False),
        ("package/..", False),
        (".data/purelib/module.py", False),
        ("package..name/module.py", True),
    ],
)
def test_fast_install_member_safety(member: str, safe: bool) -> None:
    assert is_safe_member(member) is safe


def write_wheel(
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
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {'true' if purelib else 'false'}\n",
        )
        archive.writestr(f"{name}-{version}.dist-info/RECORD", "")


def test_local_resolution_narrows_range_domain(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("1.0", "1.9", "2.0"):
        write_wheel(
            wheelhouse / f"demo-{version}-py3-none-any.whl",
            purelib=True,
            version=version,
        )

    plan = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo>=1.0,<2.0"],
        cache_dir=str(tmp_path / "cache"),
    )

    assert plan is not None
    assert [str(candidate.version) for candidate in plan.candidates] == ["1.9"]


def test_local_resolution_declines_mixed_wheelhouse(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    (wheelhouse / "demo-2.0.tar.gz").write_bytes(b"not needed by compact resolution")

    plan = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(tmp_path / "cache"),
    )

    assert plan is None


def test_local_resolution_catalog_cache_invalidates_on_wheel_changes(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    cache = tmp_path / "cache"
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)

    first = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(cache),
    )

    assert first is not None
    assert [str(candidate.version) for candidate in first.candidates] == ["1.0"]
    assert (cache / "fast-wheelhouse-catalog-v1.marshal").is_file()

    write_wheel(wheelhouse / "demo-2.0-py3-none-any.whl", purelib=True, version="2.0")
    second = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(cache),
    )

    assert second is not None
    assert [str(candidate.version) for candidate in second.candidates] == ["2.0"]


def test_local_resolution_candidate_cache_invalidates_on_wheel_rewrite(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    cache = tmp_path / "cache"
    path = wheelhouse / "demo-1.0-py3-none-any.whl"
    write_wheel(path, purelib=True)
    write_wheel(wheelhouse / "shared-1.0-py3-none-any.whl", purelib=True)

    first = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(cache),
    )
    assert first is not None
    assert first.candidates[0].dependencies == ()

    previous = path.stat()
    write_wheel(path, purelib=True, requires_dist=("shared==1.0",))
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    second = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(cache),
    )

    assert second is not None
    assert [dependency.name for dependency in second.candidates[0].dependencies] == [
        "shared",
    ]


def test_local_resolution_recovers_from_corrupt_catalog_cache(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    cache = tmp_path / "cache"
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    assert (
        ResolutionEngine.resolve_wheelhouse(
            [str(wheelhouse)],
            ["demo"],
            cache_dir=str(cache),
        )
        is not None
    )

    (cache / "fast-wheelhouse-catalog-v1.marshal").write_bytes(b"invalid")
    recovered = ResolutionEngine.resolve_wheelhouse(
        [str(wheelhouse)],
        ["demo"],
        cache_dir=str(cache),
    )

    assert recovered is not None
    assert [str(candidate.version) for candidate in recovered.candidates] == ["1.0"]


def test_local_resolution_preflights_exact_dependency_fanout(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(
        wheelhouse / "root-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("left==1.0", "right==1.0"),
    )
    write_wheel(
        wheelhouse / "root-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("left==2.0", "right==2.0"),
    )
    write_wheel(
        wheelhouse / "left-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared==1.0",),
    )
    write_wheel(
        wheelhouse / "left-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared==2.0",),
    )
    write_wheel(
        wheelhouse / "right-1.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared>=1.0,<2.0",),
    )
    write_wheel(
        wheelhouse / "right-2.0-py3-none-any.whl",
        purelib=True,
        requires_dist=("shared>=1.0,<2.0",),
    )
    for version in ("1.0", "2.0"):
        write_wheel(
            wheelhouse / f"shared-{version}-py3-none-any.whl",
            purelib=True,
            version=version,
        )

    plan = ResolutionEngine.resolve_wheelhouse(
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
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=False)
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
        ],
    )

    assert status is None
    assert not target.exists()


def test_fast_install_falls_back_for_wheel_data_schemes(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    write_wheel(wheel, purelib=True)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("demo-1.0.data/data/demo.conf", "value=true\n")
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
            "demo",
        ],
    )

    assert status is None
    assert not target.exists()


def test_fast_install_reuses_warm_wheel_metadata_cache(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    requirements = tmp_path / "requirements.in"
    requirements.write_text("demo\n", encoding="utf-8")
    cache = tmp_path / "cache"

    arguments = [
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--quiet",
        "--find-links",
        str(wheelhouse),
        "-r",
        str(requirements),
        "--cache-dir",
        str(cache),
    ]
    first = run_fast_install([*arguments, "--target", str(tmp_path / "first")])
    second = run_fast_install([*arguments, "--target", str(tmp_path / "second")])

    assert first == 0
    assert second == 0
    assert (cache / "fast-install-v3.marshal").is_file()


def test_fast_install_metadata_cache_persists_verified_digest(tmp_path: Path) -> None:
    from cpip.cli.fast_install_cache import FastInstallMetadataCache

    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    cache_root = tmp_path / "cache"
    cache = FastInstallMetadataCache(cache_root)
    identity = cache.identity(str(wheel))
    assert identity is not None
    digest = "a" * 64

    cache.put(identity, (("dependency==1",), True))
    cache.put_digest(identity, digest, (("dependency==1",), True))
    cache.flush()

    restored = FastInstallMetadataCache(cache_root)
    assert restored.get(identity) == (("dependency==1",), True)
    assert restored.get_digest(identity) == digest


def test_fast_install_reuses_warm_resolved_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    cache = tmp_path / "cache"
    arguments = [
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--quiet",
        "--find-links",
        str(wheelhouse),
        "demo",
        "--cache-dir",
        str(cache),
    ]

    assert run_fast_install([*arguments, "--target", str(tmp_path / "first")]) == 0
    monkeypatch.setattr(
        "cpip.cli.fast_install.wheel_metadata",
        lambda *_args, **_kwargs: pytest.fail("warm plan rescanned wheel metadata"),
    )

    assert run_fast_install([*arguments, "--target", str(tmp_path / "second")]) == 0


def test_fast_install_reuses_copy_on_write_install_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    cache = tmp_path / "cache"
    arguments = [
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--quiet",
        "--find-links",
        str(wheelhouse),
        "demo",
        "--cache-dir",
        str(cache),
    ]

    assert run_fast_install([*arguments, "--target", str(tmp_path / "first")]) == 0
    assert not (cache / "fast-install-trees-v1").exists()
    assert run_fast_install([*arguments, "--target", str(tmp_path / "second")]) == 0
    assert (cache / "fast-install-trees-v1").is_dir()
    monkeypatch.setattr(
        "cpip.cli.fast_install.install_resolved_pure_wheels",
        lambda *_args, **_kwargs: pytest.fail("warm install did not clone its tree"),
    )

    third = tmp_path / "third"
    assert run_fast_install([*arguments, "--target", str(third)]) == 0
    (third / "demo" / "__init__.py").write_text("changed", encoding="utf-8")
    fourth = tmp_path / "fourth"
    assert run_fast_install([*arguments, "--target", str(fourth)]) == 0
    assert (fourth / "demo" / "__init__.py").read_text(encoding="utf-8") == ""


def test_fast_install_invalidates_plan_when_wheelhouse_changes(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(
        wheelhouse / "demo-1.0-py3-none-any.whl",
        purelib=True,
        version="1.0",
    )
    cache = tmp_path / "cache"
    arguments = [
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--quiet",
        "--find-links",
        str(wheelhouse),
        "demo",
        "--cache-dir",
        str(cache),
    ]

    assert run_fast_install([*arguments, "--target", str(tmp_path / "first")]) == 0
    assert run_fast_install([*arguments, "--target", str(tmp_path / "second")]) == 0
    assert (cache / "fast-install-trees-v1").is_dir()
    write_wheel(
        wheelhouse / "demo-2.0-py3-none-any.whl",
        purelib=True,
        version="2.0",
    )

    assert run_fast_install([*arguments, "--target", str(tmp_path / "third")]) == 0
    assert (tmp_path / "third" / "demo-2.0.dist-info").is_dir()


def test_fast_install_invalidates_plan_when_selected_wheel_changes(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    write_wheel(wheel, purelib=True)
    cache = tmp_path / "cache"
    arguments = [
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--quiet",
        "--find-links",
        str(wheelhouse),
        "demo",
        "--cache-dir",
        str(cache),
    ]

    assert run_fast_install([*arguments, "--target", str(tmp_path / "first")]) == 0
    assert run_fast_install([*arguments, "--target", str(tmp_path / "second")]) == 0
    assert (cache / "fast-install-trees-v1").is_dir()
    old_mtime = wheel.stat().st_mtime_ns
    write_wheel(wheel, purelib=True, requires_dist=("missing",))
    os.utime(wheel, ns=(old_mtime + 1, old_mtime + 1))

    target = tmp_path / "third"
    assert run_fast_install([*arguments, "--target", str(target)]) is None
    assert not target.exists()


def test_fast_install_reuses_validated_remote_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cpip.core.wheel import wheel_candidate
    from cpip.install.wheel_archive_cache import (
        exact_install_plan_key_from_strings,
        prepare_cached_wheel,
        save_cached_install_plan,
    )

    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    write_wheel(wheel, purelib=True)
    cache = tmp_path / "cache"
    candidate = wheel_candidate(str(wheel)).copy_with(source_kind="wheel")
    prepare_cached_wheel(candidate, str(cache))
    context = (
        "remote-exact-v1",
        "https://pypi.org/simple",
        (),
        (),
        None,
        f"{sys.version_info.major}{sys.version_info.minor}",
        (),
        "only-if-needed",
        False,
    )
    keyed = exact_install_plan_key_from_strings(("demo==1.0",), context)
    assert keyed is not None
    assert save_cached_install_plan(str(cache), keyed[0], (candidate,), {})
    monkeypatch.setattr(
        "cpip.cli.fast_install._remote_index_url",
        lambda: "https://pypi.org/simple",
    )
    target = tmp_path / "target"

    status = run_cached_remote(
        [
            "--quiet",
            "--ignore-installed",
            "--no-compile",
            "--cache-dir",
            str(cache),
            "--target",
            str(target),
            "demo==1.0",
        ],
    )

    assert status == 0
    assert (target / "demo" / "__init__.py").is_file()


def test_fast_install_skips_resolution_for_nonempty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    requirements = tmp_path / "requirements.in"
    requirements.write_text("demo\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / ".existing").touch()

    def fail_resolution(*args: object, **kwargs: object) -> object:
        raise AssertionError("fast resolver should not run for a non-empty target")

    monkeypatch.setattr(
        ResolutionEngine,
        "resolve_wheelhouse",
        staticmethod(fail_resolution),
    )
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
            "-r",
            str(requirements),
        ],
    )

    assert status is None


def test_fast_resolution_defers_wheel_validation_to_install(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )

    plan = ResolutionEngine.resolve_wheelhouse([str(wheelhouse)], ["demo"])

    assert plan is not None
    assert (
        run_fast_install(
            [
                "--no-index",
                "--ignore-installed",
                "--no-compile",
                "--quiet",
                "--find-links",
                str(wheelhouse),
                "--target",
                str(tmp_path / "target"),
                "demo",
            ],
        )
        is None
    )


def test_fast_resolution_falls_back_for_nonstandard_metadata_path(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "custom-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )

    plan = ResolutionEngine.resolve_wheelhouse([str(wheelhouse)], ["demo"])

    assert plan is not None
    assert plan.candidates[0].name == "demo"


def test_fast_install_preserves_normal_output_for_local_wheels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
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
        ],
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
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main([
    "install", "--quiet", "--upgrade", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "cpip.cli.logging_config",
    "cpip.cli._main_fallback",
    "cpip.core.temp_dir",
    "cpip.core.cpip_version",
    "cpip.install.runner",
    "cpip.cli.commands.registry",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_keeps_fallback_modules_out_of_normal_local_install(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main([
    "install", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "cpip.cli.logging_config",
    "cpip.cli._main_fallback",
    "cpip.cli.requirements",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_uses_light_local_fallback_for_nonempty_target(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / ".existing").touch()
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main([
    "install", "--quiet", "--upgrade", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "cpip.cli._main_fallback",
    "cpip.cli.commands.registry",
    "cpip.cli.requirements",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)
    assert (target / "demo" / "__init__.py").exists()


def test_entrypoint_uses_light_local_fallback_for_exact_upgrade(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    write_wheel(
        wheelhouse / "demo-2.0-py3-none-any.whl",
        purelib=True,
        version="2.0",
    )
    target = tmp_path / "target"
    cache = tmp_path / "cache"
    assert (
        run_fast_install(
            [
                "--quiet",
                "--no-index",
                "--ignore-installed",
                "--no-compile",
                "--cache-dir",
                str(cache),
                "--target",
                str(target),
                "--find-links",
                str(wheelhouse),
                "demo==1.0",
            ],
        )
        == 0
    )
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main([
    "install", "--quiet", "--upgrade", "--no-index", "--no-compile",
    "--cache-dir", {str(cache)!r}, "--target", {str(target)!r},
    "--find-links", {str(wheelhouse)!r}, "demo==2.0",
]) == 0
for module in (
    "cpip.cli._main_fallback",
    "cpip.cli.commands.registry",
    "cpip.cli.requirements",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)
    assert (target / "demo" / "__init__.py").exists()
    assert (target / "demo-2.0.dist-info").exists()


def test_entrypoint_keeps_upgrade_on_empty_target_on_fast_path(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=True)
    target = tmp_path / "target"
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main([
    "install", "--no-index", "--ignore-installed", "--no-compile",
    "--target", {str(target)!r}, "--find-links", {str(wheelhouse)!r}, "demo",
]) == 0
for module in (
    "cpip.cli.logging_config",
    "cpip.cli._main_fallback",
    "cpip.cli.requirements",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_falls_back_for_non_pure_local_install(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_wheel(wheelhouse / "demo-1.0-py3-none-any.whl", purelib=False)
    target = tmp_path / "target"
    script = f"""
from cpip.cli.entrypoint import main

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
import cpip.cli.commands.install

for module in (
    "cpip.build.build",
    "cpip.build.check",
    "cpip.build.metadata",
    "cpip.cli.requirements",
    "cpip.core.wheel",
    "cpip.index.provider",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_entrypoint_keeps_fallback_modules_out_of_help_and_version() -> None:
    script = """
import sys
from cpip.cli.entrypoint import main

assert main(["--help"]) == 0
assert main(["--version"], version="0.0.1", location="/tmp/cpip/__init__.py") == 0
for module in (
    "cpip.cli.logging_config",
    "cpip.cli._main_fallback",
    "cpip.core.temp_dir",
    "cpip.core.cpip_version",
    "cpip.install.runner",
    "cpip.cli.commands.registry",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_command_help_does_not_load_install_runtime() -> None:
    script = """
import sys
from cpip.cli.entrypoint import main

assert main(["install", "--help"]) == 0
for module in (
    "cpip.build.build",
    "cpip.build.check",
    "cpip.build.metadata",
    "cpip.cli.logging_config",
    "cpip.core.temp_dir",
    "cpip.index.provider",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.parametrize(
    "command",
    [
        "install",
        "wheel",
        "index",
        "download",
        "uninstall",
        "list",
        "freeze",
        "show",
        "inspect",
        "check",
        "lock",
    ],
)
def test_command_help_does_not_load_runtime_stack(command: str) -> None:
    script = f"""
import sys
from cpip.cli.entrypoint import main

assert main(["help", {command!r}]) == 0
for module in (
    "cpip.build.build",
    "cpip.build.metadata",
    "cpip.index.provider",
    "cpip.network.http",
    "cpip.install.wheel_transaction",
    "cpip.vcs.bazaar",
    "cpip.vcs.git",
    "cpip.vcs.mercurial",
    "cpip.vcs.subversion",
):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


def test_versioncontrol_defers_builtin_backend_imports() -> None:
    script = """
import sys
from cpip.vcs.versioncontrol import vcs

for module in (
    "cpip.vcs.bazaar",
    "cpip.vcs.git",
    "cpip.vcs.mercurial",
    "cpip.vcs.subversion",
):
    assert module not in sys.modules, module

assert vcs.get_backend_for_scheme("git+https") is not None
for module in (
    "cpip.vcs.bazaar",
    "cpip.vcs.git",
    "cpip.vcs.mercurial",
    "cpip.vcs.subversion",
):
    assert module in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True)
