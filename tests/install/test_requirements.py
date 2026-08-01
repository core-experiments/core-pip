from __future__ import annotations

from cpip.core.packaging import parse_requirement
from cpip.install.requirements import RequirementInstaller
from cpip.resolution.req_install import InstallRequirement


def test_install_requirements_replaces_in_transaction(monkeypatch) -> None:
    requirement = InstallRequirement(parse_requirement("demo==1"))
    requirement.should_reinstall = True

    def fake_install(
        self: RequirementInstaller,
        install_requirement: InstallRequirement,
    ) -> None:
        install_requirement.install_succeeded = True

    monkeypatch.setattr(RequirementInstaller, "install", fake_install)

    result = RequirementInstaller(
        root=None,
        home=None,
        prefix=None,
        use_user_site=False,
        pycompile=True,
    ).install_all([requirement])

    assert result == ["demo"]
    assert requirement.install_succeeded
