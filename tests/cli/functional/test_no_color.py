"""
Test specific for the --no-color option
"""

import os
import shutil
import subprocess
import sys

import pytest

from cpip_test_support import CpipTestEnvironment


@pytest.mark.network
@pytest.mark.skipif(shutil.which("script") is None, reason="no 'script' executable")
def test_no_color(script: CpipTestEnvironment) -> None:
    """Ensure colour output disabled when --no-color is passed."""
    # Using 'script' in this test allows for transparently testing cpip's output
    # since cpip is smart enough to disable colour output when piped, which is
    # not the behaviour we want to be testing here.
    #
    # On the other hand, this test is non-portable due to the options passed to
    # 'script' and well as the mere use of the same.
    #
    # This test will stay until someone has the time to rewrite it.
    cpip_command = "cpip download {} setuptools==62.0.0 --no-cache-dir -d /tmp/"
    if sys.platform == "darwin":
        command = f"script -q /tmp/cpip-test-no-color.txt {cpip_command}"
    else:
        command = f'script -q /tmp/cpip-test-no-color.txt --command "{cpip_command}"'

    def get_run_output(option: str = "") -> str:
        cmd = command.format(option)
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate()

        try:
            with open("/tmp/cpip-test-no-color.txt") as output_file:
                retval = output_file.read()
            return retval
        finally:
            os.unlink("/tmp/cpip-test-no-color.txt")
            os.unlink("/tmp/setuptools-62.0.0-py3-none-any.whl")

    assert "\x1b[3" in get_run_output(""), "Expected color in output"
    assert "\x1b[3" not in get_run_output("--no-color"), "Expected no color in output"
