"""Exit codes owned by the cpip command-line application.

Deliberately importless: ``cli.entrypoint`` needs these two values on every
single invocation, so anything reachable from here is paid for by ``cpip
--version``.
"""

from __future__ import annotations

BROKEN_STDOUT = 120
VIRTUALENV_NOT_FOUND = 3
