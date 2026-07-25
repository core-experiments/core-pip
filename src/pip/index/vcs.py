"""Version-control URL parsing and source-tree materialization."""

from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse
import shutil
from pathlib import Path

from pip.index.source_models import VcsReference

VCS_SCHEMES = ("git", "hg", "svn", "bzr")


def vcs_scheme(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if "+" not in parsed.scheme:
        if parsed.scheme in VCS_SCHEMES:
            return parsed.scheme
        return None
    vcs, _, _ = parsed.scheme.partition("+")
    return vcs or None


def vcs_reference(url: str) -> VcsReference:
    vcs = vcs_scheme(url)
    if vcs is None:
        raise OSError(f"Unsupported VCS URL: {url}")
    parsed_url = urllib.parse.urlparse(url)
    bare_url = parsed_url._replace(
        scheme=parsed_url.scheme.partition("+")[2] or parsed_url.scheme,
        fragment="",
    ).geturl()
    parsed = urllib.parse.urlsplit(bare_url)
    requested_revision = None
    path = parsed.path
    if "@" in path:
        path, requested_revision = path.rsplit("@", 1)
        if requested_revision == "":
            raise OSError(f"VCS URL has an empty revision: {url}")
    repo_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )
    if requested_revision is not None:
        requested_revision = urllib.parse.unquote(requested_revision)
    return VcsReference(
        vcs=vcs, repo_url=repo_url, requested_revision=requested_revision
    )


def materialize_vcs(
    url: str, *, emit_resolution: bool = True, prompting: bool = True
) -> Path:
    reference = vcs_reference(url)
    if reference.vcs != "git":
        raise OSError(f"Unsupported VCS URL: {url}")
    target = Path(tempfile.mkdtemp(prefix="pip-index-vcs-"))
    environment = os.environ.copy()
    if not prompting:
        environment["GIT_TERMINAL_PROMPT"] = "0"
    process = subprocess.run(
        ["git", "clone", reference.repo_url, str(target)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        shutil.rmtree(target, ignore_errors=True)
        raise OSError(f"Failed to clone {url}: {detail}")
    if reference.requested_revision is not None:
        process = subprocess.run(
            ["git", "checkout", "-q", reference.requested_revision],
            cwd=target,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            fetch = subprocess.run(
                ["git", "fetch", "-q", "origin", reference.requested_revision],
                cwd=target,
                text=True,
                capture_output=True,
                check=False,
            )
            if fetch.returncode == 0:
                process = subprocess.run(
                    ["git", "checkout", "-q", "FETCH_HEAD"],
                    cwd=target,
                    text=True,
                    capture_output=True,
                    check=False,
                )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()
            shutil.rmtree(target, ignore_errors=True)
            raise OSError(f"Failed to checkout {url}: {detail}")
    commit_id = git_revision(target)
    if emit_resolution and not os.environ.get("PIP_QUIET"):
        print(f"Resolved {reference.repo_url} to commit {commit_id}")
    return target


def git_revision(source_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def is_immutable_vcs_link(url: str) -> bool:
    if vcs_scheme(url) != "git":
        return False
    try:
        revision = vcs_reference(url).requested_revision
    except OSError:
        return False
    return bool(
        revision
        and len(revision) == 40
        and all(character in "0123456789abcdefABCDEF" for character in revision)
    )
