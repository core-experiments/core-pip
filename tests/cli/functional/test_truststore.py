from collections.abc import Callable
from typing import Any

import pytest
from cpip_test_support import CpipTestEnvironment, TestCpipResult

CpipRunner = Callable[..., TestCpipResult]


@pytest.fixture
def cpip_no_truststore(script: CpipTestEnvironment) -> CpipRunner:
    def cpip(*args: str, **kwargs: Any) -> TestCpipResult:
        return script.cpip(*args, "--use-deprecated=legacy-certs", **kwargs)

    return cpip


@pytest.mark.network
@pytest.mark.parametrize(
    "package",
    [
        "INITools",
        "https://github.com/pypa/pip-test-package/archive/refs/heads/master.zip",
    ],
    ids=["PyPI", "GitHub"],
)
def test_no_truststore_can_install(
    script: CpipTestEnvironment,
    cpip_no_truststore: CpipRunner,
    package: str,
) -> None:
    result = cpip_no_truststore("install", package)
    assert "Successfully installed" in result.stdout
