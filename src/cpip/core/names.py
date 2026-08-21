"""PEP 503 name normalization.

Split out of :mod:`cpip.core.packaging` because this three-line function is
needed on paths that need nothing else from that module -- notably
``cli.fast``, which the entrypoint imports on every command.  Reaching it
through ``packaging`` costs ``platform`` and ``subprocess`` as well, for no
benefit.
"""

from __future__ import annotations

import re
from cpip.core.caches import memoized

NORMALIZE_RE = re.compile(r"[-_.]+")


@memoized(4096)
def canonicalize_name(name: str) -> str:
    # Already canonical (lowercase, "-" the only separator, no runs): the
    # common case for names read from an index or a wheel filename, and
    # four C-level scans where the substitution is a regex pass.
    if name.islower() and "_" not in name and "." not in name and "--" not in name:
        return name
    return NORMALIZE_RE.sub("-", name).lower()
