import json
import os
from pathlib import Path
from venv import EnvBuilder

from cpip_test_support import CpipTestEnvironment, TestData


def test_python_interpreter(
    script: CpipTestEnvironment,
    tmpdir: Path,
    shared_data: TestData,
) -> None:
    env_path = os.fspath(tmpdir / "venv")
    env = EnvBuilder(with_pip=False)
    env.create(env_path)

    result = script.cpip("--python", env_path, "list", "--format=json")
    before = json.loads(result.stdout)

    # Ideally we would assert that before==[], but there's a problem in CI
    # that means this isn't true. See https://github.com/pypa/cpip/pull/11326
    # for details.

    script.cpip(
        "--python",
        env_path,
        "install",
        "-f",
        shared_data.find_links,
        "--no-index",
        "simplewheel==1.0",
    )

    result = script.cpip("--python", env_path, "list", "--format=json")
    installed = json.loads(result.stdout)
    assert {"name": "simplewheel", "version": "1.0"} in installed

    script.cpip("--python", env_path, "uninstall", "simplewheel", "--yes")
    result = script.cpip("--python", env_path, "list", "--format=json")
    assert json.loads(result.stdout) == before


def test_error_python_option_wrong_location(
    script: CpipTestEnvironment,
    tmpdir: Path,
    shared_data: TestData,
) -> None:
    env_path = os.fspath(tmpdir / "venv")
    env = EnvBuilder(with_pip=False)
    env.create(env_path)

    script.cpip("list", "--python", env_path, "--format=json", expect_error=True)
