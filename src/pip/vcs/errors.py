from pip.core.errors import InstallationError


class BadCommand(InstallationError):
    """A required VCS executable could not be run."""


__all__ = ["BadCommand"]
