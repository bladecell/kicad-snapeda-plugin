"""Bound the temporary files used by Playwright browser downloads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class Download(Protocol):
    async def failure(self) -> str | None: ...

    async def cancel(self) -> None: ...

    async def path(self) -> Path: ...


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"unsafe download entry: {path.name}")
        if path.is_file():
            total += path.stat().st_size
    return total


async def wait_for_bounded_download(
    download: Download,
    directory: Path,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    poll_interval: float = 0.05,
) -> Path:
    """Poll temporary download files and cancel the browser at the byte cap."""
    if max_bytes < 1 or poll_interval <= 0:
        raise ValueError("download limit and poll interval must be positive")
    completion = asyncio.create_task(download.failure())
    while True:
        try:
            size = _directory_size(directory)
        except OSError as exc:
            await download.cancel()
            await completion
            raise ValueError("cannot safely inspect temporary download size") from exc
        except ValueError:
            await download.cancel()
            await completion
            raise
        if size > max_bytes:
            await download.cancel()
            await completion
            raise ValueError(f"download limit exceeded ({max_bytes} bytes)")
        if completion.done():
            break
        await asyncio.sleep(poll_interval)
    failure = await completion
    if failure:
        raise RuntimeError(f"SnapEDA export failed: {failure}")
    downloaded_path = await download.path()
    try:
        resolved_path = downloaded_path.resolve(strict=True)
        download_root = directory.resolve(strict=True)
    except OSError as exc:
        raise ValueError("download output is missing") from exc
    try:
        resolved_path.relative_to(download_root)
    except ValueError as exc:
        raise ValueError(
            "download path is outside monitored temporary directory"
        ) from exc
    if downloaded_path.is_symlink() or not resolved_path.is_file():
        raise ValueError("download output is not a regular file")
    if resolved_path.stat().st_size > max_bytes:
        raise ValueError(f"download limit exceeded ({max_bytes} bytes)")
    return resolved_path
