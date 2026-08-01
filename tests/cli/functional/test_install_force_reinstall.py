import os

from cpip_test_support import CpipTestEnvironment, assert_all_changes


def check_installed_version(
    script: CpipTestEnvironment, package: str, expected: str
) -> None:
    result = script.cpip("show", package)
    lines = result.stdout.splitlines()
    version = None
    for line in lines:
        if line.startswith("Version: "):
            version = line.split()[-1]
            break
    assert version == expected, f"version {version} != {expected}"


def check_force_reinstall(
    script: CpipTestEnvironment, specifier: str, expected: str
) -> None:
    """
    Args:
      specifier: the requirement specifier to force-reinstall.
      expected: the expected version after force-reinstalling.
    """
    result = script.cpip_install_local("simplewheel==1.0")
    check_installed_version(script, "simplewheel", "1.0")

    # Remove an installed file to test whether --force-reinstall fixes it.
    to_fix = script.site_packages_path.joinpath("simplewheel", "__init__.py")
    to_fix.unlink()

    result2 = script.cpip_install_local("--force-reinstall", specifier)
    check_installed_version(script, "simplewheel", expected)

    # site_packages_path is absolute, but files_created mapping uses
    # relative paths as key.
    fixed_key = os.path.relpath(to_fix, script.base_path)
    result2.did_create(fixed_key, message="force-reinstall failed")

    result3 = script.cpip("uninstall", "simplewheel", "-y")
    assert_all_changes(result, result3, [script.venv / "build", "cache"])


def test_force_reinstall_with_no_version_specifier(script: CpipTestEnvironment) -> None:
    """
    Check --force-reinstall when there is no version specifier and the
    installed version is not the newest version.
    """
    check_force_reinstall(script, "simplewheel", "2.0")


def test_force_reinstall_with_same_version_specifier(
    script: CpipTestEnvironment,
) -> None:
    """
    Check --force-reinstall when the version specifier equals the installed
    version and the installed version is not the newest version.
    """
    check_force_reinstall(script, "simplewheel==1.0", "1.0")
