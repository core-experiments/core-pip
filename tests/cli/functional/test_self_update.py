# Check that cpip can update itself correctly

from pathlib import Path
from typing import Any


def test_self_update_editable(script: Any, cpip_src: Any, common_wheels: Path) -> None:
    # Test that if we have an environment with cpip installed in non-editable
    # mode, that cpip can safely update itself to an editable install.
    # See https://github.com/pypa/cpip/issues/12666 for details.

    # Install flit-core (build backend) since we use --no-build-isolation
    script.cpip("install", "--no-index", "-f", common_wheels, "flit-core")

    # Step 1. Install cpip as non-editable. This is expected to succeed as
    # the existing cpip in the environment is installed in editable mode, so
    # it only places a .pth file in the environment.
    proc = script.cpip("install", "--no-build-isolation", "--no-deps", cpip_src)
    assert proc.returncode == 0
    # Step 2. Using the cpip we just installed, install cpip *again*, but
    # in editable mode. This could fail, as we'll need to uninstall the running
    # cpip in order to install the new copy, and uninstalling cpip while it's
    # running could fail. This test is specifically to ensure that doesn't
    # happen...
    proc = script.cpip("install", "--no-build-isolation", "--no-deps", "-e", cpip_src)
    assert proc.returncode == 0
