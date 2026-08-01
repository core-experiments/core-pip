import json

import pytest
from cpip_test_support import CpipTestEnvironment, ScriptFactory, TestData


@pytest.fixture
def simple_script(
    tmpdir_factory: pytest.TempPathFactory,
    script_factory: ScriptFactory,
    shared_data: TestData,
) -> CpipTestEnvironment:
    tmpdir = tmpdir_factory.mktemp("cpip_test_package")
    script = script_factory(tmpdir.joinpath("workspace"))
    script.cpip(
        "install",
        "-f",
        shared_data.find_links,
        "--no-index",
        "simplewheel==1.0",
    )
    return script


def test_inspect_basic(simple_script: CpipTestEnvironment) -> None:
    """Test default behavior of inspect command."""
    result = simple_script.cpip("inspect")
    report = json.loads(result.stdout)
    installed_by_name = {i["metadata"]["name"]: i for i in report["installed"]}
    # Coverage is only installed if test coverage is being collected.
    installed_by_name.pop("coverage", None)
    assert len(installed_by_name) == 3
    assert installed_by_name.keys() == {
        "cpip",
        "setuptools",
        "simplewheel",
    }
    assert installed_by_name["simplewheel"]["metadata"]["version"] == "1.0"
    assert installed_by_name["simplewheel"]["requested"] is True
    assert installed_by_name["simplewheel"]["installer"] == "cpip"
    assert "environment" in report
