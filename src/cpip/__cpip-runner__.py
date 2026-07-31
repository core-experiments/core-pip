"""Execute exactly this copy of cpip, within a different environment.

This file is named as it is, to ensure that this module can't be imported via
an import statement.
"""

# /!\ This version compatibility check section must be Python 2 compatible. /!\

import sys

# Copied from pyproject.toml
PYTHON_REQUIRES = (3, 10)


def version_str(version):
    return ".".join(str(v) for v in version)


if sys.version_info[:2] < PYTHON_REQUIRES:
    raise SystemExit(
        "This version of cpip does not support python {} (requires >={}).".format(
            version_str(sys.version_info[:2]), version_str(PYTHON_REQUIRES)
        )
    )

# From here on, we can use Python 3 features, but the syntax must remain
# Python 2 compatible.

import runpy  # noqa: E402
from os.path import dirname  # noqa: E402

CPIP_SOURCES_ROOT = dirname(dirname(__file__))
CPIP_PACKAGE_ROOT = dirname(__file__)
if CPIP_PACKAGE_ROOT in sys.path:
    sys.path.remove(CPIP_PACKAGE_ROOT)
if CPIP_SOURCES_ROOT not in sys.path:
    sys.path.insert(0, CPIP_SOURCES_ROOT)

assert __name__ == "__main__", "Cannot run __cpip-runner__.py as a non-main module"
runpy.run_module("cpip", run_name="__main__", alter_sys=True)
