from __future__ import annotations

import os
import site
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager
from textwrap import dedent
from typing import Literal

import pytest
from cpip.install.build_env.base import (
    BuildEnvironment,
    BuildEnvironmentInstaller,
)
from cpip.install.build_env.installer import (
    InprocessBuildEnvironmentInstaller,
)
from cpip.install.build_env.venv import VenvBuildEnvironment
from cpip.build.tracker import get_build_tracker
from cpip.build.cache import WheelCache
from cpip_test_support import (
    CpipTestEnvironment,
    TestData,
    TestCpipResult,
    create_basic_wheel_for_package,
    make_test_build_options,
)
from cpip_test_support.wheel import make_wheel

InstallMethod = Literal["inprocess"]
IsolationMethod = Literal["venv"]
with_both_installers = pytest.mark.parametrize("install_method", ["inprocess"])
with_both_isolation_methods = pytest.mark.parametrize("isolation_method", ["venv"])


def indent(text: str, prefix: str) -> str:
    return "\n".join((prefix if line else "") + line for line in text.split("\n"))


@pytest.fixture(params=["venv"])
def env_factory(request: pytest.FixtureRequest) -> type[BuildEnvironment]:
    return VenvBuildEnvironment


@contextmanager
def make_test_build_env_installer(
    method: InstallMethod, options: object
) -> Generator[BuildEnvironmentInstaller]:
    assert method == "inprocess"
    with get_build_tracker() as tracker:
        yield InprocessBuildEnvironmentInstaller(
            options=options,  # type: ignore[arg-type]
            build_tracker=tracker,
            wheel_cache=WheelCache(None),  # type: ignore
        )


def run_with_build_env(
    script: CpipTestEnvironment,
    setup_script_contents: str,
    test_script_contents: str | None = None,
    install_method: InstallMethod = "inprocess",
    isolation_method: IsolationMethod = "venv",
) -> TestCpipResult:
    build_env_script = script.scratch_path / "build_env.py"
    scratch_path = str(script.scratch_path)
    build_env_script.write_text(
        dedent(f"""
            import subprocess
            import sys

            from cpip.install.build_env.venv import VenvBuildEnvironment
            from cpip.install.build_env.installer import (
                BuildConfiguration,
                InprocessBuildEnvironmentInstaller,
            )
            from cpip.build.cache import WheelCache
            from cpip.build.tracker import get_build_tracker
            from cpip.network.http import NetworkSession
            from cpip.core.temp_dir import global_tempdir_manager

            session = NetworkSession()
            options = BuildConfiguration(
                session=session,
                find_links=[{scratch_path!r}],
            )

            with global_tempdir_manager(), get_build_tracker() as tracker:
                installer = InprocessBuildEnvironmentInstaller(
                    options=options,
                    build_tracker=tracker,
                    wheel_cache=WheelCache(None),
                )
                assert "{isolation_method}" == "venv"
                build_env = VenvBuildEnvironment(installer)
            """)
        + indent(dedent(setup_script_contents), "    ")
        + indent(
            dedent("""
                with build_env:
                    if len(sys.argv) > 1:
                        subprocess.check_call((
                            build_env.python_executable, sys.argv[1]
                        ))
                """),
            "    ",
        )
    )
    args = ["python", os.fspath(build_env_script)]
    if test_script_contents is not None:
        test_script = script.scratch_path / "test.py"
        test_script.write_text(dedent(test_script_contents))
        args.append(os.fspath(test_script))
    return script.run(*args)


@with_both_installers
def test_build_env_allow_empty_requirements_install(
    install_method: InstallMethod, env_factory: type[BuildEnvironment]
) -> None:
    options = make_test_build_options()
    with make_test_build_env_installer(install_method, options) as installer:
        build_env = env_factory(installer)
        for prefix in ("normal", "overlay"):
            build_env.install_requirements(
                [], prefix, kind="Installing build dependencies"
            )


def test_build_env_restores_unset_path_variables(
    monkeypatch: pytest.MonkeyPatch, env_factory: type[BuildEnvironment]
) -> None:
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)

    with make_test_build_env_installer(
        "inprocess", make_test_build_options()
    ) as installer:
        with env_factory(installer):
            pass

    assert "PATH" not in os.environ
    assert "PYTHONPATH" not in os.environ


@with_both_isolation_methods
def test_build_env_requirements_check(
    script: CpipTestEnvironment, isolation_method: IsolationMethod
) -> None:
    create_basic_wheel_for_package(script, "foo", "2.0")
    create_basic_wheel_for_package(script, "bar", "1.0")
    create_basic_wheel_for_package(script, "bar", "3.0")
    create_basic_wheel_for_package(script, "other", "0.5")

    script.cpip_install_local("-f", script.scratch_path, "foo", "bar", "other")

    run_with_build_env(
        script,
        """
        r = build_env.check_requirements(['foo', 'bar', 'other'])
        assert r == (set(), {'foo', 'bar', 'other'}), repr(r)

        r = build_env.check_requirements(['foo>1.0', 'bar==3.0'])
        assert r == (set(), {'foo>1.0', 'bar==3.0'}), repr(r)

        r = build_env.check_requirements(['foo>3.0', 'bar>=2.5'])
        assert r == (set(), {'foo>3.0', 'bar>=2.5'}), repr(r)
        """,
        isolation_method=isolation_method,
    )

    run_with_build_env(
        script,
        """
        build_env.install_requirements(['foo', 'bar==3.0'], 'normal',
                                       kind='installing foo in normal')

        r = build_env.check_requirements(['foo', 'bar', 'other'])
        assert r == (set(), {'other'}), repr(r)

        r = build_env.check_requirements(['foo>1.0', 'bar==3.0'])
        assert r == (set(), set()), repr(r)

        r = build_env.check_requirements(['foo>3.0', 'bar>=2.5'])
        assert r == ({('foo==2.0', 'foo>3.0')}, set()), repr(r)
        """,
        isolation_method=isolation_method,
    )

    run_with_build_env(
        script,
        """
        build_env.install_requirements(['foo', 'bar==3.0'], 'normal',
                                       kind='installing foo in normal')
        build_env.install_requirements(['bar==1.0'], 'overlay',
                                       kind='installing foo in overlay')

        r = build_env.check_requirements(['foo', 'bar', 'other'])
        assert r == (set(), {'other'}), repr(r)

        r = build_env.check_requirements(['foo>1.0', 'bar==3.0'])
        assert r == ({('bar==1.0', 'bar==3.0')}, set()), repr(r)

        r = build_env.check_requirements(['foo>3.0', 'bar>=2.5'])
        assert r == ({('bar==1.0', 'bar>=2.5'), ('foo==2.0', 'foo>3.0')}, \
            set()), repr(r)
        """,
        isolation_method=isolation_method,
    )

    run_with_build_env(
        script,
        """
        build_env.install_requirements(
            ["bar==3.0"],
            "normal",
            kind="installing bar in normal",
        )
        r = build_env.check_requirements(
            [
                "bar==2.0; python_version < '3.0'",
                "bar==3.0; python_version >= '3.0'",
                "foo==4.0; extra == 'dev'",
            ],
        )
        assert r == (set(), set()), repr(r)
        """,
        isolation_method=isolation_method,
    )


if sys.version_info < (3, 12):
    BUILD_ENV_ERROR_DEBUG_CODE = r"""
            from distutils.sysconfig import get_python_lib
            print(
                f'imported `pkg` from `{pkg.__file__}`',
                file=sys.stderr)
            print('system sites:\n  ' + '\n  '.join(sorted({
                            get_python_lib(plat_specific=0),
                            get_python_lib(plat_specific=1),
                    })), file=sys.stderr)
    """
else:
    BUILD_ENV_ERROR_DEBUG_CODE = r"""
            from sysconfig import get_paths
            paths = get_paths()
            print(
                f'imported `pkg` from `{pkg.__file__}`',
                file=sys.stderr)
            print('system sites:\n  ' + '\n  '.join(sorted({
                            paths['platlib'],
                            paths['purelib'],
                    })), file=sys.stderr)
    """


@with_both_installers
@with_both_isolation_methods
@pytest.mark.usefixtures("enable_user_site")
def test_build_env_isolation(
    script: CpipTestEnvironment,
    install_method: InstallMethod,
    isolation_method: IsolationMethod,
) -> None:
    # Create dummy `pkg` wheel.
    pkg_whl = create_basic_wheel_for_package(script, "pkg", "1.0")

    # Install it to site packages.
    script.cpip_install_local(pkg_whl)

    # And a copy in the user site.
    script.cpip_install_local("--ignore-installed", "--user", pkg_whl)

    # And to another directory available through a .pth file.
    target = script.scratch_path / "pth_install"
    script.cpip_install_local("-t", target, pkg_whl)
    (script.site_packages_path / "build_requires.pth").write_text(str(target) + "\n")

    # And finally to yet another directory available through PYTHONPATH.
    target = script.scratch_path / "pypath_install"
    script.cpip_install_local("-t", target, pkg_whl)
    script.environ["PYTHONPATH"] = target

    system_sites = {os.path.normcase(path) for path in site.getsitepackages()}
    # there should always be something to exclude
    assert system_sites

    run_with_build_env(
        script,
        "",
        f"""
        import sys

        try:
            import pkg
        except ImportError:
            pass
        else:
            {BUILD_ENV_ERROR_DEBUG_CODE}
            print('sys.path:\\n  ' + '\\n  '.join(sys.path), file=sys.stderr)
            sys.exit(1)
        # second check: direct check of exclusion of system site packages
        import os

        normalized_path = [os.path.normcase(path) for path in sys.path]
        for system_path in {system_sites!r}:
            assert system_path not in normalized_path, \
            f"{{system_path}} found in {{normalized_path}}"
        """,
        install_method=install_method,
        isolation_method=isolation_method,
    )


def test_build_env_can_still_access_python_tools_on_system_path(
    script: CpipTestEnvironment, data: TestData
) -> None:
    """
    Ensure that backend subprocesses can still run system Python tools available
    on PATH and that those tools can import their own dependencies from the system
    Python.

    This is a regression test for https://github.com/pypa/cpip/issues/13222 where
    our legacy sitecustomize.py trick for achieving isolation broke this use-case.
    """
    if shutil.which("cmake") is None:
        pytest.skip("requires a system cmake executable")
    script.cpip_install_local(
        data.src / "python-cmake-issue-13222",
        "--use-feature=venv-isolation",
        find_links=[data.common_wheels],
        build_isolation=True,
    )


@with_both_installers
def test_build_env_console_scripts_use_venv_python(
    script: CpipTestEnvironment, install_method: InstallMethod
) -> None:
    """
    When using venv isolation, it's important that the build environment
    console scripts are linked with the temporary environment's Python
    executable (and not the parent executable).
    """
    make_wheel(
        name="goldfish",
        version="1.0",
        extra_files={
            "goldfish/__init__.py": dedent("""
                def main():
                    print('hello, world')
            """),
        },
        console_scripts=["goldfish = goldfish:main"],
    ).save_to(script.scratch_path / "goldfish-1.0-py2.py3-none-any.whl")

    options = make_test_build_options(find_links=[os.fspath(script.scratch_path)])
    with make_test_build_env_installer(install_method, options) as installer:
        build_env = VenvBuildEnvironment(installer)
        build_env.install_requirements(
            ["goldfish==1.0"], "normal", kind="script dependency"
        )

    # Check that the console script import its own library.
    console_script = shutil.which("goldfish", path=build_env.bin_path_internal)
    assert console_script is not None, "console script wasn't found?!"
    result = script.run(console_script)
    assert result.stdout == "hello, world\n"
