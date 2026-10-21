"""Optional isolated Camoufox flow for manual sign-in and part export."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox

from .downloads import MAX_DOWNLOAD_BYTES, wait_for_bounded_download

SNAPEDA = "https://www.snapeda.com"


async def _click_unique_visible(locator: Any, label: str) -> None:
    """Click one visible control, rejecting hidden or ambiguous matches."""
    await locator.wait_for(state="visible", timeout=120_000)
    count = await locator.count()
    if count != 1:
        raise RuntimeError(f"Expected one visible {label}, found {count}")
    await locator.click()


async def _click_modal_option(page: Any, label: str) -> None:
    """Select a visible exact-text option marked as a SnapEDA modal control."""
    if label == "KiCad":
        locator = page.locator('a.s-btn-modal[data-format="KiCad"]:visible').filter(
            has_text=re.compile(r"^\s*KiCad\s*$")
        )
    else:
        locator = page.locator("a.s-btn-modal:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(label)}\s*$")
        )
    await _click_unique_visible(locator, f"SnapEDA {label} modal option")


def _part_export_download_link(page: Any) -> Any:
    return page.locator("a:visible").filter(
        has_text=re.compile(
            r"^[\s\ue000-\uf8ff]*Download Symbol and Footprint[\s\ue000-\uf8ff]*$",
            re.IGNORECASE,
        )
    )


async def download_part(part_url: str, profile_dir: Path) -> Path:
    """Open the selected part in a private browser and save its KiCad export."""
    if not part_url.startswith(f"{SNAPEDA}/parts/"):
        raise ValueError("Only SnapEDA part pages can be exported")
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile_dir, 0o700)
    output = Path(tempfile.mkdtemp(prefix="snapeda-export-"))
    os.chmod(output, 0o700)
    download_directory = output / "browser-downloads"
    download_directory.mkdir(mode=0o700)
    try:
        async with AsyncCamoufox(
            headless=False,
            user_data_dir=str(profile_dir),
            persistent_context=True,
            downloads_path=str(download_directory),
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(SNAPEDA, wait_until="domcontentloaded", timeout=120_000)
            login_link = page.get_by_role(
                "link", name=re.compile("^Log In$", re.IGNORECASE)
            )
            if await login_link.count() and await login_link.first.is_visible():
                print(
                    "Sign in to SnapEDA in the open browser, then return here.",
                    file=sys.stderr,
                    flush=True,
                )
                await login_link.first.wait_for(state="hidden", timeout=600_000)
            await page.goto(part_url, wait_until="domcontentloaded", timeout=120_000)
            download_link = _part_export_download_link(page)
            await _click_unique_visible(download_link, "part export link")
            await _click_modal_option(page, "KiCad")
            await _click_modal_option(page, "V6 & Later")
            async with page.expect_download(timeout=240_000) as pending:
                await _click_modal_option(page, "Download")
            download = await pending.value
            await wait_for_bounded_download(
                download, download_directory, max_bytes=MAX_DOWNLOAD_BYTES
            )
            filename = Path(download.suggested_filename or "snapeda-export.zip").name
            if filename in {"", ".", ".."}:
                filename = "snapeda-export.zip"
            target = output / filename
            await download.save_as(target)
            if target.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise ValueError("saved export exceeds the permitted download size")
        shutil.rmtree(download_directory, ignore_errors=True)
        return target
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def run_download(part_url: str, profile_dir: Path) -> Path:
    """Run browser automation from the system-Python GUI thread."""
    return asyncio.run(download_part(part_url, profile_dir))
