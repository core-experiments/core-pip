"""
locations.py tests

"""

import getpass
import os
import shutil
import sys
import tempfile
from typing import Any
from unittest.mock import Mock

from pip.platform.locations._sysconfig import get_scheme
from pip.platform.scheme import SCHEME_KEYS

if sys.platform == "win32":
    pwd = Mock()
else:
    import pwd


def _get_scheme_dict(*args: Any, **kwargs: Any) -> dict[str, str]:
    scheme = get_scheme(*args, **kwargs)
    return {k: getattr(scheme, k) for k in SCHEME_KEYS}


class TestScheme:
    def setup_method(self) -> None:
        self.tempdir = tempfile.mkdtemp()
        self.st_uid = 9999
        self.username = "example"
        self.patch()

    def teardown_method(self) -> None:
        self.revert_patch()
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def patch(self) -> None:
        """first store and then patch python methods pythons"""
        self.tempfile_gettempdir = tempfile.gettempdir
        self.old_os_fstat = os.fstat
        if sys.platform != "win32":
            # os.geteuid and pwd.getpwuid are not implemented on windows
            self.old_os_geteuid = os.geteuid
            self.old_pwd_getpwuid = pwd.getpwuid
        self.old_getpass_getuser = getpass.getuser

        # now patch
        tempfile.gettempdir = lambda: self.tempdir
        getpass.getuser = lambda: self.username
        os.fstat = lambda fd: self.get_mock_fstat(fd)
        if sys.platform != "win32":
            os.geteuid = lambda: self.st_uid
            pwd.getpwuid = self.get_mock_getpwuid

    def revert_patch(self) -> None:
        """revert the patches to python methods"""
        tempfile.gettempdir = self.tempfile_gettempdir
        getpass.getuser = self.old_getpass_getuser
        if sys.platform != "win32":
            # os.geteuid and pwd.getpwuid are not implemented on windows
            os.geteuid = self.old_os_geteuid
            pwd.getpwuid = self.old_pwd_getpwuid
        os.fstat = self.old_os_fstat

    def get_mock_fstat(self, fd: int) -> os.stat_result:
        """returns a basic mock fstat call result.
        Currently only the st_uid attribute has been set.
        """
        result = Mock()
        result.st_uid = self.st_uid
        return result

    def get_mock_getpwuid(self, uid: int) -> Any:
        """returns a basic mock pwd.getpwuid call result.
        Currently only the pw_name attribute has been set.
        """
        result = Mock()
        result.pw_name = self.username
        return result


class TestLocations:
    def test_root_modifies_appropriately(self) -> None:
        # This deals with nt/posix path differences
        # root is c:\somewhere\else or /somewhere/else
        root = os.path.normcase(
            os.path.abspath(os.path.join(os.path.sep, "somewhere", "else"))
        )
        norm_scheme = _get_scheme_dict("example")
        root_scheme = _get_scheme_dict("example", root=root)

        for key, value in norm_scheme.items():
            drive, path = os.path.splitdrive(os.path.abspath(value))
            expected = os.path.join(root, path[1:])
            assert os.path.abspath(root_scheme[key]) == expected

    def test_prefix_modifies_appropriately(self) -> None:
        prefix = os.path.abspath(os.path.join("somewhere", "else"))

        normal_scheme = _get_scheme_dict("example")
        prefix_scheme = _get_scheme_dict("example", prefix=prefix)

        def _calculate_expected(value: str) -> str:
            path = os.path.join(prefix, os.path.relpath(value, sys.prefix))
            return os.path.normpath(path)

        expected = {k: _calculate_expected(v) for k, v in normal_scheme.items()}
        assert prefix_scheme == expected
