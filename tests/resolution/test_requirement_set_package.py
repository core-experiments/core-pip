from __future__ import annotations

import pytest
from cpip.install.requirement_set import RequirementSet
from cpip.resolution.input_requirements import (
    install_req_from_editable,
    install_req_from_line,
)
from cpip.resolution.req_install import InstallRequirement


def test_add_named_requirement_tracks_lookup_by_canonical_name() -> None:
    reqset = RequirementSet()
    req = install_req_from_line("My_Pkg==1.0")
    reqset.add_named_requirement(req)

    assert reqset.has_requirement("my-pkg")
    assert reqset.has_requirement("my_pkg")
    assert reqset.get_requirement("my.pkg") is req
    assert list(reqset.requirements) == ["my-pkg"]


def test_add_unnamed_requirement_tracks_direct_requirements() -> None:
    reqset = RequirementSet()
    req = install_req_from_editable("svn+https://example.com/project#egg=project")
    reqset.add_unnamed_requirement(req)

    assert reqset.unnamed_requirements == [req]
    assert reqset.all_requirements == [req]


def test_all_requirements_preserves_named_then_unnamed_in_insertion_order() -> None:
    reqset = RequirementSet()
    alpha = install_req_from_line("alpha==1.0")
    beta = install_req_from_line("beta==2.0")
    direct = install_req_from_editable("svn+https://example.com/project#egg=project")

    reqset.add_named_requirement(alpha)
    reqset.add_named_requirement(beta)
    reqset.add_unnamed_requirement(direct)

    assert reqset.all_requirements == [alpha, beta, direct]


def test_add_named_requirement_requires_parsed_requirement() -> None:
    reqset = RequirementSet()
    with pytest.raises(
        ValueError,
        match="named requirements must define a parsed requirement",
    ):
        reqset.add_named_requirement(InstallRequirement(None))
