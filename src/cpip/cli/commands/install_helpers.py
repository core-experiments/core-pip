"""Small, testable operations shared by the install command."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cpip.core.hashes import Hashes
from cpip.install.target import InstallTarget


def normalize_install_args(args: list[str], options: frozenset[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in options and index + 1 < len(args):
            normalized.append(f"{token}={args[index + 1]}")
            index += 2
            continue
        normalized.append(token)
        index += 1
    return normalized


def target_library_is_empty(target: InstallTarget) -> bool:
    """Return whether target-mode library roots contain any entries."""
    seen: set[Path] = set()
    for root in target.library_roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            with os.scandir(root) as entries:
                if next(entries, None) is not None:
                    return False
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            return False
    return True


def intersect_hashes(left: Hashes, right: Hashes) -> Hashes:
    return Hashes(
        {
            algorithm: [
                digest
                for digest in left.allowed_internal.get(algorithm, [])
                if digest in right.allowed_internal.get(algorithm, [])
            ]
            for algorithm in left.allowed_internal.keys()
            & right.allowed_internal.keys()
        },
    )


def install_candidate(
    candidate: Any,
    options: Any,
    *,
    requested: bool,
    reinstall: bool,
    direct_url: Any = None,
) -> None:
    """Install one prepared candidate using the command's target options."""
    from cpip.cli.context import target_prefix as target_prefix_internal
    from cpip.install.wheel_transaction import WheelInstaller

    target = InstallTarget.from_options(
        candidate.canonical_name,
        target=options.target,
        user=options.user,
        root=options.root,
        prefix=options.prefix or target_prefix_internal(),
    )
    WheelInstaller(
        target,
        pycompile=not options.no_compile,
        force=reinstall or (direct_url is not None and direct_url.is_local_editable()),
        preserve_existing=options.ignore_installed,
    ).install(candidate.path, requested=requested, direct_url=direct_url)
