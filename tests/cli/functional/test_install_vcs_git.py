from __future__ import annotations

from pathlib import Path

import pytest
from cpip_test_support import (
    CpipTestEnvironment,
    change_test_package_version,
    create_test_package,
    pyversion,  # noqa: F401
)
from cpip_test_support.git_submodule_helpers import (
    change_test_package_submodule,
    create_test_package_with_submodule,
    pull_in_submodule_changes_to_module,
)
from cpip_test_support.local_repos import local_checkout


def get_editable_repo_dir(script: CpipTestEnvironment, package_name: str) -> Path:
    """Return the repository directory for an editable install."""
    return script.venv_path / "src" / package_name


def get_editable_branch(script: CpipTestEnvironment, package_name: str) -> str:
    """Return the current branch of an editable install."""
    repo_dir = get_editable_repo_dir(script, package_name)
    result = script.run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
    return result.stdout.strip()


def get_branch_remote(
    script: CpipTestEnvironment,
    package_name: str,
    branch: str,
) -> str:
    """ """
    repo_dir = get_editable_repo_dir(script, package_name)
    result = script.run("git", "config", f"branch.{branch}.remote", cwd=repo_dir)
    return result.stdout.strip()


def github_checkout(
    url_path: str,
    tmpdir: Path,
    rev: str | None = None,
    egg: str | None = None,
    scheme: str | None = None,
) -> str:
    """Call local_checkout() with a GitHub URL, and return the resulting URL.

    Args:
      url_path: the string used to create the package URL by filling in the
        format string "git+{scheme}://github.com/{url_path}".
      temp_dir: the pytest tmpdir value.
      egg: an optional project name to append to the URL as the egg fragment,
        prior to returning.
      scheme: the scheme without the "git+" prefix. Defaults to "https".

    """
    if scheme is None:
        scheme = "https"
    url = f"git+{scheme}://github.com/{url_path}"
    local_url = local_checkout(url, tmpdir)
    if rev is not None:
        local_url += f"@{rev}"
    if egg is not None:
        local_url += f"#egg={egg}"

    return local_url


def make_version_pkg_url(
    path: Path,
    rev: str | None = None,
    name: str = "version_pkg",
) -> str:
    """Return a "git+file://" URL to the version_pkg test package.

    Args:
      path: a pathlib.Path object pointing to a Git repository
        containing the version_pkg package.
      rev: an optional revision to install like a branch name, tag, or SHA.

    """
    file_url = path.as_uri()
    url_rev = "" if rev is None else f"@{rev}"
    url = f"git+{file_url}{url_rev}#egg={name}"

    return url


def install_version_pkg_only(
    script: CpipTestEnvironment,
    path: Path,
    rev: str | None = None,
    allow_stderr_warning: bool = False,
) -> None:
    """Install the version_pkg package in editable mode (without returning
    the version).

    Args:
      path: a pathlib.Path object pointing to a Git repository
        containing the package.
      rev: an optional revision to install like a branch name or tag.

    """
    version_pkg_url = make_version_pkg_url(path, rev=rev)
    script.cpip(
        "install",
        "--no-build-isolation",
        "-e",
        version_pkg_url,
        allow_stderr_warning=allow_stderr_warning,
    )


def install_version_pkg(
    script: CpipTestEnvironment,
    path: Path,
    rev: str | None = None,
    allow_stderr_warning: bool = False,
) -> str:
    """Install the version_pkg package in editable mode, and return the version
    installed.

    Args:
      path: a pathlib.Path object pointing to a Git repository
        containing the package.
      rev: an optional revision to install like a branch name or tag.

    """
    install_version_pkg_only(
        script,
        path,
        rev=rev,
        allow_stderr_warning=allow_stderr_warning,
    )
    result = script.run("version_pkg")
    version = result.stdout.strip()

    return version


def test_git_install_again_after_changes(script: CpipTestEnvironment) -> None:
    """Test installing a repository a second time without specifying a revision,
    and after updates to the remote repository.

    This test also checks that no warning message like the following gets
    logged on the update: "Did not find branch or tag ..., assuming ref or
    revision."
    """
    version_pkg_path = create_test_package(script.scratch_path)
    version = install_version_pkg(script, version_pkg_path)
    assert version == "0.1"

    change_test_package_version(script, version_pkg_path)
    version = install_version_pkg(script, version_pkg_path)
    assert version == "some different version"


def test_git_install_branch_again_after_branch_changes(
    script: CpipTestEnvironment,
) -> None:
    """Test installing a branch again after the branch is updated in the remote
    repository.
    """
    version_pkg_path = create_test_package(script.scratch_path)
    version = install_version_pkg(script, version_pkg_path, rev="master")
    assert version == "0.1"

    change_test_package_version(script, version_pkg_path)
    version = install_version_pkg(script, version_pkg_path, rev="master")
    assert version == "some different version"


@pytest.mark.network
def test_install_editable_from_git_with_https(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    """Test cloning from Git with https."""
    url_path = "pypa/pip-test-package.git"
    local_url = github_checkout(url_path, tmpdir, egg="pip-test-package")
    result = script.cpip("install", "-e", local_url)
    result.assert_installed(
        "piptestpackage",
        dist_name="pip-test-package",
        with_files=[".git"],
    )


@pytest.mark.network
def test_install_noneditable_git(script: CpipTestEnvironment) -> None:
    """Test installing from a non-editable git URL with a given tag."""
    result = script.cpip(
        "install",
        "git+https://github.com/pypa/pip-test-package.git@0.1.1#egg=pip-test-package",
    )
    dist_info_folder = script.site_packages / "pip_test_package-0.1.1.dist-info"
    result.assert_installed(
        "piptestpackage",
        dist_name="pip-test-package",
        editable=False,
    )
    result.did_create(dist_info_folder)


def test_git_with_sha1_revisions(script: CpipTestEnvironment) -> None:
    """Git backend should be able to install from SHA1 revisions"""
    version_pkg_path = create_test_package(script.scratch_path)
    change_test_package_version(script, version_pkg_path)
    sha1 = script.run(
        "git",
        "rev-parse",
        "HEAD~1",
        cwd=version_pkg_path,
    ).stdout.strip()
    version = install_version_pkg(script, version_pkg_path, rev=sha1)
    assert version == "0.1"


def test_git_with_short_sha1_revisions(script: CpipTestEnvironment) -> None:
    """Git backend should be able to install from SHA1 revisions"""
    version_pkg_path = create_test_package(script.scratch_path)
    change_test_package_version(script, version_pkg_path)
    sha1 = script.run(
        "git",
        "rev-parse",
        "HEAD~1",
        cwd=version_pkg_path,
    ).stdout.strip()[:7]
    version = install_version_pkg(
        script,
        version_pkg_path,
        rev=sha1,
        # WARNING: Did not find branch or tag ..., assuming revision or ref.
        allow_stderr_warning=True,
    )
    assert version == "0.1"


def test_git_with_branch_name_as_revision(script: CpipTestEnvironment) -> None:
    """Git backend should be able to install from branch names"""
    version_pkg_path = create_test_package(script.scratch_path)
    branch = "test_branch"
    script.run("git", "checkout", "-b", branch, cwd=version_pkg_path)
    change_test_package_version(script, version_pkg_path)
    version = install_version_pkg(script, version_pkg_path, rev=branch)
    assert version == "some different version"


def test_git_with_tag_name_as_revision(script: CpipTestEnvironment) -> None:
    """Git backend should be able to install from tag names"""
    version_pkg_path = create_test_package(script.scratch_path)
    script.run("git", "tag", "test_tag", cwd=version_pkg_path)
    change_test_package_version(script, version_pkg_path)
    version = install_version_pkg(script, version_pkg_path, rev="test_tag")
    assert version == "0.1"


def add_ref(script: CpipTestEnvironment, path: Path, ref: str) -> None:
    """Add a new ref to a repository at the given path."""
    script.run("git", "update-ref", ref, "HEAD", cwd=path)


def test_git_install_ref(script: CpipTestEnvironment) -> None:
    """The Git backend should be able to install a ref with the first install."""
    version_pkg_path = create_test_package(script.scratch_path)
    add_ref(script, version_pkg_path, "refs/foo/bar")
    change_test_package_version(script, version_pkg_path)

    version = install_version_pkg(
        script,
        version_pkg_path,
        rev="refs/foo/bar",
        # WARNING: Did not find branch or tag ..., assuming revision or ref.
        allow_stderr_warning=True,
    )
    assert version == "0.1"


def test_git_install_then_install_ref(script: CpipTestEnvironment) -> None:
    """The Git backend should be able to install a ref after a package has
    already been installed.
    """
    version_pkg_path = create_test_package(script.scratch_path)
    add_ref(script, version_pkg_path, "refs/foo/bar")
    change_test_package_version(script, version_pkg_path)

    version = install_version_pkg(script, version_pkg_path)
    assert version == "some different version"

    # Now install the ref.
    version = install_version_pkg(
        script,
        version_pkg_path,
        rev="refs/foo/bar",
        # WARNING: Did not find branch or tag ..., assuming revision or ref.
        allow_stderr_warning=True,
    )
    assert version == "0.1"


@pytest.mark.network
@pytest.mark.parametrize(
    "rev, expected_sha",
    [
        # Clone the default branch
        ("", "96d6d72ac54132aecbdd5adac88bc8d1f8fb986b"),
        # Clone a specific tag
        ("@0.1.1", "7d654e66c8fa7149c165ddeffa5b56bc06619458"),
        # Clone a specific commit
        (
            "@65cf0a5bdd906ecf48a0ac241c17d656d2071d56",
            "65cf0a5bdd906ecf48a0ac241c17d656d2071d56",
        ),
    ],
)
def test_install_git_logs_commit_sha(
    script: CpipTestEnvironment,
    rev: str,
    expected_sha: str,
    tmpdir: Path,
) -> None:
    """Test installing from a git repository logs a commit SHA."""
    url_path = "pypa/pip-test-package.git"
    base_local_url = github_checkout(url_path, tmpdir)
    local_url = f"{base_local_url}{rev}#egg=pip-test-package"
    result = script.cpip("install", local_url)
    # `[4:]` removes a 'git+' prefix
    assert f"Resolved {base_local_url[4:]} to commit {expected_sha}" in result.stdout


@pytest.mark.network
def test_git_branch_should_not_be_changed(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    """Editable installations should not change branch
    related to issue #32 and #161
    """
    url_path = "pypa/pip-test-package.git"
    local_url = github_checkout(url_path, tmpdir, egg="pip-test-package")
    script.cpip("install", "-e", local_url)
    branch = get_editable_branch(script, "pip-test-package")
    assert branch == "master"


@pytest.mark.network
def test_git_with_non_editable_unpacking(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    """Test cloning a git repository from a non-editable URL with a given tag."""
    url_path = "pypa/pip-test-package.git"
    local_url = github_checkout(
        url_path,
        tmpdir,
        rev="0.1.2",
        egg="pip-test-package",
    )
    result = script.cpip(
        "install",
        local_url,
        allow_stderr_warning=True,
    )
    assert "0.1.2" in result.stdout


@pytest.mark.network
def test_git_with_editable_where_egg_contains_dev_string(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    """Test cloning a git repository from an editable url which contains "dev"
    string
    """
    url_path = "dcramer/django-devserver.git"
    local_url = github_checkout(
        url_path,
        tmpdir,
        egg="django-devserver",
        scheme="https",
    )
    result = script.cpip("install", "-e", local_url)
    result.assert_installed("django-devserver", with_files=[".git"])


@pytest.mark.network
def test_git_with_non_editable_where_egg_contains_dev_string(
    script: CpipTestEnvironment,
    tmpdir: Path,
) -> None:
    """Test cloning a git repository from a non-editable url which contains "dev"
    string
    """
    url_path = "dcramer/django-devserver.git"
    local_url = github_checkout(
        url_path,
        tmpdir,
        egg="django-devserver",
        scheme="https",
    )
    result = script.cpip("install", local_url)
    devserver_folder = script.site_packages / "devserver"
    result.did_create(devserver_folder)


def test_git_with_ambiguous_revs(script: CpipTestEnvironment) -> None:
    """Test git with two "names" (tag/branch) pointing to the same commit"""
    version_pkg_path = create_test_package(script.scratch_path)
    version_pkg_url = make_version_pkg_url(version_pkg_path, rev="0.1")
    script.run("git", "tag", "0.1", cwd=version_pkg_path)
    result = script.cpip("install", "--no-build-isolation", "-e", version_pkg_url)
    assert "Could not find a tag or branch" not in result.stdout
    # it is 'version-pkg' instead of 'version_pkg' because
    # egg-link name is version-pkg.egg-link because it is a single .py module
    result.assert_installed("version_pkg", with_files=[".git"])


def test_editable__no_revision(script: CpipTestEnvironment) -> None:
    """Test a basic install in editable mode specifying no revision."""
    version_pkg_path = create_test_package(script.scratch_path)
    install_version_pkg_only(script, version_pkg_path)

    branch = get_editable_branch(script, "version-pkg")
    assert branch == "master"

    remote = get_branch_remote(script, "version-pkg", "master")
    assert remote == "origin"


def test_editable__branch_with_sha_same_as_default(script: CpipTestEnvironment) -> None:
    """Test installing in editable mode a branch whose sha matches the sha
    of the default branch, but is different from the default branch.
    """
    version_pkg_path = create_test_package(script.scratch_path)
    # Create a second branch with the same SHA.
    script.run("git", "branch", "develop", cwd=version_pkg_path)
    install_version_pkg_only(script, version_pkg_path, rev="develop")

    branch = get_editable_branch(script, "version-pkg")
    assert branch == "develop"

    remote = get_branch_remote(script, "version-pkg", "develop")
    assert remote == "origin"


def test_editable__branch_with_sha_different_from_default(
    script: CpipTestEnvironment,
) -> None:
    """Test installing in editable mode a branch whose sha is different from
    the sha of the default branch.
    """
    version_pkg_path = create_test_package(script.scratch_path)
    # Create a second branch.
    script.run("git", "branch", "develop", cwd=version_pkg_path)
    # Add another commit to the master branch to give it a different sha.
    change_test_package_version(script, version_pkg_path)

    version = install_version_pkg(script, version_pkg_path, rev="develop")
    assert version == "0.1"

    branch = get_editable_branch(script, "version-pkg")
    assert branch == "develop"

    remote = get_branch_remote(script, "version-pkg", "develop")
    assert remote == "origin"


def test_editable__non_master_default_branch(script: CpipTestEnvironment) -> None:
    """Test the branch you get after an editable install from a remote repo
    with a non-master default branch.
    """
    version_pkg_path = create_test_package(script.scratch_path)
    # Change the default branch of the remote repo to a name that is
    # alphabetically after "master".
    script.run("git", "checkout", "-b", "release", cwd=version_pkg_path)
    install_version_pkg_only(script, version_pkg_path)

    branch = get_editable_branch(script, "version-pkg")
    assert branch == "release"


def test_reinstalling_works_with_editable_non_master_branch(
    script: CpipTestEnvironment,
) -> None:
    """Reinstalling an editable installation should not assume that the "master"
    branch exists. See https://github.com/pypa/cpip/issues/4448.
    """
    version_pkg_path = create_test_package(script.scratch_path)

    # Switch the default branch to something other than 'master'
    script.run("git", "branch", "-m", "foobar", cwd=version_pkg_path)

    version = install_version_pkg(script, version_pkg_path)
    assert version == "0.1"

    change_test_package_version(script, version_pkg_path)
    version = install_version_pkg(script, version_pkg_path)
    assert version == "some different version"


# TODO(pnasrat) fix all helpers to do right things with paths on windows.
@pytest.mark.skipif("sys.platform == 'win32'")
@pytest.mark.xfail(
    condition=True,
    reason="Git submodule against file: is not working; waiting for a good solution",
    run=True,
)
def test_check_submodule_addition(script: CpipTestEnvironment) -> None:
    """Submodules are pulled in on install and updated on upgrade."""
    module_path, submodule_path = create_test_package_with_submodule(
        script,
        rel_path="testpkg/static",
    )

    install_result = script.cpip(
        "install",
        "-e",
        f"git+{module_path.as_uri()}#egg=version_pkg",
    )
    install_result.did_create(script.venv / "src/version-pkg/testpkg/static/testfile")

    change_test_package_submodule(script, submodule_path)
    pull_in_submodule_changes_to_module(
        script,
        module_path,
        rel_path="testpkg/static",
    )

    # expect error because git may write to stderr
    update_result = script.cpip(
        "install",
        "-e",
        f"git+{module_path.as_uri()}#egg=version_pkg",
        "--upgrade",
    )

    update_result.did_create(script.venv / "src/version-pkg/testpkg/static/testfile2")


def test_install_git_branch_not_cached(script: CpipTestEnvironment) -> None:
    """Installing git urls with a branch revision does not cause wheel caching."""
    PKG = "gitbranchnotcached"
    repo_dir = create_test_package(script.scratch_path, name=PKG)
    url = make_version_pkg_url(repo_dir, rev="master", name=PKG)
    result = script.cpip("install", "--no-build-isolation", url, "--only-binary=:all:")
    assert f"Successfully built {PKG}" in result.stdout, result.stdout
    script.cpip("uninstall", "-y", PKG)
    # build occurs on the second install too because it is not cached
    result = script.cpip("install", "--no-build-isolation", url)
    assert f"Successfully built {PKG}" in result.stdout, result.stdout


def test_install_git_sha_cached(script: CpipTestEnvironment) -> None:
    """Installing git urls with a sha revision does cause wheel caching."""
    PKG = "gitshacached"
    repo_dir = create_test_package(script.scratch_path, name=PKG)
    commit = script.run("git", "rev-parse", "HEAD", cwd=repo_dir).stdout.strip()
    url = make_version_pkg_url(repo_dir, rev=commit, name=PKG)
    result = script.cpip("install", "--no-build-isolation", url)
    assert f"Successfully built {PKG}" in result.stdout, result.stdout
    script.cpip("uninstall", "-y", PKG)
    # build does not occur on the second install because it is cached
    result = script.cpip("install", "--no-build-isolation", url)
    assert f"Successfully built {PKG}" not in result.stdout, result.stdout
