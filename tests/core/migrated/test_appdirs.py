import sys
from pathlib import Path

from pip.core import appdirs


def test_user_cache_dir(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
        expected = home / "local" / "pip" / "Cache"
    elif sys.platform == "darwin":
        expected = home / "Library" / "Caches" / "pip"
    else:
        expected = home / ".cache" / "pip"
    assert Path(appdirs.user_cache_dir("pip")) == expected


def test_user_cache_dir_override(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "other-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(override))
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        expected = Path(appdirs.user_cache_dir("pip"))
    else:
        expected = override / "pip"
    assert Path(appdirs.user_cache_dir("pip")) == expected


def test_user_config_dir_override(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "other-config"
    if sys.platform == "darwin":
        monkeypatch.setenv("XDG_DATA_HOME", str(override))
        monkeypatch.setattr("os.path.isdir", lambda path: True)
    elif sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(override))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(override))
    assert Path(appdirs.user_config_dir("pip")) == override / "pip"


def test_site_config_dirs_linux(monkeypatch) -> None:
    if sys.platform != "linux":
        return
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    assert appdirs.site_config_dirs("pip") == ["/etc/xdg/pip", "/etc"]
