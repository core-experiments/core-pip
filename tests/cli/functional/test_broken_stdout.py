import os
import subprocess
from pathlib import Path

BROKEN_STDOUT_RETURN_CODE = 120


def setup_broken_stdout_test(
    args: list[str],
    deprecated_python: bool,
) -> tuple[str, int]:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Make the write happen while cpip is still inside its exception
        # handler.  Buffered interpreter shutdown is reported differently on
        # Windows and would bypass cpip's broken-pipe handling entirely.
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # Call close() on stdout to cause a broken pipe.
    assert proc.stdout is not None
    proc.stdout.close()
    returncode = proc.wait()
    assert proc.stderr is not None
    stderr = proc.stderr.read().decode("utf-8")

    expected_msg = "ERROR: Pipe to stdout was broken"
    if deprecated_python:
        assert expected_msg in stderr
    else:
        assert stderr.startswith(expected_msg)

    return stderr, returncode


def test_broken_stdout_pipe(deprecated_python: bool) -> None:
    """Test a broken pipe to stdout."""
    stderr, returncode = setup_broken_stdout_test(
        ["cpip", "list"],
        deprecated_python=deprecated_python,
    )

    # Check that no traceback occurs.
    assert "raise BrokenStdoutLoggingError()" not in stderr
    assert stderr.count("Traceback") == 0

    assert returncode == BROKEN_STDOUT_RETURN_CODE


def test_broken_stdout_pipe__log_option(deprecated_python: bool, tmpdir: Path) -> None:
    """Test a broken pipe to stdout when --log is passed."""
    log_path = os.path.join(str(tmpdir), "log.txt")
    stderr, returncode = setup_broken_stdout_test(
        ["cpip", "--log", log_path, "list"],
        deprecated_python=deprecated_python,
    )

    # Check that no traceback occurs.
    assert "raise BrokenStdoutLoggingError()" not in stderr
    assert stderr.count("Traceback") == 0

    assert returncode == BROKEN_STDOUT_RETURN_CODE


def test_broken_stdout_pipe__verbose(deprecated_python: bool) -> None:
    """Test a broken pipe to stdout with verbose logging enabled."""
    stderr, returncode = setup_broken_stdout_test(
        ["cpip", "-vv", "list"],
        deprecated_python=deprecated_python,
    )

    # Check that a traceback occurs and that it occurs at most once.
    # We permit up to two because the exception can be chained.
    assert "raise BrokenStdoutLoggingError()" in stderr
    assert 1 <= stderr.count("Traceback") <= 2

    assert returncode == BROKEN_STDOUT_RETURN_CODE
