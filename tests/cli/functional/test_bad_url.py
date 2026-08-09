from typing import Any


def test_filenotfound_error_message(script: Any) -> None:
    proc = script.cpip("install", "-r", "file:///unexistent_file", expect_error=True)
    assert proc.returncode == 1
    expect = (
        "ERROR: 404 Client Error: FileNotFoundError for url: file:///unexistent_file"
    )
    assert proc.stderr.rstrip() == expect
