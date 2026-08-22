"""Read ``cpip.conf`` / ``cpip.ini`` and the ``CPIP_*`` environment.

This is a reader.  There is no ``cpip config`` command, so nothing here writes
configuration back out; if one is ever added, give it a written spec and tests
rather than restoring the untested writer API this module used to carry.

It also owns where package sources come from -- :class:`SourceConfig`,
:func:`load_source_config`, and :func:`resolve_sources` -- because the answer
is the same for every command and the install fast path needs it without
paying for :mod:`cpip.cli.requirements`.

Keep this module import-light: the install fast path loads it on startup.
"""

from __future__ import annotations

import os
import sys

from cpip.core.errors import ConfigurationError
from cpip.index.config import DEFAULT_INDEX_URL

TYPE_CHECKING = False

if TYPE_CHECKING:
    import configparser

    # Annotation only: argparse is a measurable import on the fast path.
    import argparse

NO_INDEX_VALUES = frozenset(("1", "true", "yes", "on"))

CONFIG_BASENAME = "cpip.conf" if os.name != "nt" else "cpip.ini"


_PARSER_CLASS: type | None = None


def raw_config_parser_class() -> type:
    """The config parser subclass, built on first use: configparser is
    imported only when a configuration file is actually read."""
    global _PARSER_CLASS
    if _PARSER_CLASS is not None:
        return _PARSER_CLASS

    import configparser

    class RawConfigParser_internal(configparser.RawConfigParser):
        def optionxform(self, optionstr: str) -> str:
            return optionstr

    _PARSER_CLASS = RawConfigParser_internal
    return RawConfigParser_internal


def __getattr__(name: str) -> object:
    if name == "RawConfigParser_internal":
        return raw_config_parser_class()
    raise AttributeError(name)


class ConfigLocation:
    __slots__ = ("kind", "path")

    def __init__(self, kind: str, path: str) -> None:
        self.kind = kind
        self.path = path


class ConfigurationStore:
    def __init__(self) -> None:
        self.parser_internal: configparser.RawConfigParser | None = None

    def load(self) -> None:
        paths = [
            os.fspath(location.path)
            for location in config_locations()
            if os.path.isfile(os.fspath(location.path))
        ]
        if not paths:
            # No configuration file: no parser, and configparser stays
            # unimported on this command.
            return

        import configparser

        self.parser_internal = new_parser()
        for path in paths:
            try:
                self.parser_internal.read(path, encoding="utf-8")
            except configparser.Error as exc:
                raise ConfigurationError(str(exc)) from exc

    def get(self, key: str) -> str:
        section, option = split_key(key)
        parser = self.parser_internal
        if parser is not None:
            for candidate in option_spellings(option):
                if parser.has_option(section, candidate):
                    return parser.get(section, candidate)
        raise ConfigurationError(f"No such key - {key}")

    def get_optional(self, key: str) -> str | None:
        try:
            return self.get(key)
        except ConfigurationError:
            return None


def config_locations() -> list[ConfigLocation]:
    config_dirs = os.environ.get("XDG_CONFIG_DIRS")
    if config_dirs and config_dirs.split(os.pathsep)[0]:
        global_path = os.path.join(
            config_dirs.split(os.pathsep)[0],
            "cpip",
            CONFIG_BASENAME,
        )
    else:
        global_path = os.path.join("/etc", "cpip.conf")
    locations = [ConfigLocation("global", global_path)]
    env_config = os.environ.get("CPIP_CONFIG_FILE")
    locations.append(ConfigLocation("user", user_config_path()))
    prefix = os.environ.get("VIRTUAL_ENV") or sys.prefix
    executable_prefix = os.path.dirname(os.path.dirname(sys.executable))
    if os.path.isfile(os.path.join(executable_prefix, "pyvenv.cfg")):
        # Relocated virtualenv launchers can retain the template's
        # ``sys.prefix``.  The executable's environment is the one whose
        # site-level cpip.conf should apply.
        prefix = executable_prefix
    # The site file lives directly under the prefix. (An earlier version
    # walked up from sysconfig's purelib looking for the prefix, which can
    # only ever land on this same path -- and cost every config load the
    # sysconfig and threading imports.)
    locations.append(ConfigLocation("site", os.path.join(prefix, CONFIG_BASENAME)))
    if env_config:
        locations.append(ConfigLocation("env", os.path.expanduser(env_config)))
    return locations


def split_key(key: str) -> tuple[str, str]:
    if "." not in key:
        raise ConfigurationError(
            "Key does not contain dot separated section and key. "
            "Perhaps you wanted to use 'global.index-url' instead?",
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


def new_parser() -> configparser.RawConfigParser:
    return raw_config_parser_class()()


def user_config_path() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "cpip", CONFIG_BASENAME)
    return os.path.join(os.path.expanduser("~"), ".config", CONFIG_BASENAME)


class SourceConfig:
    """Where a command looks for distributions."""

    __slots__ = ("extra_index_urls", "find_links", "index_url", "no_index")

    def __init__(
        self,
        find_links: list[str],
        index_url: str | None,
        extra_index_urls: list[str],
        no_index: bool,
    ) -> None:
        self.find_links = find_links

        self.index_url = index_url

        self.extra_index_urls = extra_index_urls

        self.no_index = no_index


def load_source_config(command: str | None = None) -> SourceConfig:
    """Read configured sources for ``command``, then apply ``CPIP_*`` overrides."""

    store = ConfigurationStore()

    try:
        store.load()

    except ConfigurationError:
        return SourceConfig([], DEFAULT_INDEX_URL, [], False)

    def configured(option: str) -> str | None:
        if command is not None:
            value = store.get_optional(f"{command}.{option}")

            if value is not None:
                return value

        return store.get_optional(f"global.{option}")

    raw_find_links = configured("find-links")

    find_links = (
        []
        if raw_find_links is None
        else [line.strip() for line in raw_find_links.splitlines() if line.strip()]
    )

    index_url = configured("index-url") or DEFAULT_INDEX_URL

    raw_extra_index_urls = configured("extra-index-url")

    extra_index_urls = (
        []
        if raw_extra_index_urls is None
        else [
            line.strip() for line in raw_extra_index_urls.splitlines() if line.strip()
        ]
    )

    no_index_value = configured("no-index")

    no_index = (
        no_index_value is not None and no_index_value.strip().lower() in NO_INDEX_VALUES
    )

    if (value := os.environ.get("CPIP_FIND_LINKS")) is not None:
        find_links = value.split()

    if (value := os.environ.get("CPIP_INDEX_URL")) is not None:
        index_url = value

    if (value := os.environ.get("CPIP_EXTRA_INDEX_URL")) is not None:
        extra_index_urls = value.split()

    if (value := os.environ.get("CPIP_NO_INDEX")) is not None:
        no_index = value.strip().lower() in NO_INDEX_VALUES

    return SourceConfig(find_links, index_url, extra_index_urls, no_index)


def resolve_sources(
    options: argparse.Namespace,
    config: SourceConfig,
) -> SourceConfig:
    """Apply command-line source options over configured defaults.

    ``install`` deliberately does not use this: it concatenates configured and
    command-line find-links and gates the index URL on whether one was given
    explicitly.  See ``install_options.requirement_bundle``.
    """

    find_links = getattr(options, "find_links", None) or config.find_links

    return SourceConfig(
        find_links,
        options.index_url or config.index_url,
        options.extra_index_url or config.extra_index_urls,
        options.no_index or config.no_index,
    )
