"""Deterministic resolver cases based on common real-world failure modes."""

from __future__ import annotations

import shutil
from pathlib import Path

from pip.resolution.fast_local_wheelhouse import resolve

from .uv_scenarios import make_metadata_wheel


SIZES = (32, 128, 512)
CASE_NAMES = (
    "many-versions",
    "backtracking",
    "unsatisfiable",
    "extras-conflict",
    "nested-extras",
    "requires-python",
    "large-catalog",
    "no-match",
)
CACHE_STATES = ("cold", "warm")


def _make_versioned_case(root: Path, size: int, *, unsatisfiable: bool) -> dict[str, object]:
    wheelhouse = root / ("unsatisfiable" if unsatisfiable else "backtracking") / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        version = f"{index}.0"
        make_metadata_wheel(wheelhouse, "shared", version)
        make_metadata_wheel(
            wheelhouse,
            "left",
            version,
            requires=(f"shared == {version}",),
        )
        right_shared = index + 1 if unsatisfiable else index
        make_metadata_wheel(
            wheelhouse,
            "right",
            version,
            requires=(f"shared == {right_shared}.0",),
        )
        right_version = index if unsatisfiable else max(1, index - 1)
        make_metadata_wheel(
            wheelhouse,
            "application",
            version,
            requires=(f"left == {version}", f"right == {right_version}.0"),
        )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application",),
        "expected": not unsatisfiable,
    }


def _make_many_versions_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "many-versions" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        make_metadata_wheel(wheelhouse, "shared", f"{index}.0")
    make_metadata_wheel(
        wheelhouse,
        "application",
        "1.0",
        requires=("shared >= 1",),
    )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application",),
        "expected": True,
    }


def _make_extras_conflict_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "extras-conflict" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        make_metadata_wheel(wheelhouse, "shared", f"{index}.0")
    make_metadata_wheel(
        wheelhouse,
        "left",
        "1.0",
        requires=("shared == 1.0",),
    )
    make_metadata_wheel(
        wheelhouse,
        "right",
        "1.0",
        requires=("shared == 2.0",),
    )
    make_metadata_wheel(
        wheelhouse,
        "application",
        "1.0",
        requires=(
            "left == 1.0; extra == 'one'",
            "right == 1.0; extra == 'two'",
        ),
        provides_extras=("one", "two"),
    )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application[one,two]",),
        "expected": False,
    }


def _make_nested_extras_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "nested-extras" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        make_metadata_wheel(wheelhouse, "shared", f"{index}.0")
    make_metadata_wheel(
        wheelhouse,
        "left",
        "1.0",
        requires=("shared == 1.0; extra == 'nested'",),
        provides_extras=("nested",),
    )
    make_metadata_wheel(
        wheelhouse,
        "right",
        "1.0",
        requires=("shared == 2.0; extra == 'nested'",),
        provides_extras=("nested",),
    )
    make_metadata_wheel(
        wheelhouse,
        "application",
        "1.0",
        requires=(
            "left[nested] == 1.0; extra == 'one'",
            "right[nested] == 1.0; extra == 'two'",
        ),
        provides_extras=("one", "two"),
    )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application[one,two]",),
        "expected": False,
    }


def _make_requires_python_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "requires-python" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        make_metadata_wheel(
            wheelhouse,
            "shared",
            f"{index}.0",
            requires_python=None if index == 1 else ">=99",
        )
    make_metadata_wheel(
        wheelhouse,
        "application",
        "1.0",
        requires=("shared >= 1",),
    )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application",),
        "expected": True,
    }


def _make_large_catalog_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "large-catalog" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(size):
        make_metadata_wheel(wheelhouse, f"unrelated-{index}", "1.0")
    make_metadata_wheel(wheelhouse, "application", "1.0")
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application",),
        "expected": True,
    }


def _make_no_match_case(root: Path, size: int) -> dict[str, object]:
    wheelhouse = root / "no-match" / str(size)
    wheelhouse.mkdir(parents=True)
    for index in range(1, size + 1):
        make_metadata_wheel(wheelhouse, "shared", f"{index}.0")
    make_metadata_wheel(
        wheelhouse,
        "application",
        "1.0",
        requires=("shared == 999.0",),
    )
    return {
        "wheelhouse": wheelhouse,
        "requirements": ("application",),
        "expected": False,
    }


def create_cases(root: Path) -> dict[str, dict[int, dict[str, object]]]:
    return {
        "many-versions": {
            size: _make_many_versions_case(root, size) for size in SIZES
        },
        "backtracking": {
            size: _make_versioned_case(root, size, unsatisfiable=False)
            for size in SIZES
        },
        "unsatisfiable": {
            size: _make_versioned_case(root, size, unsatisfiable=True)
            for size in SIZES
        },
        "extras-conflict": {
            size: _make_extras_conflict_case(root, size) for size in SIZES
        },
        "nested-extras": {
            size: _make_nested_extras_case(root, size) for size in SIZES
        },
        "requires-python": {
            size: _make_requires_python_case(root, size) for size in SIZES
        },
        "large-catalog": {
            size: _make_large_catalog_case(root, size) for size in SIZES
        },
        "no-match": {
            size: _make_no_match_case(root, size) for size in SIZES
        },
    }


class LocalResolverRealWorld:
    """Measure fast local resolution across realistic graph failure modes."""

    params = (CASE_NAMES, SIZES, CACHE_STATES)
    param_names = ("case", "versions", "cache_state")
    number = 1
    repeat = 3
    rounds = 1
    warmup_time = 0
    timeout = 180

    @staticmethod
    def setup_cache() -> dict[str, dict[int, dict[str, object]]]:
        root = Path.cwd() / "resolver-real-world"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir()
        return create_cases(root)

    def setup(
        self,
        cases: dict[str, dict[int, dict[str, object]]],
        case: str,
        versions: int,
        cache_state: str,
    ) -> None:
        self.state = cases[case][versions]
        self.cache = (
            Path(self.state["wheelhouse"]).parent.parent
            / "caches"
            / f"{case}-{versions}-{cache_state}"
        )
        shutil.rmtree(self.cache, ignore_errors=True)
        if cache_state == "warm":
            self._resolve()

    def _resolve(self):
        return resolve(
            [str(self.state["wheelhouse"])],
            list(self.state["requirements"]),
            cache_dir=str(self.cache),
        )

    def time_resolve(
        self,
        cases: dict[str, dict[int, dict[str, object]]],
        case: str,
        versions: int,
        cache_state: str,
    ) -> None:
        plan = self._resolve()
        assert (plan is not None) is self.state["expected"]
