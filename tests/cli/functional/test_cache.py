import os
import pathlib
import re
import shutil
from collections.abc import Callable
from glob import glob

import pytest
from cpip_test_support import CpipTestEnvironment, TestCpipResult

from cpip.cli.fast_install import TREE_CACHE_BUCKET
from cpip.core.appdirs import versioned_cache_dir
from cpip.core.appdirs import WHEEL_CACHE_BUCKET
from cpip.core.appdirs import http_cache_path


@pytest.fixture
def cache_dir(script: CpipTestEnvironment) -> str:
    result = script.run(
        "python",
        "-c",
        "from cpip.platform.locations.base import USER_CACHE_DIR;print(USER_CACHE_DIR)",
    )
    return result.stdout.strip()


@pytest.fixture
def http_cache_dir(cache_dir: str) -> str:
    return os.path.normcase(http_cache_path(versioned_cache_dir(cache_dir)))


@pytest.fixture
def wheel_cache_dir(cache_dir: str) -> str:
    return os.path.normcase(
        os.path.join(versioned_cache_dir(cache_dir), WHEEL_CACHE_BUCKET),
    )


@pytest.fixture
def http_cache_files(http_cache_dir: str) -> list[str]:
    destination = os.path.join(http_cache_dir, "arbitrary", "pathname")

    if not os.path.exists(destination):
        return []

    filenames = glob(os.path.join(destination, "*"))
    return [os.path.join(destination, filename) for filename in filenames]


@pytest.fixture
def wheel_cache_files(wheel_cache_dir: str) -> list[str]:
    destination = os.path.join(wheel_cache_dir, "arbitrary", "pathname")

    if not os.path.exists(destination):
        return []

    filenames = glob(os.path.join(destination, "*.whl"))
    return [os.path.join(destination, filename) for filename in filenames]


@pytest.fixture
def populate_http_cache(http_cache_dir: str) -> list[tuple[str, str]]:
    destination = os.path.join(http_cache_dir, "arbitrary", "pathname")
    os.makedirs(destination)

    files = [
        ("aaaaaaaaa", os.path.join(destination, "aaaaaaaaa")),
        ("bbbbbbbbb", os.path.join(destination, "bbbbbbbbb")),
        ("ccccccccc", os.path.join(destination, "ccccccccc")),
    ]

    for name_internal, filename in files:
        with open(filename, "w"):
            pass

    return files


@pytest.fixture
def populate_wheel_cache(wheel_cache_dir: str) -> list[tuple[str, str]]:
    destination = os.path.join(wheel_cache_dir, "arbitrary", "pathname")
    os.makedirs(destination)

    files = [
        ("yyy-1.2.3", os.path.join(destination, "yyy-1.2.3-py3-none-any.whl")),
        ("zzz-4.5.6", os.path.join(destination, "zzz-4.5.6-py3-none-any.whl")),
        ("zzz-4.5.7", os.path.join(destination, "zzz-4.5.7-py3-none-any.whl")),
        ("zzz-7.8.9", os.path.join(destination, "zzz-7.8.9-py3-none-any.whl")),
    ]

    for name_internal, filename in files:
        with open(filename, "w"):
            pass

    return files


@pytest.fixture
def empty_wheel_cache(wheel_cache_dir: str) -> None:
    if os.path.exists(wheel_cache_dir):
        shutil.rmtree(wheel_cache_dir)


def list_matches_wheel(wheel_name: str, result: TestCpipResult) -> bool:
    """Returns True if any line in `result`, which should be the output of
    a `cpip cache list` call, matches `wheel_name`.

    E.g., If wheel_name is `foo-1.2.3` it searches for a line starting with
          `- foo-1.2.3-py3-none-any.whl `.
    """
    lines = result.stdout.splitlines()
    expected = f" - {wheel_name}-py3-none-any.whl "
    return any(line.startswith(expected) for line in lines)


def list_matches_wheel_abspath(wheel_name: str, result: TestCpipResult) -> bool:
    """Returns True if any line in `result`, which should be the output of
    a `cpip cache list --format=abspath` call, is a valid path and belongs to
    `wheel_name`.

    E.g., If wheel_name is `foo-1.2.3` it searches for a line starting with
          `foo-1.2.3-py3-none-any.whl`.
    """
    lines = result.stdout.splitlines()
    expected = f"{wheel_name}-py3-none-any.whl"
    return any(
        (os.path.basename(line).startswith(expected) and os.path.exists(line))
        for line in lines
    )


RemoveMatches = Callable[[str, TestCpipResult], bool]


@pytest.fixture
def remove_matches_http(http_cache_dir: str) -> RemoveMatches:
    """Returns True if any line in `result`, which should be the output of
    a `cpip cache purge` call, matches `http_filename`.

    E.g., If http_filename is `aaaaaaaaa`, it searches for a line equal to
    `Removed <http files cache dir>/arbitrary/pathname/aaaaaaaaa`.
    """

    def remove_matches_http_internal(
        http_filename: str,
        result: TestCpipResult,
    ) -> bool:
        lines = result.stdout.splitlines()

        # The "/arbitrary/pathname/" bit is an implementation detail of how
        # the `populate_http_cache` fixture is implemented.
        path = os.path.join(
            http_cache_dir,
            "arbitrary",
            "pathname",
            http_filename,
        )
        expected = f"Removed {path}"
        return expected in lines

    return remove_matches_http_internal


@pytest.fixture
def remove_matches_wheel(wheel_cache_dir: str) -> RemoveMatches:
    """Returns True if any line in `result`, which should be the output of
    a `cpip cache remove`/`cpip cache purge` call, matches `wheel_name`.

    E.g., If wheel_name is `foo-1.2.3`, it searches for a line equal to
    `Removed <wheel cache dir>/arbitrary/pathname/foo-1.2.3-py3-none-any.whl`.
    """

    def remove_matches_wheel_internal(wheel_name: str, result: TestCpipResult) -> bool:
        lines = result.stdout.splitlines()

        wheel_filename = f"{wheel_name}-py3-none-any.whl"

        # The "/arbitrary/pathname/" bit is an implementation detail of how
        # the `populate_wheel_cache` fixture is implemented.
        path = os.path.join(
            wheel_cache_dir,
            "arbitrary",
            "pathname",
            wheel_filename,
        )
        expected = f"Removed {path}"
        return expected in lines

    return remove_matches_wheel_internal


def test_cache_dir(script: CpipTestEnvironment, cache_dir: str) -> None:
    result = script.cpip("cache", "dir")

    assert os.path.normcase(cache_dir) == result.stdout.strip()


def test_cache_dir_too_many_args(script: CpipTestEnvironment, cache_dir: str) -> None:
    result = script.cpip("cache", "dir", "aaa", expect_error=True)

    assert result.stdout == ""

    # This would be `result.stderr == ...`, but cpip prints deprecation
    # warnings on Python 2.7, so we check if the _line_ is in stderr.
    assert "ERROR: Too many arguments" in result.stderr.splitlines()


@pytest.mark.usefixtures("populate_http_cache", "populate_wheel_cache")
def test_cache_info(
    script: CpipTestEnvironment,
    http_cache_dir: str,
    wheel_cache_dir: str,
    wheel_cache_files: list[str],
) -> None:
    result = script.cpip("cache", "info")

    assert f"Package index page cache location: {http_cache_dir}" in result.stdout
    assert f"Locally built wheels location: {wheel_cache_dir}" in result.stdout
    num_wheels = len(wheel_cache_files)
    assert f"Number of locally built wheels: {num_wheels}" in result.stdout


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list` should return exactly what the
    populate_wheel_cache fixture adds.
    """
    result = script.cpip("cache", "list")

    assert list_matches_wheel("yyy-1.2.3", result)
    assert list_matches_wheel("zzz-4.5.6", result)
    assert list_matches_wheel("zzz-4.5.7", result)
    assert list_matches_wheel("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list_abspath(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list --format=abspath` should return full
    paths of exactly what the populate_wheel_cache fixture adds.
    """
    result = script.cpip("cache", "list", "--format=abspath")

    assert list_matches_wheel_abspath("yyy-1.2.3", result)
    assert list_matches_wheel_abspath("zzz-4.5.6", result)
    assert list_matches_wheel_abspath("zzz-4.5.7", result)
    assert list_matches_wheel_abspath("zzz-7.8.9", result)


@pytest.mark.usefixtures("empty_wheel_cache")
def test_cache_list_with_empty_cache(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list` with an empty cache should print
    "No locally built wheels cached." and exit.
    """
    result = script.cpip("cache", "list")
    assert result.stdout == "No locally built wheels cached.\n"


@pytest.mark.usefixtures("empty_wheel_cache")
def test_cache_list_with_empty_cache_abspath(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list --format=abspath` with an empty cache should not
    print anything and exit.
    """
    result = script.cpip("cache", "list", "--format=abspath")
    assert result.stdout.strip() == ""


@pytest.mark.usefixtures("empty_wheel_cache")
def test_cache_purge_with_empty_cache(script: CpipTestEnvironment) -> None:
    """Running `cpip cache purge` with an empty cache should print a warning
    and exit without an error code.
    """
    result = script.cpip("cache", "purge", allow_stderr_warning=True)
    assert result.stderr == "WARNING: No matching packages\n"
    assert result.stdout == "Files removed: 0 (0 bytes)\nDirectories removed: 0\n"


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_remove_with_bad_pattern(script: CpipTestEnvironment) -> None:
    """Running `cpip cache remove` with a bad pattern should print a warning
    and exit without an error code.
    """
    result = script.cpip("cache", "remove", "aaa", allow_stderr_warning=True)
    assert result.stderr == 'WARNING: No matching packages for pattern "aaa"\n'
    assert result.stdout == "Files removed: 0 (0 bytes)\nDirectories removed: 0\n"


def test_cache_list_too_many_args(script: CpipTestEnvironment) -> None:
    """Passing `cpip cache list` too many arguments should cause an error."""
    script.cpip("cache", "list", "aaa", "bbb", expect_error=True)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list_name_match(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list zzz` should list zzz-4.5.6, zzz-4.5.7,
    zzz-7.8.9, but nothing else.
    """
    result = script.cpip("cache", "list", "zzz", "--verbose")

    assert not list_matches_wheel("yyy-1.2.3", result)
    assert list_matches_wheel("zzz-4.5.6", result)
    assert list_matches_wheel("zzz-4.5.7", result)
    assert list_matches_wheel("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list_name_match_abspath(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list zzz --format=abspath` should list paths of
    zzz-4.5.6, zzz-4.5.7, zzz-7.8.9, but nothing else.
    """
    result = script.cpip("cache", "list", "zzz", "--format=abspath", "--verbose")

    assert not list_matches_wheel_abspath("yyy-1.2.3", result)
    assert list_matches_wheel_abspath("zzz-4.5.6", result)
    assert list_matches_wheel_abspath("zzz-4.5.7", result)
    assert list_matches_wheel_abspath("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list_name_and_version_match(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list zzz-4.5.6` should list zzz-4.5.6, but
    nothing else.
    """
    result = script.cpip("cache", "list", "zzz-4.5.6", "--verbose")

    assert not list_matches_wheel("yyy-1.2.3", result)
    assert list_matches_wheel("zzz-4.5.6", result)
    assert not list_matches_wheel("zzz-4.5.7", result)
    assert not list_matches_wheel("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_list_name_and_version_match_abspath(script: CpipTestEnvironment) -> None:
    """Running `cpip cache list zzz-4.5.6 --format=abspath` should list path of
    zzz-4.5.6, but nothing else.
    """
    result = script.cpip("cache", "list", "zzz-4.5.6", "--format=abspath", "--verbose")

    assert not list_matches_wheel_abspath("yyy-1.2.3", result)
    assert list_matches_wheel_abspath("zzz-4.5.6", result)
    assert not list_matches_wheel_abspath("zzz-4.5.7", result)
    assert not list_matches_wheel_abspath("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_remove_no_arguments(script: CpipTestEnvironment) -> None:
    """Running `cpip cache remove` with no arguments should cause an error."""
    script.cpip("cache", "remove", expect_error=True)


def test_cache_remove_too_many_args(script: CpipTestEnvironment) -> None:
    """Passing `cpip cache remove` too many arguments should cause an error."""
    script.cpip("cache", "remove", "aaa", "bbb", expect_error=True)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_remove_name_match(
    script: CpipTestEnvironment,
    remove_matches_wheel: RemoveMatches,
) -> None:
    """Running `cpip cache remove zzz` should remove zzz-4.5.6 and zzz-7.8.9,
    but nothing else.
    """
    result = script.cpip("cache", "remove", "zzz", "--verbose")

    assert not remove_matches_wheel("yyy-1.2.3", result)
    assert remove_matches_wheel("zzz-4.5.6", result)
    assert remove_matches_wheel("zzz-4.5.7", result)
    assert remove_matches_wheel("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_wheel_cache")
def test_cache_remove_name_and_version_match(
    script: CpipTestEnvironment,
    remove_matches_wheel: RemoveMatches,
) -> None:
    """Running `cpip cache remove zzz-4.5.6` should remove zzz-4.5.6, but
    nothing else.
    """
    result = script.cpip("cache", "remove", "zzz-4.5.6", "--verbose")

    assert not remove_matches_wheel("yyy-1.2.3", result)
    assert remove_matches_wheel("zzz-4.5.6", result)
    assert not remove_matches_wheel("zzz-4.5.7", result)
    assert not remove_matches_wheel("zzz-7.8.9", result)


@pytest.mark.usefixtures("populate_http_cache", "populate_wheel_cache")
def test_cache_purge(
    script: CpipTestEnvironment,
    remove_matches_http: RemoveMatches,
    remove_matches_wheel: RemoveMatches,
) -> None:
    """Running `cpip cache purge` should remove all cached http files and
    wheels.
    """
    result = script.cpip("cache", "purge", "--verbose")

    assert remove_matches_http("aaaaaaaaa", result)
    assert remove_matches_http("bbbbbbbbb", result)
    assert remove_matches_http("ccccccccc", result)

    assert remove_matches_wheel("yyy-1.2.3", result)
    assert remove_matches_wheel("zzz-4.5.6", result)
    assert remove_matches_wheel("zzz-4.5.7", result)
    assert remove_matches_wheel("zzz-7.8.9", result)


def test_cache_purge_removes_fast_install_snapshots(
    script: CpipTestEnvironment,
    cache_dir: str,
) -> None:
    from cpip.cli.fast_install import NAME as FAST_INSTALL_SNAPSHOT_NAME

    snapshot = os.path.join(versioned_cache_dir(cache_dir), FAST_INSTALL_SNAPSHOT_NAME)
    tree_file = os.path.join(
        versioned_cache_dir(cache_dir),
        TREE_CACHE_BUCKET,
        "aa",
        "digest",
        "tree",
        "demo.py",
    )
    os.makedirs(os.path.dirname(tree_file))
    with open(snapshot, "wb") as file:
        file.write(b"snapshot")
    with open(tree_file, "wb") as file:
        file.write(b"tree")

    script.cpip("cache", "purge", "--verbose")

    assert not os.path.exists(snapshot)
    assert not os.path.exists(tree_file)


def _plant(path: str, content: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file:
        file.write(content)
    return path


def _plant_every_store(cache_root: str) -> list[str]:
    """Seed every shape a purge must remove: a nested bucket, a built wheel,
    a SQLite store with its -wal sidecar, a snapshot temp file, and a file
    under another cpip version's directory."""
    cache_dir = versioned_cache_dir(cache_root)
    return [
        _plant(os.path.join(cache_root, "v999", "http", "aa", "entry")),
        _plant(os.path.join(http_cache_path(cache_dir), "aa", "entry")),
        _plant(
            os.path.join(
                cache_dir,
                WHEEL_CACHE_BUCKET,
                "aa",
                "bb",
                "digest",
                "demo-1.0-py3-none-any.whl",
            ),
        ),
        _plant(os.path.join(cache_dir, "store.sqlite")),
        _plant(os.path.join(cache_dir, "store.sqlite-wal")),
        _plant(os.path.join(cache_dir, "snapshot.marshal.123.tmp")),
    ]


def test_cache_purge_removes_every_store(
    script: CpipTestEnvironment,
    cache_dir: str,
) -> None:
    planted = _plant_every_store(cache_dir)

    result = script.cpip("cache", "purge", "--verbose")

    assert f"Files removed: {len(planted)}" in result.stdout
    assert [path for path in planted if os.path.exists(path)] == []
    assert "No matching packages" not in result.stderr


def test_cache_sees_built_wheels(
    script: CpipTestEnvironment,
    cache_dir: str,
) -> None:
    wheel = _plant(
        os.path.join(
            versioned_cache_dir(cache_dir),
            WHEEL_CACHE_BUCKET,
            "aa",
            "bb",
            "digest",
            "demo-1.0-py3-none-any.whl",
        ),
    )

    assert "Number of locally built wheels: 1" in script.cpip("cache", "info").stdout
    assert "demo-1.0-py3-none-any.whl" in script.cpip("cache", "list").stdout
    script.cpip("cache", "remove", "demo")
    assert not os.path.exists(wheel)


def test_cache_purge_escapes_glob_metacharacters_in_cache_dir(
    script: CpipTestEnvironment,
    tmp_path: pathlib.Path,
) -> None:
    cache_dir = os.fspath(tmp_path / "cache[1]")
    planted = _plant_every_store(cache_dir)

    result = script.cpip("cache", "purge", "--cache-dir", cache_dir)

    assert f"Files removed: {len(planted)}" in result.stdout
    assert [path for path in planted if os.path.exists(path)] == []


def test_cache_commands_honor_cpip_cache_dir(
    script: CpipTestEnvironment,
    tmp_path: pathlib.Path,
) -> None:
    """The manager resolves its root the way every cache writer does."""
    cache_dir = os.fspath(tmp_path / "configured")
    script.environ["CPIP_CACHE_DIR"] = cache_dir
    planted = _plant_every_store(cache_dir)

    assert script.cpip("cache", "dir").stdout.strip() == os.path.normcase(cache_dir)
    result = script.cpip("cache", "purge")

    assert f"Files removed: {len(planted)}" in result.stdout
    assert [path for path in planted if os.path.exists(path)] == []


def test_cache_purge_sweeps_empty_directories_without_files(
    script: CpipTestEnvironment,
    http_cache_dir: str,
) -> None:
    """A second purge finds only empty buckets; it removes them instead of
    warning that nothing matched."""
    os.makedirs(os.path.join(http_cache_dir, "empty", "nested"))

    result = script.cpip("cache", "purge")

    assert "Files removed: 0" in result.stdout
    assert "Directories removed: 4" in result.stdout
    assert not os.path.exists(http_cache_dir)
    assert "No matching packages" not in result.stderr


@pytest.mark.usefixtures("populate_http_cache", "populate_wheel_cache")
def test_cache_purge_too_many_args(
    script: CpipTestEnvironment,
    http_cache_files: list[str],
    wheel_cache_files: list[str],
) -> None:
    """Running `cpip cache purge aaa` should raise an error and remove no
    cached http files or wheels.
    """
    result = script.cpip("cache", "purge", "aaa", "--verbose", expect_error=True)
    assert result.stdout == ""

    # This would be `result.stderr == ...`, but cpip prints deprecation
    # warnings on Python 2.7, so we check if the _line_ is in stderr.
    assert "ERROR: Too many arguments" in result.stderr.splitlines()

    # Make sure nothing was deleted.
    for filename in http_cache_files + wheel_cache_files:
        assert os.path.exists(filename)


@pytest.mark.parametrize("command", ["info", "list", "remove", "purge"])
def test_cache_abort_when_no_cache_dir(
    script: CpipTestEnvironment,
    command: str,
) -> None:
    """Running any cpip cache command when cache is disabled should
    abort and log an informative error
    """
    result = script.cpip("cache", command, "--no-cache-dir", expect_error=True)
    assert result.stdout == ""

    assert (
        "ERROR: cpip cache commands can not function"
        " since cache is disabled." in result.stderr.splitlines()
    )


@pytest.fixture
def populate_wheel_cache_with_empty_dirs(wheel_cache_dir: str) -> None:
    metadata_dir = os.path.join(wheel_cache_dir, "metadata_only")
    os.makedirs(metadata_dir)
    with open(os.path.join(metadata_dir, "metadata.json"), "w"):
        pass

    empty_dir = os.path.join(wheel_cache_dir, "completely_empty")
    os.makedirs(empty_dir)

    nested_empty = os.path.join(wheel_cache_dir, "nested", "empty", "dirs")
    os.makedirs(nested_empty)


@pytest.fixture
def populate_http_cache_with_empty_dirs(http_cache_dir: str) -> None:
    os.makedirs(os.path.join(http_cache_dir, "empty1"))
    os.makedirs(os.path.join(http_cache_dir, "empty2", "nested"))
    os.makedirs(os.path.join(http_cache_dir, "empty3", "nested"))


@pytest.mark.usefixtures(
    "populate_wheel_cache_with_empty_dirs",
    "populate_http_cache_with_empty_dirs",
)
def test_cache_purge_removes_empty_dirs(
    script: CpipTestEnvironment,
    http_cache_dir: str,
    wheel_cache_dir: str,
) -> None:
    """Test cpip cache purge/remove with empty directories.

    Verifies purge removes:
    - Wheel cache directories without .whl files
    - HTTP cache empty directories
    - Reports correct directory counts
    Also tests that 'cache remove' works similarly.
    """
    metadata_dir = os.path.join(wheel_cache_dir, "metadata_only")

    # Verify setup
    assert os.path.exists(metadata_dir)
    assert os.path.exists(os.path.join(http_cache_dir, "empty1"))
    assert os.path.exists(os.path.join(http_cache_dir, "empty3"))

    result = script.cpip("cache", "purge", "--verbose", allow_stderr_warning=True)

    # Verify all cleanup happened
    assert not os.path.exists(metadata_dir)
    assert not os.path.exists(os.path.join(wheel_cache_dir, "completely_empty"))
    assert not os.path.exists(os.path.join(http_cache_dir, "empty1"))
    assert not os.path.exists(os.path.join(http_cache_dir, "empty3"))
    assert "Directories removed:" in result.stdout

    # Verify directory count is positive
    dir_count = int(re.findall(r"Directories removed: (\d+)", result.stdout)[0])
    assert dir_count > 0


def test_cache_purge_with_mixed_content(
    script: CpipTestEnvironment,
    populate_wheel_cache: list[tuple[str, str]],
    wheel_cache_dir: str,
) -> None:
    """Test purge removes both wheel files and empty directories."""
    # Add an empty directory alongside the wheels
    empty_dir = os.path.join(wheel_cache_dir, "empty_subdir")
    os.makedirs(empty_dir)

    result = script.cpip("cache", "purge", "--verbose")

    # Verify wheels and empty directory were removed
    for name_internal, filepath in populate_wheel_cache:
        assert not os.path.exists(filepath)
    assert not os.path.exists(empty_dir)

    # Verify counts in output
    assert "Files removed:" in result.stdout
    assert "Directories removed:" in result.stdout
    files_removed = int(re.findall(r"Files removed: (\d+)", result.stdout)[0])
    assert files_removed == 4
