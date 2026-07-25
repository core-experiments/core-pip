from __future__ import annotations

from pip_test_support import (
    PipTestEnvironment,
    create_basic_wheel_for_package,
    create_test_package_with_setup,
)
from ..wheel_helpers import make_sdist


def test_cli_version_and_help(script: PipTestEnvironment) -> None:
    version = script.pip("--version")
    assert version.stdout.startswith("pip ")
    assert " from " in version.stdout

    help_result = script.pip("--help")
    assert "Usage:" in help_result.stdout
    assert "install" in help_result.stdout


def test_install_local_wheel_with_dependency(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokedep", "1.0")
    create_basic_wheel_for_package(
        script,
        "smokeparent",
        "1.0",
        depends=["smokedep==1.0"],
    )

    result = script.pip_install_local(
        "smokeparent==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokedep-1.0 smokeparent-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokedep", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokeparent", "__init__.py").is_file()


def test_install_requirements_and_already_satisfied(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "smokereq", "1.0")
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text("smokereq==1.0\n", encoding="utf-8")

    first = script.pip_install_local(
        "-r",
        requirements,
        find_links=script.scratch_path,
    )
    second = script.pip_install_local(
        "-r",
        requirements,
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokereq-1.0" in first.stdout
    assert "Requirement already satisfied: smokereq==1.0" in second.stdout


def test_install_mixed_states_and_missing_package(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokeinstalled", "1.0")
    create_basic_wheel_for_package(script, "smokemixeddep", "1.0")
    create_basic_wheel_for_package(
        script,
        "smokemixednew",
        "1.0",
        depends=["smokemixeddep==1.0"],
    )

    preinstall = script.pip_install_local(
        "smokeinstalled==1.0",
        find_links=script.scratch_path,
    )
    mixed = script.pip_install_local(
        "smokeinstalled==1.0",
        "smokemixednew==1.0",
        find_links=script.scratch_path,
    )
    missing = script.pip_install_local(
        "smokeinstalled==1.0",
        "smokemisspelled",
        find_links=script.scratch_path,
        expect_error=True,
    )

    assert "Successfully installed smokeinstalled-1.0" in preinstall.stdout
    assert "Requirement already satisfied: smokeinstalled==1.0" in mixed.stdout
    assert "Successfully installed smokemixeddep-1.0 smokemixednew-1.0" in mixed.stdout
    assert script.site_packages_path.joinpath("smokemixeddep", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokemixednew", "__init__.py").is_file()

    assert missing.returncode == 1
    assert "No matching distribution found for smokemisspelled" in missing.stderr


def test_install_local_source_tree(script: PipTestEnvironment) -> None:
    project = create_test_package_with_setup(
        script,
        name="smokesource",
        version="1.0",
        packages=["smokesource"],
    )
    project.joinpath("smokesource").mkdir()
    project.joinpath("smokesource", "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    result = script.pip(
        "install",
        "--no-index",
        project,
    )

    assert "Successfully installed smokesource-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokesource", "__init__.py").is_file()


def test_install_local_sdist_with_dependency(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokesdistdep", "1.0")
    make_sdist(
        script.scratch_path,
        "smokesdist",
        "smokesdist",
        "1.0",
        requires=["smokesdistdep==1.0"],
    )

    result = script.pip_install_local(
        "smokesdist==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokesdistdep-1.0 smokesdist-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokesdist", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokesdistdep", "__init__.py").is_file()


def test_download_local_wheel(script: PipTestEnvironment) -> None:
    wheel = create_basic_wheel_for_package(script, "smokedownload", "1.0")
    destination = script.scratch_path / "downloads"

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        destination,
        "smokedownload==1.0",
    )

    assert "Successfully downloaded smokedownload" in result.stdout
    assert destination.joinpath(wheel.name).is_file()
    assert not script.site_packages_path.joinpath("smokedownload").exists()


def test_install_target_keeps_site_packages_clean(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smoketarget", "1.0")
    target = script.scratch_path / "target"

    result = script.pip_install_local(
        "--target",
        target,
        "smoketarget==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smoketarget-1.0" in result.stdout
    assert target.joinpath("smoketarget", "__init__.py").is_file()
    assert not script.site_packages_path.joinpath("smoketarget").exists()


def test_install_upgrade_and_force_reinstall(script: PipTestEnvironment) -> None:
    package = script.site_packages_path / "smokeupgrade" / "__init__.py"
    create_basic_wheel_for_package(script, "smokeupgrade", "1.0")
    create_basic_wheel_for_package(script, "smokeupgrade", "2.0")

    first = script.pip_install_local(
        "smokeupgrade==1.0",
        find_links=script.scratch_path,
    )
    upgraded = script.pip_install_local(
        "--upgrade",
        "smokeupgrade",
        find_links=script.scratch_path,
    )
    package.write_text("# local mutation\n", encoding="utf-8")
    reinstalled = script.pip_install_local(
        "--force-reinstall",
        "smokeupgrade==2.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokeupgrade-1.0" in first.stdout
    assert "Successfully installed smokeupgrade-2.0" in upgraded.stdout
    assert "Successfully installed smokeupgrade-2.0" in reinstalled.stdout
    assert "__version__ = '2.0'" in package.read_text(encoding="utf-8")


def test_install_unsatisfiable_dependency_fails_cleanly(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(
        script,
        "smokeconflict",
        "1.0",
        depends=["smokemissing==99"],
    )

    result = script.pip_install_local(
        "smokeconflict==1.0",
        find_links=script.scratch_path,
        expect_error=True,
    )

    assert result.returncode == 1
    assert "No matching distribution found for smokemissing==99" in result.stderr


def test_installed_state_commands(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokestate", "1.0")
    script.pip_install_local("smokestate==1.0", find_links=script.scratch_path)

    list_result = script.pip("list")
    show_result = script.pip("show", "smokestate")
    missing_show_result = script.pip("show", "smokemissing", expect_error=True)
    freeze_result = script.pip("freeze")

    assert "smokestate" in list_result.stdout
    assert "Name: smokestate" in show_result.stdout
    assert missing_show_result.returncode == 1
    assert "Package(s) not found: smokemissing" in missing_show_result.stderr
    assert "smokestate==1.0" in freeze_result.stdout

    uninstall_result = script.pip("uninstall", "smokestate", "-y")
    assert "Successfully uninstalled smokestate" in uninstall_result.stdout
    assert not script.site_packages_path.joinpath("smokestate").exists()
