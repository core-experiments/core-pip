"""Requirement-file and pylock parsing for the resolution input boundary."""

from cpip.resolution.engine.input.files.models import (
    ParsedRequirement,
    RequirementsFileParseError,
)
from cpip.resolution.engine.input.files.options import (
    expand_env_variables,
    merge_config_setting,
    normalize_reference,
    strip_matching_quotes,
)
from cpip.resolution.engine.input.files.parser import (
    parse_line,
    parse_requirement_line,
    parse_requirements,
    parse_requirements_internal,
)
from cpip.resolution.engine.input.files.pylock import (
    is_pylock_reference,
    parse_pylock,
    pylock_location,
)

__all__ = [
    "ParsedRequirement",
    "RequirementsFileParseError",
    "expand_env_variables",
    "is_pylock_reference",
    "merge_config_setting",
    "normalize_reference",
    "parse_line",
    "parse_pylock",
    "parse_requirement_line",
    "parse_requirements",
    "parse_requirements_internal",
    "pylock_location",
    "strip_matching_quotes",
]
