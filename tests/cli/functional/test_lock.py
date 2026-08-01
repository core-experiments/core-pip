import textwrap
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import tomllib
from cpip.core.urls import path_to_url
from cpip_test_support import CpipTestEnvironment, TestData


def expected_simplewheel_lock(
    shared_data: TestData,
    wheel_name: str,
) -> dict[str, object]:
    wheel = shared_data.root.joinpath("packages", wheel_name)
    assert wheel.is_file()
    return {
        "name": wheel.name,
        "url": path_to_url(str(wheel)),
        "hashes": {"sha256": sha256(wheel.read_bytes()).hexdigest()},
    }


def test_lock_wheel_from_findlinks(
    script: CpipTestEnvironment,
    shared_data: TestData,
    tmp_path: Path,
) -> None:
    """Test locking a simple wheel package, to the default pylock.toml."""
    result = script.cpip(
        "lock",
        "simplewheel==2.0",
        "--no-index",
        "--find-links",
        str(shared_data.root / "packages/"),
        expect_stderr=True,  # for the experimental warning
    )
    result.did_create(Path("scratch") / "pylock.toml")
    pylock = tomllib.loads(script.scratch_path.joinpath("pylock.toml").read_text())
    wheel_name = pylock["packages"][0]["wheels"][0]["name"]
    assert pylock == {
        "created-by": "cpip",
        "lock-version": "1.0",
        "packages": [
            {
                "name": "simplewheel",
                "version": "2.0",
                "wheels": [
                    {
                        **expected_simplewheel_lock(shared_data, wheel_name),
                    },
                ],
            },
        ],
    }


def test_lock_sdist_from_findlinks(
    script: CpipTestEnvironment,
    shared_data: TestData,
) -> None:
    """Test locking a simple wheel package, to the default pylock.toml."""
    result = script.cpip(
        "lock",
        "--no-build-isolation",
        "simple==2.0",
        "--no-binary=simple",
        "--quiet",
        "--output=-",
        "--no-index",
        "--find-links",
        str(shared_data.root / "packages/"),
        expect_stderr=True,  # for the experimental warning
    )
    pylock = tomllib.loads(result.stdout)
    assert pylock["packages"] == [
        {
            "name": "simple",
            "sdist": {
                "hashes": {
                    "sha256": (
                        "3a084929238d13bcd3bb928af04f3bac"
                        "7ca2357d419e29f01459dc848e2d69a4"
                    ),
                },
                "name": "simple-2.0.tar.gz",
                "url": path_to_url(
                    str(shared_data.root / "packages" / "simple-2.0.tar.gz"),
                ),
            },
            "version": "2.0",
        },
    ]


def test_lock_local_directory(
    script: CpipTestEnvironment,
    shared_data: TestData,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "pkga"
    project_path.mkdir()
    project_path.joinpath("pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "pkga"
            version = "1.0"
            """),
    )
    result = script.cpip(
        "lock",
        ".",
        "--quiet",
        "--output=-",
        "--no-build-isolation",  # to use the pre-installed setuptools
        "--no-index",
        "--find-links",
        str(shared_data.root / "packages/"),
        cwd=project_path,
        expect_stderr=True,  # for the experimental warning
    )
    pylock = tomllib.loads(result.stdout)
    assert pylock["packages"] == [
        {
            "name": "pkga",
            "directory": {"path": "."},
        },
    ]


def test_lock_local_editable_with_dep(
    script: CpipTestEnvironment,
    shared_data: TestData,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "pkga"
    project_path.mkdir()
    project_path.joinpath("pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "pkga"
            version = "1.0"
            dependencies = ["simplewheel==2.0"]
            """),
    )
    result = script.cpip(
        "lock",
        "-e",
        ".",
        "--quiet",
        "--output=-",
        "--no-build-isolation",  # to use the pre-installed setuptools
        "--no-index",
        "--find-links",
        str(shared_data.root / "packages/"),
        cwd=project_path,
        expect_stderr=True,  # for the experimental warning
    )
    pylock = tomllib.loads(result.stdout)
    wheel_name = pylock["packages"][1]["wheels"][0]["name"]
    assert pylock["packages"] == [
        {
            "name": "pkga",
            "directory": {"editable": True, "path": "."},
        },
        {
            "name": "simplewheel",
            "version": "2.0",
            "wheels": [
                {
                    **expected_simplewheel_lock(shared_data, wheel_name),
                },
            ],
        },
    ]


@pytest.mark.network
def test_lock_vcs(script: CpipTestEnvironment, shared_data: TestData) -> None:
    result = script.cpip(
        "lock",
        "git+https://github.com/pypa/pip-test-package@0.1.2",
        "--quiet",
        "--output=-",
        "--no-build-isolation",  # to use the pre-installed setuptools
        "--no-index",
        expect_stderr=True,  # for the experimental warning
    )
    pylock = tomllib.loads(result.stdout)
    assert pylock["packages"] == [
        {
            "name": "pip-test-package",
            "vcs": {
                "type": "git",
                "url": "https://github.com/pypa/pip-test-package",
                "requested-revision": "0.1.2",
                "commit-id": "f1c1020ebac81f9aeb5c766ff7a772f709e696ee",
            },
        },
    ]


@pytest.mark.network
def test_lock_archive(script: CpipTestEnvironment, shared_data: TestData) -> None:
    result = script.cpip(
        "lock",
        "https://github.com/pypa/pip-test-package/tarball/0.1.2",
        "--quiet",
        "--output=-",
        "--no-build-isolation",  # to use the pre-installed setuptools
        "--no-index",
        expect_stderr=True,  # for the experimental warning
    )
    pylock = tomllib.loads(result.stdout)
    assert pylock["packages"] == [
        {
            "name": "pip-test-package",
            "archive": {
                "url": "https://github.com/pypa/pip-test-package/tarball/0.1.2",
                "hashes": {
                    "sha256": (
                        "1b176298e5ecd007da367bfda91aad3c"
                        "4a6534227faceda087b00e5b14d596bf"
                    ),
                },
            },
        },
    ]


def test_lock_roundtrip(script: CpipTestEnvironment, data: TestData) -> None:
    pylock_path = data.lockfiles.joinpath("pylock.toml")
    pylock_result_path = pylock_path.parent / "pylock.result.toml"
    script.cpip(
        "lock",
        "--quiet",
        "--no-build-isolation",  # to use the pre-installed setuptools
        "--no-index",
        "-r",
        pylock_path,
        "--output",
        pylock_result_path,
        expect_stderr=True,  # for the experimental warning
    )

    def simplify_path_and_url(d: dict[str, Any]) -> None:
        """Keep last part of path/url as filename key"""
        if path := d.get("path"):
            d["filename"] = path.rpartition("/")[-1]
            del d["path"]
        if url := d.get("url"):
            d["filename"] = url.rpartition("/")[-1]
            del d["url"]

    def simplify_paths_and_urls(d: dict[str, Any]) -> None:
        for p in d["packages"]:
            if "archive" in p:
                simplify_path_and_url(p["archive"])
            elif "sdist" in p:
                simplify_path_and_url(p["sdist"])
            elif "wheels" in p:
                for wheel in p["wheels"]:
                    simplify_path_and_url(wheel)

    pylock = tomllib.loads(pylock_path.read_text(encoding="utf-8"))
    simplify_paths_and_urls(pylock)
    pylock_result = tomllib.loads(pylock_result_path.read_text(encoding="utf-8"))
    simplify_paths_and_urls(pylock_result)
    assert pylock_result == pylock
