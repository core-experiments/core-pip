"""Version control system support for pip."""

# Import the concrete backends so their registration side effects happen when
# any VCS service is used.  Keeping this at the package boundary avoids making
# every caller know which backend modules must be imported first.
from . import bazaar, git, mercurial, subversion  # noqa: F401
