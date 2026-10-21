"""Install the IPC action into the current user's KiCad 10 plugin directory."""

from __future__ import annotations

import shutil
import os
from pathlib import Path

import wx


def plugin_install_directory(documents_home: Path) -> Path:
    """Return the KiCad 10 IPC plugin location under a documents home."""
    return documents_home / "10.0" / "plugins" / "snapeda-parts-search"


def install_copy(source: Path, destination: Path) -> None:
    """Copy the plugin without replacing existing user content."""
    if destination.exists():
        raise FileExistsError(
            f"Plugin directory already exists; refusing to replace it: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", "__pycache__", "browser-env", "tests"
        ),
    )


def main() -> None:
    source = Path(__file__).resolve().parent
    documents_home = Path(
        os.environ.get(
            "KICAD_DOCUMENTS_HOME",
            str(
                Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
                / "KiCad"
            ),
        )
    )
    destination = plugin_install_directory(documents_home)
    if destination.exists():
        raise FileExistsError(
            f"Plugin directory already exists; refusing to replace it: {destination}"
        )
    _ = wx.App(False)
    if (
        wx.MessageBox(
            f"Install the SnapEDA IPC action to:\n\n{destination}\n\nExisting plugin data will not be replaced. Restart KiCad after installation.",
            "Install SnapEDA for KiCad",
            wx.OK | wx.CANCEL | wx.ICON_QUESTION,
        )
        != wx.ID_OK
    ):
        return
    install_copy(source, destination)
    wx.MessageBox(
        f"Installed. Restart KiCad, open the PCB Editor, and choose Search SnapEDA Parts.\n\n{destination}",
        "Installation complete",
        wx.OK | wx.ICON_INFORMATION,
    )


if __name__ == "__main__":
    main()
