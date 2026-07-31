"""
tests specific to uninstalling --user installs
"""

from os.path import isdir, isfile, normcase

import pytest

from .test_install_user import patch_dist_in_site_packages
from cpip_test_support import CpipTestEnvironment, TestData, assert_all_changes
from cpip_test_support.venv import VirtualEnvironment
from cpip_test_support.wheel import make_wheel


@pytest.mark.usefixtures("enable_user_site")
class Tests_UninstallUserSite:
    @pytest.mark.network
    def test_uninstall_from_usersite(self, script: CpipTestEnvironment) -> None:
        """
        Test uninstall from usersite
        """
        result1 = script.cpip("install", "--user", "INITools==0.3")
        result2 = script.cpip("uninstall", "-y", "INITools")
        assert_all_changes(result1, result2, [script.venv / "build", "cache"])

    def test_uninstall_from_usersite_with_dist_in_global_site(
        self, virtualenv: VirtualEnvironment, script: CpipTestEnvironment
    ) -> None:
        """
        Test uninstall from usersite (with same dist in global site)
        """
        entry_points_txt = "[console_scripts]\nscript = pkg:func"
        make_wheel(
            "pkg",
            "0.1",
            extra_metadata_files={"entry_points.txt": entry_points_txt},
        ).save_to_dir(script.scratch_path)
        make_wheel(
            "pkg",
            "0.1.1",
            extra_metadata_files={"entry_points.txt": entry_points_txt},
        ).save_to_dir(script.scratch_path)

        patch_dist_in_site_packages(virtualenv)

        script.cpip(
            "install",
            "--no-index",
            "--find-links",
            script.scratch_path,
            "--no-warn-script-location",
            "pkg==0.1",
        )

        result2 = script.cpip(
            "install",
            "--no-index",
            "--find-links",
            script.scratch_path,
            "--no-warn-script-location",
            "--user",
            "pkg==0.1.1",
        )
        result3 = script.cpip("uninstall", "-vy", "pkg")

        # uninstall console is mentioning user scripts, but not global scripts
        assert normcase(script.user_bin_path) in result3.stdout, str(result3)
        assert normcase(script.bin_path) not in result3.stdout, str(result3)

        # uninstall worked
        assert_all_changes(result2, result3, [script.venv / "build", "cache"])

        # site still has 0.2 (can't look in result1; have to check)
        dist_info_folder = script.base_path / script.site_packages / "pkg-0.1.dist-info"
        assert isdir(dist_info_folder)

    def test_uninstall_editable_from_usersite(
        self, script: CpipTestEnvironment, data: TestData
    ) -> None:
        """
        Test uninstall editable local user install
        """
        assert script.user_site_path.exists()

        # install
        to_install = data.packages.joinpath("FSPkg")
        result1 = script.run(
            "python", "setup.py", "develop", "--user", "--prefix=", cwd=to_install
        )
        egg_link = script.user_site / "FSPkg.egg-link"
        result1.did_create(egg_link)

        # uninstall
        result2 = script.cpip("uninstall", "-y", "FSPkg")
        assert not isfile(script.base_path / egg_link)

        assert_all_changes(
            result1,
            result2,
            [
                script.venv / "build",
                "cache",
                script.user_site / "easy-install.pth",
            ],
        )
