from pathlib import Path

import pytest
from cpip.vcs.subversion import Subversion
from cpip.vcs.versioncontrol import RemoteNotFoundError
from cpip_test_support import CpipTestEnvironment, create_svn_repo, need_svn


@need_svn
def test_get_remote_url__no_remote(script: CpipTestEnvironment, tmpdir: Path) -> None:
    repo_path = tmpdir / "temp-repo"
    repo_path.mkdir()
    repo_dir = str(repo_path)

    create_svn_repo(script.scratch_path, repo_dir)

    with pytest.raises(RemoteNotFoundError):
        Subversion().get_remote_url(repo_dir)


@need_svn
def test_get_remote_url__no_remote_with_setup(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    repo_path = tmpdir / "temp-repo"
    repo_path.mkdir()
    setup = repo_path / "setup.py"
    setup.touch()
    repo_dir = str(repo_path)

    create_svn_repo(script.scratch_path, repo_dir)

    with pytest.raises(RemoteNotFoundError):
        Subversion().get_remote_url(repo_dir)
