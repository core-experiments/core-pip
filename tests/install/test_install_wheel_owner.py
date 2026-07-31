import importlib.util
import os
import sysconfig
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cpip.core.errors import InstallationError
from cpip.core.metadata import default_lib_path
from cpip.core.wheel import wheel_candidate
from cpip.install.requirements import RequirementInstaller
from cpip.install.target import InstallTarget
from cpip.install.transaction import InstallTransaction
from cpip.install.wheel_transaction import (
    WheelInstaller,
    install_wheels_transactionally,
)


def install_wheel(
    path: Path,
    *,
    scheme: SimpleNamespace | None = None,
    pycompile: bool = True,
    script_executable: str | None = None,
    target: str | None = None,
    user: bool = False,
    root: str | None = None,
    prefix: str | None = None,
    requested: bool = False,
) -> object:
    install_target = (
        InstallTarget.from_scheme(scheme)
        if scheme is not None
        else InstallTarget.from_options(
            "owner-demo",
            target=target,
            user=user,
            root=root,
            prefix=prefix,
        )
    )
    return WheelInstaller(
        install_target,
        pycompile=pycompile,
        script_executable=script_executable,
    ).install(
        path,
        requested=requested,
    )


def make_wheel_internal(
    directory: Path,
    *,
    version: str = "1.0",
    extra_files: dict[str, str] | None = None,
    entry_points: str | None = None,
) -> Path:
    wheel = directory / f"owner_demo-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("owner_demo/__init__.py", f"VALUE = {version!r}\n")
        archive.writestr(
            f"owner_demo-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: owner-demo\nVersion: {version}\n",
        )
        archive.writestr(
            f"owner_demo-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if entry_points is not None:
            archive.writestr(
                f"owner_demo-{version}.dist-info/entry_points.txt", entry_points
            )
        for path, data in (extra_files or {}).items():
            archive.writestr(path, data)
        archive.writestr(f"owner_demo-{version}.dist-info/RECORD", "")
    return wheel


def test_install_and_uninstall_are_owned_by_cpip_install(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "site-packages"

    candidate = install_wheel(wheel, target=str(target), requested=True)

    assert candidate.name == "owner-demo"
    assert target.joinpath("owner_demo", "__init__.py").read_text() == "VALUE = '1.0'\n"
    assert (
        target.joinpath("owner_demo-1.0.dist-info", "INSTALLER").read_text() == "cpip\n"
    )
    assert RequirementInstaller().uninstall("owner-demo", paths=[str(target)])
    assert not target.joinpath("owner_demo").exists()


def test_uninstall_removes_recorded_files_and_generated_bytecode(
    tmp_path: Path,
) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "site-packages"
    install_wheel(wheel, target=str(target), requested=True, pycompile=False)
    unrelated = target / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    cache = Path(
        importlib.util.cache_from_source(str(target / "owner_demo" / "__init__.py"))
    )
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"bytecode")

    assert RequirementInstaller().uninstall("owner-demo", paths=[str(target)])
    assert not (target / "owner_demo").exists()
    assert not cache.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_uninstall_unlinks_symlinks_without_following_targets(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "site-packages"
    install_wheel(wheel, target=str(target), requested=True)
    symlink_target = tmp_path / "outside.txt"
    symlink_target.write_text("keep", encoding="utf-8")
    symlink = target / "owner-link"
    symlink.symlink_to(symlink_target)
    with (target / "owner_demo-1.0.dist-info" / "RECORD").open(
        "a", encoding="utf-8"
    ) as record:
        record.write("owner-link,,\n")

    assert RequirementInstaller().uninstall("owner-demo", paths=[str(target)])
    assert not os.path.lexists(symlink)
    assert symlink_target.read_text(encoding="utf-8") == "keep"


def test_uninstall_ignores_unsafe_record_paths(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "site-packages"
    install_wheel(wheel, target=str(target), requested=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    with (target / "owner_demo-1.0.dist-info" / "RECORD").open(
        "a", encoding="utf-8"
    ) as record:
        record.write(f"{outside},,\n")
        record.write("../outside.txt,,\n")

    assert RequirementInstaller().uninstall("owner-demo", paths=[str(target)])
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_requires_record(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "site-packages"
    install_wheel(wheel, target=str(target), requested=True)
    (target / "owner_demo-1.0.dist-info" / "RECORD").unlink()

    with pytest.raises(InstallationError, match="no RECORD file was found"):
        RequirementInstaller().uninstall("owner-demo", paths=[str(target)])
    assert (target / "owner_demo" / "__init__.py").exists()


def test_uninstall_missing_distribution_returns_false(tmp_path: Path) -> None:
    assert not RequirementInstaller().uninstall("missing", paths=[str(tmp_path)])


def test_install_target_places_package_in_target(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "target"

    install_wheel(wheel, target=str(target))

    assert (target / "owner_demo" / "__init__.py").exists()
    assert (target / "owner_demo-1.0.dist-info" / "METADATA").exists()


def test_install_root_relocates_default_library(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    root = tmp_path / "root"

    install_wheel(wheel, root=str(root))

    relocated = root / Path(*default_lib_path().parts[1:])
    assert (relocated / "owner_demo" / "__init__.py").exists()


def test_install_prefix_places_data_and_scripts_under_prefix(tmp_path: Path) -> None:
    wheel = make_wheel_internal(
        tmp_path,
        extra_files={"owner_demo.data/purelib/owner_demo/data.txt": "data"},
        entry_points="[console_scripts]\nowner-demo = owner_demo:main\n",
    )
    prefix = tmp_path / "prefix"

    install_wheel(wheel, prefix=str(prefix))

    vars = {"base": str(prefix), "platbase": str(prefix)}
    lib_dir = Path(sysconfig.get_path("purelib", vars=vars))
    scripts_dir = Path(sysconfig.get_path("scripts", vars=vars))
    assert (lib_dir / "owner_demo" / "data.txt").read_text() == "data"
    assert (scripts_dir / "owner-demo").exists()


def test_install_accepts_scheme_and_target_script_executable(tmp_path: Path) -> None:
    wheel = make_wheel_internal(
        tmp_path,
        extra_files={"owner_demo.data/data/share.txt": "data"},
        entry_points="[console_scripts]\nowner-demo = owner_demo:main\n",
    )
    scheme = SimpleNamespace(
        purelib=str(tmp_path / "purelib"),
        platlib=str(tmp_path / "platlib"),
        scripts=str(tmp_path / "scripts"),
        data=str(tmp_path / "data"),
        headers=str(tmp_path / "headers"),
    )

    install_wheel(
        wheel,
        scheme=scheme,
        pycompile=False,
        script_executable="/target/python",
    )

    assert (tmp_path / "data" / "share.txt").read_text() == "data"
    assert (
        (tmp_path / "scripts" / "owner-demo")
        .read_text()
        .startswith("#!/target/python\n")
    )


def test_install_upgrade_uninstalls_previous_version(tmp_path: Path) -> None:
    first = make_wheel_internal(tmp_path, version="1.0")
    second = make_wheel_internal(tmp_path, version="2.0")
    target = tmp_path / "target"

    install_wheel(first, target=str(target), requested=True)
    install_wheel(second, target=str(target), requested=True)

    assert not (target / "owner_demo-1.0.dist-info").exists()
    assert (target / "owner_demo-2.0.dist-info").exists()
    assert (target / "owner_demo" / "__init__.py").read_text() == "VALUE = '2.0'\n"


def test_batch_install_rolls_back_when_destinations_overlap(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path)
    target = tmp_path / "target"

    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_wheels_transactionally(
            [
                (wheel, True, None),
                (wheel, False, None),
                (wheel, False, None),
                (wheel, False, None),
            ],
            target=InstallTarget.from_options("owner-demo", target=str(target)),
            pycompile=False,
            lookup_existing=False,
        )

    assert not target.exists()


def test_large_fresh_batch_writes_without_staging(tmp_path: Path, monkeypatch) -> None:
    wheel = make_wheel_internal(
        tmp_path,
        extra_files={f"owner_demo/{index}.bin": "x" * (128 * 1024) for index in range(40)},
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / ".existing").touch()
    with zipfile.ZipFile(wheel) as archive:
        candidate = wheel_candidate(
            wheel,
            archive=archive,
            dist_info_dir="owner_demo-1.0.dist-info",
        )

    def fail_commit(*args: object, **kwargs: object) -> None:
        raise AssertionError("direct fresh installation should not commit staged files")

    monkeypatch.setattr(InstallTransaction, "commit", fail_commit)
    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options("owner-demo", target=str(target)),
        pycompile=False,
        lookup_existing=False,
        candidates=[candidate],
    )

    assert (target / ".existing").exists()
    assert (target / "owner_demo" / "0.bin").read_text() == "x" * (128 * 1024)


def test_direct_batch_rolls_back_final_writes_on_later_failure(
    tmp_path: Path, monkeypatch
) -> None:
    wheel = make_wheel_internal(
        tmp_path,
        extra_files={f"owner_demo/{index}.bin": "x" * (128 * 1024) for index in range(40)},
    )
    other = tmp_path / "other_demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr(
            "other_demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: other-demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "other_demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("other_demo/__init__.py", "\n")
        archive.writestr("other_demo-1.0.dist-info/RECORD", "")
    with zipfile.ZipFile(wheel) as archive:
        first = wheel_candidate(
            wheel,
            archive=archive,
            dist_info_dir="owner_demo-1.0.dist-info",
        )
    with zipfile.ZipFile(other) as archive:
        second = wheel_candidate(
            other,
            archive=archive,
            dist_info_dir="other_demo-1.0.dist-info",
        )
    target = tmp_path / "target"
    target.mkdir()
    (target / ".existing").touch()
    original_install = WheelInstaller.install
    calls = 0

    def fail_second(self, path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected direct-install failure")
        return original_install(self, path, *args, **kwargs)

    monkeypatch.setattr(WheelInstaller, "install", fail_second)
    with pytest.raises(RuntimeError, match="injected direct-install failure"):
        install_wheels_transactionally(
            [(wheel, True, None), (other, False, None)],
            target=InstallTarget.from_options("owner-demo", target=str(target)),
            pycompile=False,
            lookup_existing=False,
            candidates=[first, second],
        )

    assert (target / ".existing").exists()
    assert list(target.rglob("*.bin")) == []
    assert not (target / "owner_demo-1.0.dist-info" / "INSTALLER").exists()


def test_install_rejects_wheel_member_path_traversal(tmp_path: Path) -> None:
    wheel = make_wheel_internal(tmp_path, extra_files={"../escape.txt": "escape"})
    target = tmp_path / "target"

    with pytest.raises(InstallationError, match="outside the install destination"):
        install_wheel(wheel, target=str(target))
    assert not (tmp_path / "escape.txt").exists()


def test_install_rejects_wheel_member_symlink_escape(tmp_path: Path) -> None:
    wheel = make_wheel_internal(
        tmp_path, extra_files={"owner_demo/linked/escape.txt": "escape"}
    )
    target = tmp_path / "target"
    package = target / "owner_demo"
    package.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (package / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallationError, match="escapes installation root"):
        install_wheel(wheel, target=str(target))
    assert not (outside / "escape.txt").exists()


def test_batch_validation_rejects_shared_destinations(tmp_path: Path) -> None:
    first = make_wheel_internal(tmp_path, version="1.0")
    second = make_wheel_internal(tmp_path, version="2.0")
    target = InstallTarget.from_options("owner-demo", target=str(tmp_path / "target"))

    with pytest.raises(InstallationError, match="multiple wheels target"):
        WheelInstaller(target).validate_batch([first, second])


def test_install_rejects_entry_point_path_traversal(tmp_path: Path) -> None:
    wheel = make_wheel_internal(
        tmp_path,
        entry_points="[console_scripts]\n../escape = owner_demo:main\n",
    )
    prefix = tmp_path / "prefix"

    with pytest.raises(InstallationError, match="outside the scripts directory"):
        install_wheel(wheel, prefix=str(prefix))
