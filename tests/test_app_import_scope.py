from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import wx

from snapeda_plugin import app as app_module
from snapeda_plugin.app import MainFrame
from snapeda_plugin.credential_store import CredentialStore
from snapeda_plugin.search import Part


@pytest.fixture(scope="module")
def wx_app() -> wx.App:
    app = wx.App(False)
    yield app
    app.Destroy()


def part() -> Part:
    return Part(
        part_number="LM358",
        manufacturer="Example",
        url="https://www.snapeda.com/parts/lm358/Example/view-part/",
        preview="",
        has_symbol=True,
        has_footprint=True,
        has_3d=False,
        previews={},
        unipart_id=12,
        symbol_id=34,
        footprint_id=34,
        model_3d_id=None,
    )


class ChoiceDialog:
    selection = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def ShowModal(self) -> int:
        return wx.ID_OK

    def GetSelection(self) -> int:
        return self.selection

    def Destroy(self) -> None:
        pass


class FolderDialog:
    path = ""
    calls = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).calls += 1

    def __enter__(self) -> FolderDialog:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def ShowModal(self) -> int:
        return wx.ID_OK

    def GetPath(self) -> str:
        return self.path


def prepare_frame(monkeypatch: pytest.MonkeyPatch) -> MainFrame:
    monkeypatch.setattr(CredentialStore, "detect", lambda: CredentialStore())
    frame = MainFrame()
    frame.selected = part()
    frame.http_client = SimpleNamespace(is_authenticated=True)  # type: ignore[assignment]
    return frame


def test_project_scope_uses_detected_root_without_folder_picker(
    wx_app: wx.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "board"
    project.mkdir()
    (project / "board.kicad_pro").touch()
    original_project = tmp_path / "previous-project"
    original_project.mkdir()
    (original_project / "previous.kicad_pro").touch()
    ChoiceDialog.selection = 0
    FolderDialog.calls = 0
    monkeypatch.setattr(wx, "SingleChoiceDialog", ChoiceDialog)
    monkeypatch.setattr(wx, "DirDialog", FolderDialog)
    confirmations: list[str] = []
    monkeypatch.setattr(
        wx,
        "MessageBox",
        lambda message, *_args, **_kwargs: confirmations.append(message) or wx.ID_OK,
    )
    monkeypatch.setattr(app_module, "active_project_root", lambda: original_project)
    frame = prepare_frame(monkeypatch)
    monkeypatch.setattr(app_module, "active_project_root", lambda: project)
    requests = []
    monkeypatch.setattr(frame, "_start_http_import", requests.append)
    try:
        frame.on_import(None)
        assert FolderDialog.calls == 0
        assert len(requests) == 1
        assert requests[0].destination == project
        assert not requests[0].global_scope
        assert str(project) in confirmations[0]
    finally:
        frame.Destroy()


def test_global_scope_uses_chosen_folder_and_global_table_directory(
    wx_app: wx.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom_assets = tmp_path / "global-assets"
    config = tmp_path / "kicad-config"
    ChoiceDialog.selection = 1
    FolderDialog.path = str(custom_assets)
    FolderDialog.calls = 0
    monkeypatch.setattr(wx, "SingleChoiceDialog", ChoiceDialog)
    monkeypatch.setattr(wx, "DirDialog", FolderDialog)
    monkeypatch.setattr(wx, "MessageBox", lambda *_args, **_kwargs: wx.ID_OK)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    detection_calls: list[None] = []

    def unexpected_project_detection() -> Path | None:
        detection_calls.append(None)
        raise AssertionError("global imports do not need project IPC")

    monkeypatch.setattr(app_module, "active_project_root", unexpected_project_detection)
    frame = prepare_frame(monkeypatch)
    requests = []
    monkeypatch.setattr(frame, "_start_http_import", requests.append)
    try:
        frame.on_import(None)
        assert FolderDialog.calls == 1
        assert len(requests) == 1
        assert requests[0].destination == custom_assets
        assert requests[0].table_dir == config
        assert requests[0].global_scope
        assert detection_calls == []
    finally:
        frame.Destroy()


def test_missing_project_context_uses_picker_instead_of_cached_project(
    wx_app: wx.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected_project = tmp_path / "selected-project"
    selected_project.mkdir()
    ChoiceDialog.selection = 0
    FolderDialog.path = str(selected_project)
    FolderDialog.calls = 0
    monkeypatch.setattr(wx, "SingleChoiceDialog", ChoiceDialog)
    monkeypatch.setattr(wx, "DirDialog", FolderDialog)
    confirmations: list[str] = []
    monkeypatch.setattr(
        wx,
        "MessageBox",
        lambda message, *_args, **_kwargs: confirmations.append(message) or wx.ID_OK,
    )
    monkeypatch.setattr(app_module, "active_project_root", lambda: None)
    frame = prepare_frame(monkeypatch)
    requests = []
    monkeypatch.setattr(frame, "_start_http_import", requests.append)
    try:
        frame.on_import(None)
        assert FolderDialog.calls == 1
        assert len(requests) == 1
        assert requests[0].destination == selected_project
        assert str(selected_project) in confirmations[0]
    finally:
        frame.Destroy()
