"""Fetch the existing PCM package index, accepting only HTTP 404 as absent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


def fetch_existing_index(url: str, destination: Path, *, timeout: float = 15) -> bool:
    """Save an existing index, returning false only when the server returns 404."""
    try:
        with urlopen(url, timeout=timeout) as response:
            content = response.read()
    except HTTPError as error:
        if error.code == 404:
            error.close()
            destination.unlink(missing_ok=True)
            return False
        raise

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        found = fetch_existing_index(args.url, args.output)
    except OSError as error:
        print(f"Could not fetch prior PCM index: {error}", file=sys.stderr)
        return 2
    if found:
        print(f"Fetched prior package index: {args.output}")
    else:
        print("No prior package index exists (HTTP 404); starting a new repository.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
