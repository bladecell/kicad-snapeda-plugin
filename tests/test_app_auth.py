from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import wx

from snapeda_plugin import app as app_module
from snapeda_plugin.app import CredentialsDialog, ImportRequest, MainFrame
from snapeda_plugin.credential_store import CredentialStore
from snapeda_plugin.http_auth import AuthError
from snapeda_plugin.search import Part


@pytest.fixture(scope="module")
def wx_app() -> wx.App:
    app = wx.App(False)
    yield app
    app.Destroy()


def test_credentials_dialog_masks_password_and_disables_unavailable_keyring(
    wx_app: wx.App,
) -> None:
    dialog = CredentialsDialog(None, CredentialStore())
    try:
        assert dialog.password_input.GetWindowStyleFlag() & wx.TE_PASSWORD
        assert not dialog.remember_checkbox.IsEnabled()
        assert not dialog.remember_checkbox.GetValue()
        assert "will not be remembered" in dialog.message.GetLabel()
    finally:
        dialog.Destroy()


def test_gui_export_availability_uses_selected_search_result_ids(
    wx_app: wx.App,
) -> None:
    frame = MainFrame()
    part = Part(
        part_number="NE555",
        manufacturer="Example",
        url="https://www.snapeda.com/parts/ne555/Example/view-part/",
        preview="",
        has_symbol=True,
        has_footprint=True,
        has_3d=True,
        previews={},
        unipart_id=22_235_084,
        symbol_id=10_314_751,
        footprint_id=10_314_751,
        model_3d_id=12_568_557,
    )
    try:
        frame.parts = [part]
        frame.selected = part
        frame._update_controls()
        assert frame.import_button.IsEnabled()
        assert part.export_part_id == 10_314_751
        assert part.export_part_id != part.model_3d_id

        part_with_conflict = Part(
            part_number="OTHER",
            manufacturer="Example",
            url=part.url,
            preview="",
            has_symbol=True,
            has_footprint=True,
            has_3d=True,
            previews={},
            unipart_id=22_235_084,
            symbol_id=10_314_751,
            footprint_id=10_314_752,
            model_3d_id=12_568_557,
        )
        frame.selected = part_with_conflict
        frame._update_controls()
        assert not frame.import_button.IsEnabled()
        assert not hasattr(frame, "process")
    finally:
        frame.Destroy()


def test_gui_export_worker_passes_selected_ids_not_3d_id(
    wx_app: wx.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    part = Part(
        part_number="NE555",
        manufacturer="Example",
        url="https://www.snapeda.com/parts/ne555/Example/view-part/",
        preview="",
        has_symbol=True,
        has_footprint=True,
        has_3d=True,
        previews={},
        unipart_id=22_235_084,
        symbol_id=10_314_751,
        footprint_id=10_314_751,
        model_3d_id=12_568_557,
    )

    class FakeClient:
        is_authenticated = True

        def __init__(self) -> None:
            self.export_ids: tuple[int, int] | None = None

        def export(self, part_id: int, unipart_id: int, target: Path) -> int:
            self.export_ids = (part_id, unipart_id)
            bundle = io.BytesIO()
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(
                    "Part.kicad_mod",
                    b'(footprint "Part" (version 20240108))',
                )
                archive.writestr(
                    "Part.kicad_sym",
                    b"(kicad_symbol_lib (version 20231120) "
                    b'(generator kicad_symbol_editor) (symbol "Part"))',
                )
                archive.writestr("Part.step", b"ISO-10303-21;")
            data = bundle.getvalue()
            target.write_bytes(data)
            return len(data)

        def close(self) -> None:
            pass

    client = FakeClient()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("browser subprocess started")
        ),
    )
    frame = MainFrame()
    frame.http_client = client  # type: ignore[assignment]
    destination = tmp_path / "libraries"
    destination.mkdir()
    try:
        frame._start_http_import(ImportRequest(part, destination, False, None))
        assert frame.job is not None
        frame.job.join(timeout=5)
        assert not frame.job.is_alive()
        assert client.export_ids == (10_314_751, 22_235_084)
        assert client.export_ids[0] != part.model_3d_id
        wx_app.Yield()
        library = destination / "libs" / "SnapEDA"
        footprint_path = library / "SnapEDA.pretty" / "Part.kicad_mod"
        assert footprint_path.is_file()
        assert (
            library / "SnapEDA.3dshapes" / "NE555" / "Part.step"
        ).read_bytes() == b"ISO-10303-21;"
        assert (
            "${KIPRJMOD}/libs/SnapEDA/SnapEDA.3dshapes/NE555/Part.step"
            in footprint_path.read_text()
        )
        assert "SnapEDA" in (destination / "fp-lib-table").read_text()
        symbol_path = library / "SnapEDA.kicad_sym"
        assert symbol_path.is_file()
        assert "SnapEDA" in (destination / "sym-lib-table").read_text()
        assert frame.status.GetLabel().startswith("Imported to ")
        assert list(tmp_path.glob("snapeda-export-*")) == []
    finally:
        frame.closing = True
        frame.Destroy()


def test_cancelled_credential_dialog_starts_no_login_or_export(
    wx_app: wx.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    part = Part(
        "NE555",
        "Example",
        "https://www.snapeda.com/parts/ne555/Example/view-part/",
        "",
        True,
        True,
        True,
        {},
        22_235_084,
        10_314_751,
        10_314_751,
        12_568_557,
    )

    class CancelDialog:
        password_input = SimpleNamespace(Clear=lambda: None)

        def __init__(self, *_args: object) -> None:
            pass

        def ShowModal(self) -> int:
            return wx.ID_CANCEL

        def Destroy(self) -> None:
            pass

    frame = MainFrame()
    monkeypatch.setattr(app_module, "CredentialsDialog", CancelDialog)
    monkeypatch.setattr(
        app_module.SnapEDAClient,
        "authenticate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("login started")),
    )
    try:
        frame._prompt_credentials(ImportRequest(part, tmp_path, False, None))
        assert frame.http_client is None
        assert frame.job is None
        assert frame.status.GetLabel() == "Sign-in cancelled. No download was started."
    finally:
        frame.Destroy()


def test_expired_export_resets_gui_session_without_automatic_reauthentication(
    wx_app: wx.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExpiredClient:
        is_authenticated = True
        closed = False

        def export(self, _part_id: int, _unipart_id: int, _target: Path) -> int:
            raise AuthError(
                "SnapEDA session expired; sign in again.", session_expired=True
            )

        def close(self) -> None:
            self.closed = True

    frame = MainFrame()
    client = ExpiredClient()
    frame.http_client = client  # type: ignore[assignment]
    frame.auth_button.SetLabel("Sign out")
    part = Part(
        "NE555",
        "Example",
        "https://www.snapeda.com/parts/ne555/Example/view-part/",
        "",
        True,
        True,
        True,
        {},
        22_235_084,
        10_314_751,
        10_314_751,
        12_568_557,
    )
    monkeypatch.setattr(
        app_module.SnapEDAClient,
        "authenticate",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("automatic reauthentication attempted")
        ),
    )
    try:
        frame._start_http_import(
            ImportRequest(part, tmp_path / "libraries", False, None)
        )
        assert frame.job is not None
        frame.job.join(timeout=5)
        assert not frame.job.is_alive()
        wx_app.Yield()
        assert client.closed
        assert frame.http_client is None
        assert frame.auth_button.GetLabel() == "Sign in"
        assert "session expired" in frame.status.GetLabel()
    finally:
        frame.closing = True
        frame.Destroy()
