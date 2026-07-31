from __future__ import annotations

import sys


def running_under_virtualenv() -> bool:
    """Check whether the interpreter is running in a PEP 405 virtualenv."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)
