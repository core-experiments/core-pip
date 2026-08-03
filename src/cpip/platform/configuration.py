"""Configuration management setup

Some terminology:
- name
  As written in config files.
- value
  Value associated with a name
- key
  Name combined with it's section (section.name)
- variant
  A single word describing where the configuration key-value pair came from
"""

from __future__ import annotations

import configparser
import locale
import logging
import os
import sys
from collections.abc import Iterable
from typing import Any, NewType

from cpip.core.appdirs import site_config_dirs, user_config_dir
from cpip.core.errors import (
    ConfigurationError,
)
from cpip.core.filesystem import ensure_dir
from cpip.core.misc import enum

WINDOWS = os.name == "nt"


class ConfigurationFileCouldNotBeLoaded(ConfigurationError):
    """A configuration file could not be decoded or parsed."""

    def __init__(
        self,
        reason: str = "could not be loaded",
        fname: str | None = None,
        error: configparser.Error | None = None,
    ) -> None:
        super().__init__(error)
        self.reason = reason
        self.fname = fname
        self.error = error


def get_locale_encoding() -> str:
    """Return the locale encoding without triggering UTF-8 mode warnings."""
    if sys.version_info >= (3, 11):
        return locale.getencoding()
    return locale.getpreferredencoding(False)


RawConfigParser = configparser.RawConfigParser  # Shorthand
Kind = NewType("Kind", str)

CONFIG_BASENAME = "cpip.ini" if WINDOWS else "cpip.conf"
ENV_NAMES_IGNORED = "version", "help"

# The kinds of configurations there are.
kinds = enum(
    USER="user",  # User Specific
    GLOBAL="global",  # System Wide
    SITE="site",  # [Virtual] Environment Specific
    ENV="env",  # from CPIP_CONFIG_FILE
    ENV_VAR="env-var",  # from Environment Variables
)
OVERRIDE_ORDER = kinds.GLOBAL, kinds.USER, kinds.SITE, kinds.ENV, kinds.ENV_VAR
VALID_LOAD_ONLY = kinds.USER, kinds.GLOBAL, kinds.SITE

logger = logging.getLogger(__name__)


# NOTE: Maybe use the optionx attribute to normalize keynames.
def normalize_name(name: str) -> str:
    """Make a name consistent regardless of source (environment or file)"""
    name = name.lower().replace("_", "-")
    name = name.removeprefix("--")  # only prefer long opts
    return name


def disassemble_key(name: str) -> list[str]:
    if "." not in name:
        error_message = (
            "Key does not contain dot separated section and key. "
            f"Perhaps you wanted to use 'global.{name}' instead?"
        )
        raise ConfigurationError(error_message)
    return name.split(".", 1)


def get_configuration_files() -> dict[Kind, list[str]]:
    global_config_files = [
        os.path.join(path, CONFIG_BASENAME) for path in site_config_dirs("cpip")
    ]

    site_config_file = os.path.join(sys.prefix, CONFIG_BASENAME)
    legacy_config_file = os.path.join(
        os.path.expanduser("~"),
        "cpip" if WINDOWS else ".cpip",
        CONFIG_BASENAME,
    )
    new_config_file = os.path.join(user_config_dir("cpip"), CONFIG_BASENAME)
    return {
        kinds.GLOBAL: global_config_files,
        kinds.SITE: [site_config_file],
        kinds.USER: [legacy_config_file, new_config_file],
    }


class Configuration:
    """Handles management of configuration.

    Provides an interface to accessing and managing configuration files.

    This class converts provides an API that takes "section.key-name" style
    keys and stores the value associated with it as "key-name" under the
    section "section".

    This allows for a clean interface wherein the both the section and the
    key-name are preserved in an easy to manage form in the configuration files
    and the data stored is also nice.
    """

    def __init__(self, isolated: bool, load_only: Kind | None = None) -> None:
        super().__init__()

        if load_only is not None and load_only not in VALID_LOAD_ONLY:
            raise ConfigurationError(
                "Got invalid value for load_only - should be one of {}".format(
                    ", ".join(map(repr, VALID_LOAD_ONLY)),
                ),
            )
        self.isolated = isolated
        self.load_only = load_only

        # Because we keep track of where we got the data from
        self.parsers_internal: dict[Kind, list[tuple[str, RawConfigParser]]] = {
            variant: [] for variant in OVERRIDE_ORDER
        }
        self.config_internal: dict[Kind, dict[str, dict[str, Any]]] = {
            variant: {} for variant in OVERRIDE_ORDER
        }
        self.modified_parsers: list[tuple[str, RawConfigParser]] = []

    def load(self) -> None:
        """Loads configuration from configuration files and environment"""
        self.load_config_files()
        if not self.isolated:
            self.load_environment_vars()

    def get_file_to_edit(self) -> str | None:
        """Returns the file with highest priority in configuration"""
        assert self.load_only is not None, "Need to be specified a file to be editing"

        try:
            return self.get_parser_to_modify()[0]
        except IndexError:
            return None

    def items(self) -> Iterable[tuple[str, Any]]:
        """Returns key-value pairs like dict.items() representing the loaded
        configuration
        """
        return self.dictionary.items()

    def get_value(self, key: str) -> Any:
        """Get a value from the configuration."""
        orig_key = key
        key = normalize_name(key)
        try:
            clean_config: dict[str, Any] = {}
            for file_values in self.dictionary.values():
                clean_config.update(file_values)
            return clean_config[key]
        except KeyError:
            # disassembling triggers a more useful error message than simply
            # "No such key" in the case that the key isn't in the form command.option
            disassemble_key(key)
            raise ConfigurationError(f"No such key - {orig_key}")

    def set_value(self, key: str, value: Any) -> None:
        """Modify a value in the configuration."""
        key = normalize_name(key)
        self.ensure_have_load_only()

        assert self.load_only
        fname, parser = self.get_parser_to_modify()

        if parser is not None:
            section, name = disassemble_key(key)

            # Modify the parser and the configuration
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, name, value)

        self.config_internal[self.load_only].setdefault(fname, {})
        self.config_internal[self.load_only][fname][key] = value
        self.mark_as_modified(fname, parser)

    def unset_value(self, key: str) -> None:
        """Unset a value in the configuration."""
        orig_key = key
        key = normalize_name(key)
        self.ensure_have_load_only()

        assert self.load_only
        fname, parser = self.get_parser_to_modify()

        if (
            key not in self.config_internal[self.load_only][fname]
            and key not in self.config_internal[self.load_only]
        ):
            raise ConfigurationError(f"No such key - {orig_key}")

        if parser is not None:
            section, name = disassemble_key(key)
            if not (parser.has_section(section) and parser.remove_option(section, name)):
                # The option was not removed.
                raise ConfigurationError(
                    "Fatal Internal error [id=1]. Please report as a bug.",
                )

            # The section may be empty after the option was removed.
            if not parser.items(section):
                parser.remove_section(section)
            self.mark_as_modified(fname, parser)
        try:
            del self.config_internal[self.load_only][fname][key]
        except KeyError:
            del self.config_internal[self.load_only][key]

    def save(self) -> None:
        """Save the current in-memory state."""
        self.ensure_have_load_only()

        for fname, parser in self.modified_parsers:
            logger.info("Writing to %s", fname)

            # Ensure directory exists.
            ensure_dir(os.path.dirname(fname))

            # Ensure directory's permission(need to be writeable)
            try:
                with open(fname, "w") as f:
                    parser.write(f)
            except OSError as error:
                raise ConfigurationError(
                    f"An error occurred while writing to the configuration file {fname}: {error}",
                )

    #
    # Private routines
    #

    def ensure_have_load_only(self) -> None:
        if self.load_only is None:
            raise ConfigurationError("Needed a specific file to be modifying.")
        logger.debug("Will be working with %s variant only", self.load_only)

    @property
    def dictionary(self) -> dict[str, dict[str, Any]]:
        """A dictionary representing the loaded configuration."""
        # NOTE: Dictionaries are not populated if not loaded. So, conditionals
        #       are not needed here.
        retval = {}

        for variant in OVERRIDE_ORDER:
            retval.update(self.config_internal[variant])

        return retval

    def load_config_files(self) -> None:
        """Loads configuration from configuration files"""
        config_files = dict(self.iter_config_files())
        if config_files[kinds.ENV][0:1] == [os.devnull]:
            logger.debug(
                "Skipping loading configuration files due to "
                "environment's CPIP_CONFIG_FILE being os.devnull",
            )
            return

        for variant, files in config_files.items():
            for fname in files:
                # If there's specific variant set in `load_only`, load only
                # that variant, not the others.
                if self.load_only is not None and variant != self.load_only:
                    logger.debug("Skipping file '%s' (variant: %s)", fname, variant)
                    continue

                parser = self.load_file(variant, fname)

                # Keeping track of the parsers used
                self.parsers_internal[variant].append((fname, parser))

    def load_file(self, variant: Kind, fname: str) -> RawConfigParser:
        logger.log(15, "For variant '%s', will try loading '%s'", variant, fname)
        parser = self.construct_parser(fname)

        for section in parser.sections():
            items = parser.items(section)
            self.config_internal[variant].setdefault(fname, {})
            self.config_internal[variant][fname].update(
                self.normalized_keys(section, items),
            )

        return parser

    def construct_parser(self, fname: str) -> RawConfigParser:
        parser = configparser.RawConfigParser()
        # If there is no such file, don't bother reading it but create the
        # parser anyway, to hold the data.
        # Doing this is useful when modifying and saving files, where we don't
        # need to construct a parser.
        locale_encoding = get_locale_encoding()
        try:
            parser.read(fname, encoding=locale_encoding)
        except UnicodeDecodeError:
            # See https://github.com/pypa/cpip/issues/4963
            raise ConfigurationFileCouldNotBeLoaded(
                reason=f"contains invalid {locale_encoding} characters",
                fname=fname,
            )
        except configparser.Error as error:
            # See https://github.com/pypa/cpip/issues/4893
            raise ConfigurationFileCouldNotBeLoaded(error=error)
        return parser

    def load_environment_vars(self) -> None:
        """Loads configuration from environment variables"""
        self.config_internal[kinds.ENV_VAR].setdefault(":env:", {})
        self.config_internal[kinds.ENV_VAR][":env:"].update(
            self.normalized_keys(":env:", self.get_environ_vars()),
        )

    def normalized_keys(
        self,
        section: str,
        items: Iterable[tuple[str, Any]],
    ) -> dict[str, Any]:
        """Normalizes items to construct a dictionary with normalized keys.

        This routine is where the names become keys and are made the same
        regardless of source - configuration files or environment.
        """
        normalized = {}
        for name, val in items:
            key = section + "." + normalize_name(name)
            normalized[key] = val
        return normalized

    def get_environ_vars(self) -> Iterable[tuple[str, str]]:
        """Returns a generator with all environmental vars with prefix CPIP_"""
        for key, val in os.environ.items():
            if key.startswith("CPIP_"):
                name = key[4:].lower()
                if name not in ENV_NAMES_IGNORED:
                    yield name, val

    # XXX: This is patched in the tests.
    def iter_config_files(self) -> Iterable[tuple[Kind, list[str]]]:
        """Yields variant and configuration files associated with it.

        This should be treated like items of a dictionary. The order
        here doesn't affect what gets overridden. That is controlled
        by OVERRIDE_ORDER. However this does control the order they are
        displayed to the user. It's probably most ergonomic to display
        things in the same order as OVERRIDE_ORDER
        """
        # SMELL: Move the conditions out of this function

        env_config_file = os.environ.get("CPIP_CONFIG_FILE", None)
        config_files = get_configuration_files()

        yield kinds.GLOBAL, config_files[kinds.GLOBAL]

        # per-user config is not loaded when env_config_file exists
        should_load_user_config = not self.isolated and not (
            env_config_file and os.path.exists(env_config_file)
        )
        if should_load_user_config:
            # The legacy config file is overridden by the new config file
            yield kinds.USER, config_files[kinds.USER]

        # virtualenv config
        yield kinds.SITE, config_files[kinds.SITE]

        if env_config_file is not None:
            yield kinds.ENV, [env_config_file]
        else:
            yield kinds.ENV, []

    def get_values_in_config(self, variant: Kind) -> dict[str, Any]:
        """Get values present in a config file"""
        return self.config_internal[variant]

    def get_parser_to_modify(self) -> tuple[str, RawConfigParser]:
        # Determine which parser to modify
        assert self.load_only
        parsers = self.parsers_internal[self.load_only]
        if not parsers:
            # This should not happen if everything works correctly.
            raise ConfigurationError(
                "Fatal Internal error [id=2]. Please report as a bug.",
            )

        # Use the highest priority parser.
        return parsers[-1]

    # XXX: This is patched in the tests.
    def mark_as_modified(self, fname: str, parser: RawConfigParser) -> None:
        file_parser_tuple = (fname, parser)
        if file_parser_tuple not in self.modified_parsers:
            self.modified_parsers.append(file_parser_tuple)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.dictionary!r})"
