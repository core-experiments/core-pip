"""Build the ASV checkout without relying on shell environment prefixes."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root / "src"))
    from cpip.cli.main import main as cpip_main

    return cpip_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
