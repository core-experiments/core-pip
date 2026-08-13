from __future__ import annotations

from cpip_test_support import (
    CpipTestEnvironment,
    create_basic_wheel_for_package,
    create_test_package_with_setup,
)

from ....wheel_helpers import make_sdist


def test_cli_version_and_help(script: CpipTestEnvironment) -> None:
    version = script.cpip("--version")
    assert version.stdout.startswith("cpip ")
    assert " from " in version.stdout

    help_result = script.cpip("--help")
    assert "Usage:" in help_result.stdout
    assert "install" in help_result.stdout


def test_install_local_wheel_with_dependency(script: CpipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokedep", "1.0")
    create_basic_wheel_for_package(
        script,
        "smokeparent",
        "1.0",
        depends=["smokedep==1.0"],
    )

    result = script.cpip_install_local(
        "smokeparent==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokeparent-1.0 smokedep-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokedep", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokeparent", "__init__.py").is_file()


def test_install_requirements_and_already_satisfied(
    script: CpipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "smokereq", "1.0")
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text("smokereq==1.0\n", encoding="utf-8")

    first = script.cpip_install_local(
        "-r",
        requirements,
        find_links=script.scratch_path,
    )
    second = script.cpip_install_local(
        "-r",
        requirements,
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokereq-1.0" in first.stdout
    assert "Requirement already satisfied: smokereq==1.0" in second.stdout


def test_install_mixed_states_and_missing_package(script: CpipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokeinstalled", "1.0")
    create_basic_wheel_for_package(script, "smokemixeddep", "1.0")
    create_basic_wheel_for_package(
        script,
        "smokemixednew",
        "1.0",
        depends=["smokemixeddep==1.0"],
    )

    preinstall = script.cpip_install_local(
        "smokeinstalled==1.0",
        find_links=script.scratch_path,
    )
    mixed = script.cpip_install_local(
        "smokeinstalled==1.0",
        "smokemixednew==1.0",
        find_links=script.scratch_path,
    )
    missing = script.cpip_install_local(
        "smokeinstalled==1.0",
        "smokemisspelled",
        find_links=script.scratch_path,
        expect_error=True,
    )

    assert "Successfully installed smokeinstalled-1.0" in preinstall.stdout
    assert "Requirement already satisfied: smokeinstalled==1.0" in mixed.stdout
    assert "Successfully installed smokemixednew-1.0 smokemixeddep-1.0" in mixed.stdout
    assert script.site_packages_path.joinpath("smokemixeddep", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokemixednew", "__init__.py").is_file()

    assert missing.returncode == 1
    assert "No matching distribution found for smokemisspelled" in missing.stderr


def test_install_local_source_tree(script: CpipTestEnvironment) -> None:
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

    result = script.cpip(
        "install",
        "--no-index",
        project,
    )

    assert "Successfully installed smokesource-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokesource", "__init__.py").is_file()


def test_install_local_sdist_with_dependency(script: CpipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokesdistdep", "1.0")
    make_sdist(
        script.scratch_path,
        "smokesdist",
        "smokesdist",
        "1.0",
        requires=["smokesdistdep==1.0"],
    )

    result = script.cpip_install_local(
        "smokesdist==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokesdist-1.0 smokesdistdep-1.0" in result.stdout
    assert script.site_packages_path.joinpath("smokesdist", "__init__.py").is_file()
    assert script.site_packages_path.joinpath("smokesdistdep", "__init__.py").is_file()


def test_download_local_wheel(script: CpipTestEnvironment) -> None:
    wheel = create_basic_wheel_for_package(script, "smokedownload", "1.0")
    destination = script.scratch_path / "downloads"

    result = script.cpip(
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


def test_install_target_keeps_site_packages_clean(script: CpipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smoketarget", "1.0")
    target = script.scratch_path / "target"

    result = script.cpip_install_local(
        "--target",
        target,
        "smoketarget==1.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smoketarget-1.0" in result.stdout
    assert target.joinpath("smoketarget", "__init__.py").is_file()
    assert not script.site_packages_path.joinpath("smoketarget").exists()


def test_install_upgrade_and_force_reinstall(script: CpipTestEnvironment) -> None:
    package = script.site_packages_path / "smokeupgrade" / "__init__.py"
    create_basic_wheel_for_package(script, "smokeupgrade", "1.0")
    create_basic_wheel_for_package(script, "smokeupgrade", "2.0")

    first = script.cpip_install_local(
        "smokeupgrade==1.0",
        find_links=script.scratch_path,
    )
    upgraded = script.cpip_install_local(
        "--upgrade",
        "smokeupgrade",
        find_links=script.scratch_path,
    )
    package.write_text("# local mutation\n", encoding="utf-8")
    reinstalled = script.cpip_install_local(
        "--force-reinstall",
        "smokeupgrade==2.0",
        find_links=script.scratch_path,
    )

    assert "Successfully installed smokeupgrade-1.0" in first.stdout
    assert "Successfully installed smokeupgrade-2.0" in upgraded.stdout
    assert "Successfully installed smokeupgrade-2.0" in reinstalled.stdout
    assert "__version__ = '2.0'" in package.read_text(encoding="utf-8")


def test_install_unsatisfiable_dependency_fails_cleanly(
    script: CpipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(
        script,
        "smokeconflict",
        "1.0",
        depends=["smokemissing==99"],
    )

    result = script.cpip_install_local(
        "smokeconflict==1.0",
        find_links=script.scratch_path,
        expect_error=True,
    )

    assert result.returncode == 1
    assert "No matching distribution found for smokemissing==99" in result.stderr


def test_installed_state_commands(script: CpipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "smokestate", "1.0")
    script.cpip_install_local("smokestate==1.0", find_links=script.scratch_path)

    list_result = script.cpip("list")
    show_result = script.cpip("show", "smokestate")
    missing_show_result = script.cpip("show", "smokemissing", expect_error=True)
    freeze_result = script.cpip("freeze")

    assert "smokestate" in list_result.stdout
    assert "Name: smokestate" in show_result.stdout
    assert missing_show_result.returncode == 1
    assert "Package(s) not found: smokemissing" in missing_show_result.stderr
    assert "smokestate==1.0" in freeze_result.stdout

    uninstall_result = script.cpip("uninstall", "smokestate", "-y")
    assert "Successfully uninstalled smokestate" in uninstall_result.stdout
    assert not script.site_packages_path.joinpath("smokestate").exists()
