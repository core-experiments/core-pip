from __future__ import annotations

import datetime
import sys


def parse_iso_datetime(isodate: str) -> datetime.datetime:
    """Parse ISO datetimes, including trailing ``Z`` on older Python versions."""
    if sys.version_info >= (3, 11):
        return datetime.datetime.fromisoformat(isodate)
    return datetime.datetime.fromisoformat(
        isodate.replace("Z", "+00:00")
        if isodate.endswith("Z") and ("T" in isodate or " " in isodate.strip())
        else isodate,
    )
