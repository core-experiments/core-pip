"""Unit tests for src/cpip/install/wheel_archive_installer.py helpers."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cpip.core.errors import InstallationError
from cpip.core.wheel import wheel_candidate
from cpip.install.target import InstallTarget
from cpip.install.wheel_archive_cache import prepare_cached_wheels
from cpip.install.wheel_archive_installer import (
    _rewrite_metadata,
    install_wheels_from_archive_cache,
)


def test_rewrite_metadata_normalizes_mismatched_name(tmp_path: Path) -> None:
    """A wheel's on-disk METADATA Name is normalized to the resolved candidate

    name, even when that name contains characters isalpha() rejects (a
    hyphen, here) -- most real package names do.
    """
    path = tmp_path / "METADATA"
    path.write_text("Metadata-Version: 2.1\nName: Owner_Demo\nVersion: 1.0\n")
    candidate = SimpleNamespace(name="owner-demo")

    rewritten = _rewrite_metadata(str(path), candidate)

    assert rewritten is not None
    assert "Name: owner-demo\n" in path.read_text()


def test_rewrite_metadata_normalizes_digit_containing_name(tmp_path: Path) -> None:
    path = tmp_path / "METADATA"
    path.write_text("Metadata-Version: 2.1\nName: Numpy2\nVersion: 1.0\n")
    candidate = SimpleNamespace(name="numpy2")

    rewritten = _rewrite_metadata(str(path), candidate)

    assert rewritten is not None
    assert "Name: numpy2\n" in path.read_text()


def test_rewrite_metadata_is_noop_when_already_normalized(tmp_path: Path) -> None:
    path = tmp_path / "METADATA"
    original = "Metadata-Version: 2.1\nName: owner-demo\nVersion: 1.0\n"
    path.write_text(original)
    candidate = SimpleNamespace(name="owner-demo")

    rewritten = _rewrite_metadata(str(path), candidate)

    assert rewritten is None
    assert path.read_text() == original


def _make_wheel(
    directory: Path,
    name: str,
    *,
    shared_module: str | None = None,
    entry_points: str | None = None,
) -> Path:
    """Build a minimal wheel, optionally with a file at ``shared_module`` or

    an ``entry_points.txt``. Two wheels sharing a ``shared_module`` path or a
    console-script name simulate a colliding destination -- both would try
    to write the same file into the target.
    """
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if shared_module is not None:
            archive.writestr(shared_module, f"# from {name}\n")
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if entry_points is not None:
            archive.writestr(f"{name}-1.0.dist-info/entry_points.txt", entry_points)
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


def _prevalidated_candidates(
    tmp_path: Path,
    cache_dir: Path,
    wheel_a: Path,
    wheel_b: Path,
) -> tuple[object, object]:
    """Build two candidates with ``wheel_layout`` already set to a

    CachedWheelArchive, matching what the normal candidate-materialization
    path (``install/output.py:prepare_install_candidates``) already does
    before ``install_wheels_from_archive_cache`` ever runs.
    """
    candidate_a = wheel_candidate(wheel_a).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel_a.read_bytes()).hexdigest()},
        source_kind="wheel",
    )
    candidate_b = wheel_candidate(wheel_b).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel_b.read_bytes()).hexdigest()},
        source_kind="wheel",
    )

    archives = prepare_cached_wheels((candidate_a, candidate_b), str(cache_dir))

    return (
        candidate_a.copy_with(wheel_layout=archives[0]),
        candidate_b.copy_with(wheel_layout=archives[1]),
    )


def test_prevalidated_candidates_still_reject_colliding_files(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    wheel_a = _make_wheel(tmp_path, "pkg_a", shared_module="shared_thing.py")
    wheel_b = _make_wheel(tmp_path, "pkg_b", shared_module="shared_thing.py")
    candidate_a, candidate_b = _prevalidated_candidates(
        tmp_path,
        cache_dir,
        wheel_a,
        wheel_b,
    )

    target = tmp_path / "target"
    install_target = InstallTarget.from_options("pkg_a", target=str(target))

    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_wheels_from_archive_cache(
            [(wheel_a, True, None), (wheel_b, True, None)],
            (candidate_a, candidate_b),
            target=install_target,
            cache_dir=str(cache_dir),
        )


def test_prevalidated_candidates_still_reject_colliding_console_scripts(
    tmp_path: Path,
) -> None:
    """Two unrelated packages both providing a ``mytool`` console script

    must fail the batch, not silently install one script and drop the
    other. Script generation writes via ``os.rename``, which overwrites
    silently on POSIX -- the batch-level destination reservation
    (``_reserve_destination``) is the only thing that can catch this, and it
    must run unconditionally.
    """
    cache_dir = tmp_path / "cache"
    entry_points = "[console_scripts]\nmytool = pkg:main\n"
    wheel_a = _make_wheel(tmp_path, "pkg_a", entry_points=entry_points)
    wheel_b = _make_wheel(tmp_path, "pkg_b", entry_points=entry_points)
    candidate_a, candidate_b = _prevalidated_candidates(
        tmp_path,
        cache_dir,
        wheel_a,
        wheel_b,
    )

    target = tmp_path / "target"
    install_target = InstallTarget.from_options("pkg_a", target=str(target))

    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_wheels_from_archive_cache(
            [(wheel_a, True, None), (wheel_b, True, None)],
            (candidate_a, candidate_b),
            target=install_target,
            cache_dir=str(cache_dir),
        )


def test_record_rows_match_the_files_on_disk(tmp_path: Path) -> None:
    """Rows whose hash is computed from the bytes written (INSTALLER,
    REQUESTED, a rewritten METADATA) must agree with the files themselves."""
    import base64
    import csv

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "Mixed_Case", shared_module="mixed_case.py")
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    target = tmp_path / "target"
    install_target = InstallTarget.from_options("mixed-case", target=str(target))

    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
    )
    assert installed is not None

    dist_info = next(target.glob("*.dist-info"))
    rows = list(csv.reader((dist_info / "RECORD").read_text().splitlines()))
    checked = 0
    for relative, digest, size in rows:
        if not digest:
            continue
        path = target / relative
        data = path.read_bytes()
        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        assert digest == f"sha256={expected.rstrip(b'=').decode('ascii')}", relative
        assert size == str(len(data)), relative
        checked += 1
    assert {row[0].rsplit("/", 1)[-1] for row in rows} >= {
        "INSTALLER",
        "REQUESTED",
        "METADATA",
        "RECORD",
    }
    assert checked >= 3


def _prevalidated_candidates_for(
    tmp_path: Path,
    cache_dir: Path,
    *wheels: Path,
) -> tuple[object, ...]:
    candidates = tuple(
        wheel_candidate(wheel).copy_with(
            source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
            source_kind="wheel",
        )
        for wheel in wheels
    )
    archives = prepare_cached_wheels(candidates, str(cache_dir))
    return tuple(
        candidate.copy_with(wheel_layout=archive)
        for candidate, archive in zip(candidates, archives, strict=True)
    )


def test_archive_route_compiles_bytecode_and_records_it(tmp_path: Path) -> None:
    """With compilation on, the clone route writes the .pyc files and lists
    them in RECORD with real hashes, like the transactional route does."""
    import base64
    import csv

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "pkg_compiled", shared_module="compiled_mod.py")
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    target = tmp_path / "target"
    install_target = InstallTarget.from_options("pkg-compiled", target=str(target))

    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
        pycompile=True,
    )
    assert installed is not None

    compiled = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.pyc"))
    assert compiled, "no bytecode was written"
    assert all("__pycache__/" in path for path in compiled)

    dist_info = next(target.glob("*.dist-info"))
    rows = {
        row[0]: row[1:]
        for row in csv.reader((dist_info / "RECORD").read_text().splitlines())
    }
    for path in compiled:
        digest, size = rows[path]
        data = (target / path).read_bytes()
        expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        assert digest == f"sha256={expected.rstrip(b'=').decode('ascii')}"
        assert size == str(len(data))


def test_transactional_install_with_compilation_takes_the_clone_route(
    tmp_path: Path,
) -> None:
    from cpip.install.wheel_archive_cache import ARCHIVE_CACHE_BUCKET
    from cpip.install.wheel_transaction import install_wheels_transactionally

    cache_dir = tmp_path / "cache"
    wheel = _make_wheel(tmp_path, "pkg_default", shared_module="default_mod.py")
    candidate = wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )
    target = tmp_path / "target"

    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options("pkg-default", target=str(target)),
        pycompile=True,
        lookup_existing=False,
        candidates=[candidate],
        cache_dir=str(cache_dir),
    )

    assert (cache_dir / ARCHIVE_CACHE_BUCKET).is_dir(), "the clone route was not used"
    assert list(target.rglob("*.pyc")), "bytecode was not compiled"


def _make_wheel_with_members(
    directory: Path, name: str, members: dict[str, str]
) -> Path:
    """A wheel carrying arbitrary members besides its dist-info scaffolding."""
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in members.items():
            archive.writestr(path, text)
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


def test_wheel_shipping_its_own_generated_pyc_is_rejected_under_compilation(
    tmp_path: Path,
) -> None:
    """A wheel that ships both a module and the .pyc byte-compilation would
    generate collides on one destination; the clone route rejects it rather
    than overwrite the member and emit a duplicate RECORD row."""
    import sys

    cache_dir = tmp_path / "cache"
    tag = sys.implementation.cache_tag
    wheel = _make_wheel_with_members(
        tmp_path,
        "pkg_self_pyc",
        {"selfmod.py": "x = 1\n", f"__pycache__/selfmod.{tag}.pyc": "stale\n"},
    )
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)
    install_target = InstallTarget.from_options(
        "pkg-self-pyc", target=str(tmp_path / "target")
    )

    with pytest.raises(InstallationError, match="duplicate installation destination"):
        install_wheels_from_archive_cache(
            [(wheel, True, None)],
            (candidate,),
            target=install_target,
            cache_dir=str(cache_dir),
            pycompile=True,
        )

    # Without compilation the .pyc is just a member; nothing collides.
    installed = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=InstallTarget.from_options("pkg-self-pyc", target=str(tmp_path / "t2")),
        cache_dir=str(cache_dir),
        pycompile=False,
    )
    assert installed is not None


def test_unowned_target_pyc_declines_the_clone_route(tmp_path: Path) -> None:
    """An unowned file already at a generated .pyc path makes the route
    decline (to the transactional path) instead of overwriting it in the
    clone after preflight."""
    import sys

    cache_dir = tmp_path / "cache"
    tag = sys.implementation.cache_tag
    wheel = _make_wheel_with_members(tmp_path, "pkg_new", {"newmod.py": "x = 1\n"})
    (candidate,) = _prevalidated_candidates_for(tmp_path, cache_dir, wheel)

    # A populated target holding an unrelated distribution and a stray file
    # exactly where newmod's bytecode would land.
    target = tmp_path / "target"
    (target / "other-9.9.dist-info").mkdir(parents=True)
    (target / "other-9.9.dist-info" / "RECORD").write_text("")
    stray = target / "__pycache__" / f"newmod.{tag}.pyc"
    stray.parent.mkdir(parents=True)
    stray.write_text("not ours\n")

    install_target = InstallTarget.from_options("pkg-new", target=str(target))

    declined = install_wheels_from_archive_cache(
        [(wheel, True, None)],
        (candidate,),
        target=install_target,
        cache_dir=str(cache_dir),
        pycompile=True,
    )
    assert declined is None
    # The stray file is untouched: the route did not clone-and-swap.
    assert stray.read_text() == "not ours\n"
