import sys

from pip.core import appdirs


def test_user_cache_dir(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    expected = (
        "/home/test/.cache/pip"
        if sys.platform not in {"win32", "darwin"}
        else appdirs.user_cache_dir("pip")
    )
    assert appdirs.user_cache_dir("pip") == expected


def test_user_cache_dir_override(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/test/.other-cache")
    assert appdirs.user_cache_dir("pip") == "/home/test/.other-cache/pip"


def test_user_config_dir_override(monkeypatch) -> None:
    if sys.platform == "darwin":
        monkeypatch.setenv("XDG_DATA_HOME", "/home/test/.other-config")
        monkeypatch.setattr("os.path.isdir", lambda path: True)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/home/test/.other-config")
    assert appdirs.user_config_dir("pip") == "/home/test/.other-config/pip"


def test_site_config_dirs_linux(monkeypatch) -> None:
    if sys.platform != "linux":
        return
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    assert appdirs.site_config_dirs("pip") == ["/etc/xdg/pip", "/etc"]
