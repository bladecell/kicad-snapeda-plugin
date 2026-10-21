from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from snapeda_plugin.downloads import wait_for_bounded_download
import snapeda_plugin.browser_helper as browser_helper


class FakeDownload:
    def __init__(self, path: Path) -> None:
        self.download_path = path
        self.cancelled = False
        self.finished = asyncio.Event()

    async def failure(self) -> str | None:
        await self.finished.wait()
        return "canceled" if self.cancelled else None

    async def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()

    async def path(self) -> Path:
        return self.download_path


def test_bounded_download_returns_completed_path(tmp_path: Path) -> None:
    async def run() -> Path:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        path = downloads / "asset.zip"
        path.write_bytes(b"1234")
        download = FakeDownload(path)
        download.finished.set()
        return await wait_for_bounded_download(download, downloads, max_bytes=4)

    assert asyncio.run(run()).name == "asset.zip"


def test_oversize_in_progress_download_is_cancelled(tmp_path: Path) -> None:
    async def run() -> FakeDownload:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        path = downloads / "asset.zip.part"
        path.write_bytes(b"12")
        download = FakeDownload(path)

        async def exceed_limit() -> None:
            await asyncio.sleep(0.005)
            path.write_bytes(b"12345")

        growth = asyncio.create_task(exceed_limit())
        with pytest.raises(ValueError, match="download limit"):
            await wait_for_bounded_download(
                download, downloads, max_bytes=4, poll_interval=0.001
            )
        await growth
        return download

    result = asyncio.run(run())
    assert result.cancelled


def test_temporary_download_directory_uses_aggregate_size(tmp_path: Path) -> None:
    async def run() -> FakeDownload:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        path = downloads / "asset.zip.part"
        path.write_bytes(b"123")
        (downloads / "second-part").write_bytes(b"45")
        download = FakeDownload(path)
        with pytest.raises(ValueError, match="download limit"):
            await wait_for_bounded_download(
                download, downloads, max_bytes=4, poll_interval=0.001
            )
        return download

    assert asyncio.run(run()).cancelled


def test_download_fails_closed_when_temporary_size_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        download = FakeDownload(downloads / "unfinished")
        task = asyncio.create_task(
            wait_for_bounded_download(download, downloads, max_bytes=4)
        )
        await asyncio.sleep(0.002)
        (downloads / "unexpected").symlink_to(tmp_path / "missing")
        # Symlinks are rejected rather than followed during quota accounting.
        with pytest.raises(ValueError, match="unsafe download entry"):
            await task

    asyncio.run(run())


def test_download_path_must_remain_inside_monitored_directory(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        external = tmp_path / "outside.zip"
        external.write_bytes(b"data")
        download = FakeDownload(external)
        download.finished.set()
        with pytest.raises(ValueError, match="outside monitored"):
            await wait_for_bounded_download(download, downloads, max_bytes=10)

    asyncio.run(run())


def test_helper_sets_hard_file_size_limit_only_for_linux_child_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import resource

    limits: list[tuple[int, int]] = []
    monkeypatch.setattr(browser_helper.sys, "platform", "linux")
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda _kind: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        resource, "setrlimit", lambda _kind, value: limits.append(value)
    )
    browser_helper.limit_download_file_size()
    assert limits == [(512 * 1024 * 1024, 512 * 1024 * 1024)]
