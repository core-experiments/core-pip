import sysconfig
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from cpip.core import target_python


class Testcompatibility_tags:
    def mock_get_config_var(self, **kwd: str) -> Callable[[str], Any]:
        """Patch sysconfig.get_config_var for arbitrary keys."""
        get_config_var = sysconfig.get_config_var

        def mock_get_config_var_internal(var: str) -> Any:
            if var in kwd:
                return kwd[var]
            return get_config_var(var)

        return mock_get_config_var_internal

    def test_no_hyphen_tag(self) -> None:
        """Test that no tag contains a hyphen."""
        mock_gcf = self.mock_get_config_var(SOABI="cpython-35m-darwin")

        with patch("sysconfig.get_config_var", mock_gcf):
            supported = target_python.get_supported()

        for tag in supported:
            assert "-" not in tag.interpreter
            assert "-" not in tag.abi
            assert "-" not in tag.platform


class TestManylinux2010Tags:
    @pytest.mark.parametrize(
        "manylinux2010,manylinux1",
        [
            ("manylinux2010_x86_64", "manylinux1_x86_64"),
            ("manylinux2010_i686", "manylinux1_i686"),
        ],
    )
    def test_manylinux2010_implies_manylinux1(
        self,
        manylinux2010: str,
        manylinux1: str,
    ) -> None:
        """Specifying manylinux2010 implies manylinux1."""
        groups: dict[tuple[str, str], list[str]] = {}
        supported = target_python.get_supported(platforms=[manylinux2010])
        for tag in supported:
            groups.setdefault((tag.interpreter, tag.abi), []).append(tag.platform)

        for arches in groups.values():
            if arches == ["any"]:
                continue
            assert arches[:2] == [manylinux2010, manylinux1]


class TestManylinux2014Tags:
    @pytest.mark.parametrize(
        "manylinuxA,manylinuxB",
        [
            ("manylinux2014_x86_64", ["manylinux2010_x86_64", "manylinux1_x86_64"]),
            ("manylinux2014_i686", ["manylinux2010_i686", "manylinux1_i686"]),
        ],
    )
    def test_manylinuxA_implies_manylinuxB(
        self,
        manylinuxA: str,
        manylinuxB: list[str],
    ) -> None:
        """Specifying manylinux2014 implies manylinux2010/manylinux1."""
        groups: dict[tuple[str, str], list[str]] = {}
        supported = target_python.get_supported(platforms=[manylinuxA])
        for tag in supported:
            groups.setdefault((tag.interpreter, tag.abi), []).append(tag.platform)

        expected_arches = [manylinuxA]
        expected_arches.extend(manylinuxB)
        for arches in groups.values():
            if arches == ["any"]:
                continue
            assert arches[:3] == expected_arches
