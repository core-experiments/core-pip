"""Backend detection for an editable checkout spawns no VCS command when the
checkout's own directory carries the marker directory."""

from __future__ import annotations

from pathlib import Path

import pytest
from cpip.vcs.git import Git
from cpip.vcs.versioncontrol import VersionControl, vcs


@pytest.fixture
def no_vcs_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    vcs._ensure_builtin_backends_loaded()

    def refuse(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise AssertionError(f"{cls.name} command spawned: {args[0]}")

    for backend in vcs.registry_internal.values():
        monkeypatch.setattr(type(backend), "run_command", classmethod(refuse))
    monkeypatch.setattr(VersionControl, "run_command", classmethod(refuse))


def test_marker_in_location_decides_the_backend_without_probing(
    tmp_path: Path, no_vcs_commands: None
) -> None:
    checkout = tmp_path / "project"
    (checkout / ".git").mkdir(parents=True)

    backend = vcs.get_backend_for_dir(str(checkout))

    assert isinstance(backend, Git)


def test_marker_in_location_wins_over_a_marker_above_it(
    tmp_path: Path, no_vcs_commands: None
) -> None:
    """The innermost repository owns the directory, as before."""
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "vendored"
    (nested / ".hg").mkdir(parents=True)

    backend = vcs.get_backend_for_dir(str(nested))

    assert backend is not None
    assert backend.name == "hg"


def test_no_marker_still_asks_the_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "project"
    checkout.mkdir()
    vcs._ensure_builtin_backends_loaded()
    asked: list[str] = []

    def record(cls, location):  # noqa: ANN001, ANN202
        asked.append(cls.name)
        return None

    for backend in vcs.registry_internal.values():
        monkeypatch.setattr(type(backend), "get_repository_root", classmethod(record))

    assert vcs.get_backend_for_dir(str(checkout)) is None
    assert sorted(asked) == sorted(vcs.registry_internal)


def test_git_subdirectory_of_a_repository_root_needs_no_git(
    tmp_path: Path, no_vcs_commands: None
) -> None:
    (tmp_path / ".git").mkdir()

    assert Git.get_subdirectory(str(tmp_path)) is None


def test_git_subdirectory_with_a_gitdir_file_still_asks_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree or submodule carries a .git file; its root is git's call."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / "pkg" / "pyproject.toml").write_text("[project]\nname = 'pkg'\n")
    (worktree / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'x'}\n")
    commands: list[list[str]] = []

    def fake_run(cls, args, **kwargs):  # noqa: ANN001, ANN003, ANN202
        commands.append(list(args))
        return str(worktree / ".git") + "\n"

    monkeypatch.setattr(Git, "run_command", classmethod(fake_run))

    assert Git.get_subdirectory(str(worktree / "pkg")) == "pkg"
    assert commands == [["rev-parse", "--git-dir"]]
