"""Tests for the ``cpip hash`` command"""

from pathlib import Path

from cpip_test_support import CpipTestEnvironment


def test_basic_hash(script: CpipTestEnvironment, tmpdir: Path) -> None:
    """Run 'cpip hash' through its default behavior."""
    expected = (
        "--hash=sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    result = script.cpip("hash", hello_file(tmpdir))
    assert expected in str(result)


def test_good_algo_option(script: CpipTestEnvironment, tmpdir: Path) -> None:
    """Make sure the -a option works."""
    expected = (
        "--hash=sha512:9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caad"
        "ae2dff72519673ca72323c3d99ba5c11d7c7acc6e14b8c5da0c4663475c2e"
        "5c3adef46f73bcdec043"
    )
    result = script.cpip("hash", "-a", "sha512", hello_file(tmpdir))
    assert expected in str(result)


def test_bad_algo_option(script: CpipTestEnvironment, tmpdir: Path) -> None:
    """Make sure the -a option raises an error when given a bad operand."""
    result = script.cpip(
        "hash",
        "-a",
        "invalidname",
        hello_file(tmpdir),
        expect_error=True,
    )
    assert "invalid choice: 'invalidname'" in str(result)


def hello_file(tmpdir: Path) -> Path:
    """Return a temp file to hash containing "hello"."""
    file = tmpdir / "hashable"
    file.write_text("hello")
    return file
