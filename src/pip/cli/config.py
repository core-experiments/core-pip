from __future__ import annotations

import configparser
import os
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

from pip.core.errors import ConfigurationError

CONFIG_BASENAME = "pip.conf" if os.name != "nt" else "pip.ini"


class RawConfigParser_internal(configparser.RawConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@dataclass(frozen=True)
class ConfigLocation:
    kind: str
    path: Path


@dataclass(frozen=True)
class ConfigDebugView:
    env_vars: tuple[tuple[str, str], ...]
    locations: tuple[ConfigLocation, ...]
    values: tuple[tuple[str, str, str], ...]


class ConfigurationStore:
    def __init__(self) -> None:
        self.parser_internal = new_parser()
        self.env_internal: dict[str, str] = {}

    def load(self) -> None:
        self.parser_internal = new_parser()
        self.env_internal = {}
        for key, value in os.environ.items():
            if not key.startswith("PIP_"):
                continue
            if key in {"PIP_VERSION", "PIP_HELP", "PIP_CONFIG_FILE"}:
                continue
            self.env_internal[key[4:].lower().replace("_", "-")] = value
        for location in config_locations():
            if location.path.is_file():
                try:
                    self.parser_internal.read(location.path, encoding="utf-8")
                except configparser.Error as exc:
                    raise ConfigurationError(str(exc)) from exc

    def get(self, key: str) -> str:
        if key.startswith(":env:."):
            option = key[len(":env:.") :]
            if option in self.env_internal:
                return self.env_internal[option]
            raise ConfigurationError(f"No such key - {key}")
        section, option = split_key(key)
        for candidate in option_spellings(option):
            if self.parser_internal.has_option(section, candidate):
                return self.parser_internal.get(section, candidate)
        raise ConfigurationError(f"No such key - {key}")

    def get_optional(self, key: str) -> str | None:
        try:
            return self.get(key)
        except ConfigurationError:
            return None

    def set(self, location: ConfigLocation, key: str, value: str) -> None:
        parser = self.read_single(location.path)
        section, option = split_key(key)
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, option.replace("_", "-"), value)
        write_parser(location.path, parser)

    def unset(self, location: ConfigLocation, key: str) -> None:
        parser = self.read_single(location.path)
        section, option = split_key(key)
        if not parser.has_section(section):
            raise ConfigurationError(f"No such key - {key}")
        removed = False
        for candidate in option_spellings(option):
            removed = parser.remove_option(section, candidate) or removed
        if not removed:
            raise ConfigurationError(f"No such key - {key}")
        if not list(parser.items(section)):
            parser.remove_section(section)
        write_parser(location.path, parser)

    def items(self) -> list[tuple[str, str]]:
        values: dict[str, str] = {}
        for section in self.parser_internal.sections():
            for option, value in self.parser_internal.items(section):
                values[f"{section}.{option.replace('_', '-')}"] = value
        return sorted(values.items())

    def debug_view(self) -> ConfigDebugView:
        values: list[tuple[str, str, str]] = []
        for location in config_locations():
            parser = self.read_single(location.path)
            for section in parser.sections():
                for option, value in parser.items(section):
                    values.append(
                        (
                            location.kind,
                            f"{section}.{option.replace('_', '-')}",
                            value,
                        )
                    )
        env_vars = sorted(
            (key, value)
            for key, value in os.environ.items()
            if key.startswith("PIP_")
            and key not in {"PIP_VERSION", "PIP_HELP"}
            and key != "PIP_CONFIG_FILE"
        )
        return ConfigDebugView(
            env_vars=tuple(env_vars),
            locations=tuple(config_locations()),
            values=tuple(values),
        )

    def read_single(self, path: Path) -> RawConfigParser_internal:
        parser = new_parser()
        if path.is_file():
            parser.read(path, encoding="utf-8")
        return parser


def config_locations() -> list[ConfigLocation]:
    config_dirs = os.environ.get("XDG_CONFIG_DIRS")
    if config_dirs and config_dirs.split(os.pathsep)[0]:
        global_path = Path(config_dirs.split(os.pathsep)[0]) / "pip" / CONFIG_BASENAME
    else:
        global_path = Path("/etc") / "pip.conf"
    locations = [ConfigLocation("global", global_path)]
    env_config = os.environ.get("PIP_CONFIG_FILE")
    locations.append(ConfigLocation("user", user_config_path()))
    prefix = os.environ.get("VIRTUAL_ENV") or sys.prefix
    executable_prefix = Path(sys.executable).parent.parent
    if (executable_prefix / "pyvenv.cfg").is_file():
        # Relocated virtualenv launchers can retain the template's
        # ``sys.prefix``.  The executable's environment is the one whose
        # site-level pip.conf should apply.
        prefix = os.fspath(executable_prefix)
    purelib = Path(
        sysconfig.get_path("purelib", vars={"base": prefix, "platbase": prefix})
    )
    site_path = Path(prefix) / CONFIG_BASENAME
    for parent in purelib.parents:
        candidate = parent / CONFIG_BASENAME
        if candidate.parent == Path(prefix):
            site_path = candidate
            break
    locations.append(ConfigLocation("site", site_path))
    if env_config:
        locations.append(ConfigLocation("env", Path(env_config).expanduser()))
    return locations


def split_key(key: str) -> tuple[str, str]:
    if "." not in key:
        raise ConfigurationError(
            "Key does not contain dot separated section and key. "
            "Perhaps you wanted to use 'global.index-url' instead?"
        )
    section, option = key.split(".", 1)
    if not section or not option:
        raise ConfigurationError(f"Invalid configuration key: {key}")
    return section, option


def option_spellings(option: str) -> tuple[str, ...]:
    dotted = option.replace("_", "-")
    underscored = option.replace("-", "_")
    if dotted == underscored:
        return (dotted,)
    return (dotted, underscored)


def write_parser(path: Path, parser: RawConfigParser_internal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        parser.write(file)


def new_parser() -> RawConfigParser_internal:
    return RawConfigParser_internal()


def user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "pip" / CONFIG_BASENAME
    return Path.home() / ".config" / "pip" / CONFIG_BASENAME
