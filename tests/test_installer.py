from __future__ import annotations

from pathlib import Path

import pytest

from install_plugin import install_copy, plugin_install_directory


def test_installer_uses_kicad_10_documents_plugin_directory(tmp_path: Path) -> None:
    assert plugin_install_directory(tmp_path) == (
        tmp_path / "10.0" / "plugins" / "snapeda-parts-search"
    )


def test_installer_does_not_overwrite_existing_plugin(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "KiCad" / "10.0" / "plugins" / "snapeda-parts-search"
    source.mkdir()
    (source / "plugin.json").write_text("new")
    destination.mkdir(parents=True)
    (destination / "plugin.json").write_text("existing")

    with pytest.raises(FileExistsError):
        install_copy(source, destination)
    assert (destination / "plugin.json").read_text() == "existing"
