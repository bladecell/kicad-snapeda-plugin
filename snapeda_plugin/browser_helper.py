"""Command-line bridge run by the dedicated browser Python environment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .downloads import MAX_DOWNLOAD_BYTES


def limit_download_file_size() -> None:
    """Apply a hard per-file cap to this isolated helper and its browser child."""
    if sys.platform.startswith("linux"):
        import resource

        _, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
        soft_limit = (
            MAX_DOWNLOAD_BYTES
            if hard_limit == resource.RLIM_INFINITY
            else min(MAX_DOWNLOAD_BYTES, hard_limit)
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft_limit, soft_limit))


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: browser_helper PART_URL PROFILE_DIR")
    limit_download_file_size()
    from .browser import run_download

    print(json.dumps(str(run_download(sys.argv[1], Path(sys.argv[2])))))


if __name__ == "__main__":
    main()
