from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from collections.abc import Generator
from types import TracebackType

from typing import Any

from pip.core.temp_dir import TempDirectory

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def get_build_tracker() -> Generator[BuildTracker, None, None]:
    root = os.environ.get("PIP_BUILD_TRACKER")
    if root is not None:
        with BuildTracker(root) as tracker:
            yield tracker
        return

    with TempDirectory(kind="build-tracker") as temporary_directory:
        root = temporary_directory.path
        previous = os.environ.get("PIP_BUILD_TRACKER")
        os.environ["PIP_BUILD_TRACKER"] = root
        try:
            logger.debug("Initialized build tracking at %s", root)
            with BuildTracker(root) as tracker:
                yield tracker
        finally:
            if previous is None:
                del os.environ["PIP_BUILD_TRACKER"]
            else:
                os.environ["PIP_BUILD_TRACKER"] = previous


class TrackerId(str):
    """Uniquely identifying string provided to the build tracker."""


class BuildTracker:
    """Ensure that an sdist cannot request itself as a setup requirement.

    When an sdist is prepared, it identifies its setup requirements in the
    context of ``BuildTracker.track()``. If a requirement shows up recursively, this
    raises an exception.

    This stops fork bombs embedded in malicious packages."""

    def __init__(self, root: str) -> None:
        self.root_internal = root
        self.entries_internal: dict[TrackerId, Any] = {}
        logger.debug("Created build tracker: %s", self.root_internal)

    def __enter__(self) -> BuildTracker:
        logger.debug("Entered build tracker: %s", self.root_internal)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.cleanup()

    def entry_path_internal(self, key: TrackerId) -> str:
        hashed = hashlib.sha224(key.encode()).hexdigest()
        return os.path.join(self.root_internal, hashed)

    def add(self, req: Any, key: TrackerId) -> None:
        """Add an InstallRequirement to build tracking."""

        # Get the file to write information about this requirement.
        entry_path = self.entry_path_internal(key)

        # Try reading from the file. If it exists and can be read from, a build
        # is already in progress, so a LookupError is raised.
        try:
            with open(entry_path) as fp:
                contents = fp.read()
        except FileNotFoundError:
            pass
        else:
            message = f"{req.link} is already being built: {contents}"
            raise LookupError(message)

        # If we're here, req should really not be building already.
        assert key not in self.entries_internal

        # Start tracking this requirement.
        with open(entry_path, "w", encoding="utf-8") as fp:
            fp.write(str(req))
        self.entries_internal[key] = req

        logger.debug("Added %s to build tracker %r", req, self.root_internal)

    def remove(self, req: Any, key: TrackerId) -> None:
        """Remove an InstallRequirement from build tracking."""

        # Delete the created file and the corresponding entry.
        os.unlink(self.entry_path_internal(key))
        del self.entries_internal[key]

        logger.debug("Removed %s from build tracker %r", req, self.root_internal)

    def cleanup(self) -> None:
        for key, req in list(self.entries_internal.items()):
            self.remove(req, key)

        logger.debug("Removed build tracker: %r", self.root_internal)

    @contextlib.contextmanager
    def track(self, req: Any, key: str) -> Generator[None, None, None]:
        """Ensure that `key` cannot install itself as a setup requirement.

        :raises LookupError: If `key` was already provided in a parent invocation of
                             the context introduced by this method."""
        tracker_id = TrackerId(key)
        self.add(req, tracker_id)
        yield
        self.remove(req, tracker_id)
