"""Information about the Python interpreter running cpip."""

from __future__ import annotations

import sys

CURRENT_PYTHON_VERSION_INFO = sys.version_info
CURRENT_PYTHON_VERSION = (
    f"{CURRENT_PYTHON_VERSION_INFO.major}.{CURRENT_PYTHON_VERSION_INFO.minor}"
)
CURRENT_PYTHON_VERSION_DIGITS = CURRENT_PYTHON_VERSION.replace(".", "")
CURRENT_PYTHON_VERSION_FULL = ".".join(
    str(part) for part in CURRENT_PYTHON_VERSION_INFO[:3]
)
CURRENT_PYTHON_MAJOR_TAG = f"py{CURRENT_PYTHON_VERSION_INFO.major}"
CURRENT_PYTHON_FULL_TAG = f"py{CURRENT_PYTHON_VERSION_DIGITS}"
