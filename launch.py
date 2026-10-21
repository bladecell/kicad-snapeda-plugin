"""Launch the native window from KiCad's system Python."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    if str(root.parent) not in sys.path:
        sys.path.insert(0, str(root.parent))
    from snapeda_plugin.app import main as run

    run()


if __name__ == "__main__":
    main()
