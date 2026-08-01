import os

from cpip.vcs.mercurial import Mercurial
from cpip_test_support import CpipTestEnvironment, create_test_package, need_mercurial


@need_mercurial
def test_get_repository_root(script: CpipTestEnvironment) -> None:
    version_pkg_path = create_test_package(script.scratch_path, vcs="hg")
    tests_path = version_pkg_path.joinpath("tests")
    tests_path.mkdir()

    root1 = Mercurial.get_repository_root(os.fspath(version_pkg_path))
    assert root1 is not None
    assert os.path.normcase(root1) == os.path.normcase(version_pkg_path)

    root2 = Mercurial.get_repository_root(os.fspath(tests_path))
    assert root2 is not None
    assert os.path.normcase(root2) == os.path.normcase(version_pkg_path)
