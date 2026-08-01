import os
from pathlib import Path

from cpip import __version__
from cpip_test_support import CpipTestEnvironment


def test_runner_work_in_environments_with_no_pip(
    script: CpipTestEnvironment,
    cpip_src: Path,
) -> None:
    runner = cpip_src / "src" / "cpip" / "__cpip-runner__.py"

    # Ensure there's no cpip distribution installed in the environment.
    script.cpip("uninstall", "cpip", "--yes", use_module=True)
    # We don't use script.cpip to check here, as when testing a
    # zipapp, script.cpip will run cpip from the zipapp.
    script.run("python", "-c", "import cpip", expect_error=True)

    # The runner script should still invoke a usable cpip
    result = script.run("python", os.fspath(runner), "--version")

    assert __version__ in result.stdout
