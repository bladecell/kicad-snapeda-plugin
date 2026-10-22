"""Native wxPython search and import window."""

from __future__ import annotations

import os
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse
from pathlib import Path
from threading import Event
from typing import Any

import wx

from .archive import inspect_archive
from .credential_store import CredentialStore
from .http_auth import AuthError, SnapEDAClient
from .images import PreviewToken, inspect_image_header, preview_is_current
from .library import import_component, model_binding_options
from .project_context import active_project_root
from .search import Part, search_parts

_PREVIEW_HOSTS = {
    "www.snapeda.com",
    "images.snapeda.com",
    "snapeda.s3.amazonaws.com",
}


@dataclass(frozen=True, slots=True)
class ImportRequest:
    part: Part
    destination: Path
    global_scope: bool
    table_dir: Path | None


def _is_allowed_preview_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _PREVIEW_HOSTS
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _is_valid_datasheet_url(url: str) -> bool:
    if (
        not isinstance(url, str)
        or not url
        or any(character.isspace() or not character.isprintable() for character in url)
    ):
        return False
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def decode_preview_image(data: bytes) -> wx.Image:
    """Decode a previously size-checked image using wx's supported stream API."""
    info = inspect_image_header(data)
    image_type = wx.BITMAP_TYPE_PNG if info.format == "png" else wx.BITMAP_TYPE_JPEG
    image = wx.Image(BytesIO(data), image_type)
    if not image.IsOk():
        raise ValueError("wx could not decode the preview image")
    return image


def fit_preview_size(
    image_width: int, image_height: int, available_width: int, available_height: int
) -> tuple[int, int] | None:
    """Fit an image into its preview area without changing its aspect ratio."""
    if min(image_width, image_height, available_width, available_height) <= 0:
        return None
    scale = min(available_width / image_width, available_height / image_height)
    return (max(1, int(image_width * scale)), max(1, int(image_height * scale)))


class PreviewCanvas(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent)
        self.SetMinSize((1, 1))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._source: wx.Image | None = None
        self.rendered_size = (0, 0)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._on_size)

    def set_image(self, image: wx.Image | None) -> None:
        self._source = image
        self.Refresh()

    def _on_size(self, event: wx.SizeEvent) -> None:
        self.Refresh()
        event.Skip()

    def _paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.Clear()
        self.rendered_size = (0, 0)
        if self._source is None or not self._source.IsOk():
            return
        width, height = self.GetClientSize()
        size = fit_preview_size(
            self._source.GetWidth(), self._source.GetHeight(), width, height
        )
        if size is None:
            return
        image = self._source.Copy()
        image.Rescale(*size, wx.IMAGE_QUALITY_HIGH)
        bitmap = wx.Bitmap(image)
        self.rendered_size = size
        dc.DrawBitmap(bitmap, (width - size[0]) // 2, (height - size[1]) // 2, True)


class CredentialsDialog(wx.Dialog):
    """Collect a username and masked password, with explicit keyring opt-in."""

    def __init__(
        self,
        parent: wx.Window,
        credential_store: CredentialStore,
        username: str = "",
    ) -> None:
        super().__init__(parent, title="Sign in to SnapEDA")
        self.credential_store = credential_store
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(
            wx.StaticText(
                self, label="Use your SnapEDA account to download this part."
            ),
            0,
            wx.ALL,
            12,
        )
        layout.Add(wx.StaticText(self, label="Username"), 0, wx.LEFT | wx.RIGHT, 12)
        self.username_input = wx.TextCtrl(self, value=username)
        layout.Add(
            self.username_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12
        )
        layout.Add(wx.StaticText(self, label="Password"), 0, wx.LEFT | wx.RIGHT, 12)
        self.password_input = wx.TextCtrl(
            self, style=wx.TE_PASSWORD | wx.TE_PROCESS_ENTER
        )
        layout.Add(
            self.password_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12
        )
        self.remember_checkbox = wx.CheckBox(
            self, label="Remember password in the OS keyring"
        )
        self.remember_checkbox.SetValue(False)
        self.remember_checkbox.Enable(credential_store.available)
        layout.Add(self.remember_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.message = wx.StaticText(self, label="")
        if not credential_store.available:
            self.message.SetLabel(
                "No secure OS keyring is available. The password will not be remembered."
            )
        layout.Add(self.message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(layout)
        self.SetMinSize((390, -1))
        self.remember_checkbox.Bind(wx.EVT_CHECKBOX, self._on_remember_changed)
        self.password_input.Bind(wx.EVT_TEXT_ENTER, self._on_accept)
        self.Bind(wx.EVT_BUTTON, self._on_accept, id=wx.ID_OK)
        self.username_input.SetFocus()

    def _on_remember_changed(self, _event: wx.CommandEvent) -> None:
        if not self.remember_checkbox.GetValue():
            return
        username = self.username_input.GetValue().strip()
        if not username:
            self.message.SetLabel("Enter a username to check for a saved password.")
            return
        if self.password_input.GetValue():
            self.message.SetLabel("Password will be stored after a successful sign in.")
            return
        try:
            saved_password = self.credential_store.get_password(username)
        except Exception:
            self.credential_store.disable()
            self.remember_checkbox.SetValue(False)
            self.remember_checkbox.Disable()
            self.message.SetLabel(
                "The OS keyring could not be accessed. Password remembering is disabled."
            )
            return
        if saved_password:
            self.password_input.SetValue(saved_password)
            self.password_input.SetInsertionPointEnd()
            self.message.SetLabel("Saved password loaded from the OS keyring.")
        else:
            self.message.SetLabel("No saved password. Sign in to remember it securely.")

    def _on_accept(self, _event: wx.CommandEvent) -> None:
        if not self.username_input.GetValue().strip():
            self.message.SetLabel("Enter your SnapEDA username.")
            self.username_input.SetFocus()
            return
        if not self.password_input.GetValue():
            self.message.SetLabel("Enter your SnapEDA password.")
            self.password_input.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class _PreviewRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        if not _is_allowed_preview_url(new_url):
            return None
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


class MainFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="SnapEDA Parts for KiCad", size=(840, 700))
        self.SetMinSize((640, 480))
        self.parts: list[Part] = []
        self.selected: Part | None = None
        self.preview_token = 0
        self.credential_store = CredentialStore.detect()
        self.http_client: SnapEDAClient | None = None
        self.auth_username = ""
        self.operation_active = False
        self.cancel_job = Event()
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(panel, label="Find a part on SnapEDA"), 0, wx.ALL, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.query = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_button = wx.Button(panel, label="Search")
        row.Add(self.query, 1, wx.RIGHT, 8)
        row.Add(self.search_button, 0)
        outer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        splitter = wx.SplitterWindow(panel, style=wx.SP_LIVE_UPDATE)
        preview_panel = wx.Panel(splitter)
        results_panel = wx.Panel(splitter)
        preview_panel.SetMinSize((1, 1))
        results_panel.SetMinSize((1, 1))
        results_sizer = wx.BoxSizer(wx.VERTICAL)
        self.results = wx.ListCtrl(results_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.results.SetMinSize((1, 1))
        self.results.InsertColumn(0, "Part number", width=185)
        self.results.InsertColumn(1, "Manufacturer", width=165)
        self.results.InsertColumn(2, "Description", width=240)
        for index, label in enumerate(("Symbol", "Footprint", "3D"), start=3):
            self.results.InsertColumn(
                index, label, format=wx.LIST_FORMAT_CENTRE, width=56
            )
        results_sizer.Add(self.results, 1, wx.EXPAND)
        results_panel.SetSizer(results_sizer)
        preview_sizer = wx.BoxSizer(wx.VERTICAL)
        self.preview_area = wx.Panel(preview_panel)
        preview_row = wx.BoxSizer(wx.HORIZONTAL)
        self.preview_widgets: dict[str, tuple[PreviewCanvas, wx.StaticText]] = {}
        for label in ("Symbol", "Footprint", "3D model"):
            preview_column = wx.Panel(self.preview_area)
            column_sizer = wx.BoxSizer(wx.VERTICAL)
            heading = wx.StaticText(preview_column, label=label)
            bitmap = PreviewCanvas(preview_column)
            message = wx.StaticText(preview_column, label="Select a part to preview.")
            preview_column.SetMinSize((1, 1))
            column_sizer.Add(heading, 0, wx.BOTTOM, 4)
            column_sizer.Add(bitmap, 1, wx.EXPAND | wx.BOTTOM, 8)
            column_sizer.Add(message, 0, wx.EXPAND)
            preview_column.SetSizer(column_sizer)
            preview_row.Add(preview_column, 1, wx.EXPAND | wx.RIGHT, 8)
            self.preview_widgets[label] = (bitmap, message)
        self.preview_area.SetSizer(preview_row)
        self.preview_area.SetMinSize((1, 1))
        self.details = wx.StaticText(
            preview_panel, label="Select a result to inspect available files."
        )
        line_height = self.details.GetTextExtent("Ag").height
        self.details.SetMinSize((-1, line_height * 3))
        self.auth_button = wx.Button(preview_panel, label="Sign in")
        self.datasheet_button = wx.Button(preview_panel, label="Datasheet")
        self.datasheet_button.Disable()
        self.import_button = wx.Button(preview_panel, label="Download and import…")
        self.import_button.Disable()
        preview_sizer.Add(self.preview_area, 1, wx.EXPAND | wx.BOTTOM, 8)
        preview_sizer.Add(self.details, 0, wx.EXPAND | wx.BOTTOM, 8)
        preview_sizer.Add(self.datasheet_button, 0, wx.BOTTOM, 6)
        preview_sizer.Add(self.auth_button, 0, wx.BOTTOM, 6)
        preview_sizer.Add(self.import_button, 0)
        preview_panel.SetSizer(preview_sizer)
        splitter.SetMinimumPaneSize(100)
        splitter.SplitHorizontally(preview_panel, results_panel, 450)
        outer.Add(splitter, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.status = wx.StaticText(
            panel, label="Search parts by name or manufacturer."
        )
        outer.Add(self.status, 0, wx.ALL, 10)
        panel.SetSizer(outer)
        self.search_button.Bind(wx.EVT_BUTTON, self.on_search)
        self.query.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.results.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select)
        self.results.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.on_deselect)
        self.import_button.Bind(wx.EVT_BUTTON, self.on_import)
        self.auth_button.Bind(wx.EVT_BUTTON, self.on_auth_action)
        self.datasheet_button.Bind(wx.EVT_BUTTON, self.on_open_datasheet)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.job: threading.Thread | None = None
        self.closing = False

    def on_search(self, _event: wx.Event) -> None:
        term = self.query.GetValue().strip()
        if not term or self.operation_active:
            return
        self.operation_active = True
        self.results.DeleteAllItems()
        self.parts.clear()
        self.selected = None
        self.details.SetLabel("Select a result to inspect available files.")
        self.preview_token += 1
        self._request_current_preview()
        self._update_controls()
        self.status.SetLabel("Searching SnapEDA…")
        self.search_button.Disable()

        def work() -> None:
            try:
                parts = search_parts(term)
                wx.CallAfter(self.show_results, parts)
            except Exception:
                wx.CallAfter(self.show_error, "Search failed. Try again later.")

        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()

    def show_results(self, parts: list[Part]) -> None:
        if self.closing:
            return
        self.operation_active = False
        self.parts = parts
        self._update_controls()
        if not parts:
            self.status.SetLabel(
                "No matching parts. Try another part number or manufacturer."
            )
            return
        for part in parts:
            index = self.results.InsertItem(
                self.results.GetItemCount(), part.part_number
            )
            self.results.SetItem(index, 1, part.manufacturer)
            self.results.SetItem(index, 2, part.description)
            for column, available in enumerate(
                (part.has_symbol, part.has_footprint, part.has_3d), start=3
            ):
                self.results.SetItem(index, column, "✓" if available else "")
        self.status.SetLabel(
            f"{len(parts)} results. Select a part, then choose its import destination."
        )

    def show_error(self, message: str) -> None:
        if self.closing:
            return
        self.operation_active = False
        self._update_controls()
        self.status.SetLabel(message)

    def on_select(self, event: wx.ListEvent) -> None:
        index = event.GetIndex()
        self.selected = self.parts[index]
        self.preview_token += 1
        part = self.selected
        self.details.SetLabel(
            f"{part.part_number}\n{part.manufacturer}\nSymbol: {'yes' if part.has_symbol else 'no'}  Footprint: {'yes' if part.has_footprint else 'no'}  3D: {'yes' if part.has_3d else 'no'}"
        )
        self.details.Wrap(max(1, self.details.GetParent().GetClientSize().width))
        self.details.GetParent().Layout()
        self._update_controls()
        if part.export_part_id is None or part.unipart_id is None:
            if part.has_ambiguous_export_ids:
                message = "Symbol and footprint export IDs differ; this part cannot be exported safely."
            elif part.model_3d_id is not None:
                message = "Only a 3D model ID is available; no KiCad symbol/footprint export is available."
            else:
                message = "No supported export IDs are available for this part."
            self.status.SetLabel(message)
        else:
            self.status.SetLabel("Selected part is ready to download.")
        self._request_current_preview()

    def on_deselect(self, event: wx.ListEvent) -> None:
        if self.results.GetFirstSelected() == -1:
            self.selected = None
            self.preview_token += 1
            self.details.SetLabel("Select a result to inspect available files.")
            self._update_controls()
            self._request_current_preview()
        event.Skip()

    def on_open_datasheet(self, _event: wx.Event) -> None:
        part = self.selected
        if part is not None and _is_valid_datasheet_url(part.datasheet_url):
            wx.LaunchDefaultBrowser(part.datasheet_url)

    def _update_controls(self) -> None:
        if self.closing:
            return
        self.search_button.Enable(not self.operation_active)
        self.auth_button.Enable(not self.operation_active)
        part = self.selected
        self.datasheet_button.Enable(
            not self.operation_active
            and part is not None
            and _is_valid_datasheet_url(part.datasheet_url)
        )
        can_export = bool(
            part is not None
            and part.export_part_id is not None
            and part.unipart_id is not None
        )
        self.import_button.Enable(not self.operation_active and can_export)

    def _request_current_preview(self) -> None:
        part = self.selected
        for preview_type, (bitmap, message) in self.preview_widgets.items():
            bitmap.set_image(None)
            if part is None:
                message.SetLabel("Select a part to preview.")
                continue
            url = part.previews.get(preview_type)
            if not url:
                message.SetLabel("No preview is available for this asset.")
                continue
            if not _is_allowed_preview_url(url):
                message.SetLabel("Preview URL is not allowed.")
                continue
            token: PreviewToken = (self.preview_token, part.url, preview_type)
            message.SetLabel("Loading preview…")
            thread = threading.Thread(
                target=self.load_preview, args=(url, token), daemon=True
            )
            thread.start()

    def load_preview(self, url: str, token: PreviewToken) -> None:
        try:
            if not _is_allowed_preview_url(url):
                return

            request = urllib.request.Request(
                url, headers={"User-Agent": "SnapEDA-KiCad-Plugin/1.0"}
            )
            opener = urllib.request.build_opener(_PreviewRedirectHandler())
            with opener.open(request, timeout=8) as response:
                data = response.read(3_000_001)
            if len(data) > 3_000_000:
                return
            image = decode_preview_image(data)
            wx.CallAfter(self.set_preview, image, token)
        except Exception as exc:
            wx.CallAfter(self.set_preview_error, str(exc), token)

    def _current_preview_token(self, preview_type: str) -> PreviewToken:
        part = self.selected
        return (self.preview_token, part.url if part else "", preview_type)

    def set_preview(self, image: wx.Image, token: PreviewToken) -> None:
        if not preview_is_current(
            token,
            self._current_preview_token(token[2]),
            closing=self.closing,
        ):
            return
        preview, message = self.preview_widgets[token[2]]
        preview.set_image(image)
        message.SetLabel("")

    def set_preview_error(self, error: str, token: PreviewToken) -> None:
        if not preview_is_current(
            token,
            self._current_preview_token(token[2]),
            closing=self.closing,
        ):
            return
        self.preview_widgets[token[2]][1].SetLabel(f"Preview unavailable: {error}")

    def on_import(self, _event: wx.Event) -> None:
        part = self.selected
        if part is None:
            return
        if self.operation_active:
            return
        if part.unipart_id is None or part.export_part_id is None:
            if part.has_ambiguous_export_ids:
                self.status.SetLabel(
                    "Symbol and footprint export IDs differ; this part cannot be exported safely."
                )
            elif part.model_3d_id is not None:
                self.status.SetLabel(
                    "Only a 3D model ID is available; no KiCad symbol/footprint export is available."
                )
            else:
                self.status.SetLabel("This part has no supported KiCad export mapping.")
            return
        scope_dialog = wx.SingleChoiceDialog(
            self,
            "Choose where to register the imported libraries. Project scope uses the current KiCad project when available.",
            "Library scope",
            ["Project libraries", "Personal global libraries"],
        )
        if scope_dialog.ShowModal() != wx.ID_OK:
            scope_dialog.Destroy()
            return
        global_scope = scope_dialog.GetSelection() == 1
        scope_dialog.Destroy()
        if global_scope:
            with wx.DirDialog(
                self,
                "Choose the personal library folder",
                style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
            ) as picker:
                if picker.ShowModal() != wx.ID_OK:
                    return
                destination = Path(picker.GetPath())
        else:
            project_root = active_project_root()
            if project_root is None:
                with wx.DirDialog(
                    self,
                    "KiCad project context is unavailable; choose the project root folder",
                    style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
                ) as picker:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    destination = Path(picker.GetPath())
            else:
                destination = project_root
            if (
                wx.MessageBox(
                    f"Import project libraries into {destination}?",
                    "Confirm project destination",
                    wx.OK | wx.CANCEL | wx.ICON_QUESTION,
                    self,
                )
                != wx.ID_OK
            ):
                return
        if (
            global_scope
            and wx.MessageBox(
                "Register this library in KiCad 10's global symbol and footprint tables? Existing entries will be preserved.",
                "Confirm global registration",
                wx.OK | wx.CANCEL | wx.ICON_WARNING,
                self,
            )
            != wx.ID_OK
        ):
            return
        table_dir = None
        if global_scope:
            table_dir = Path(
                os.environ.get(
                    "KICAD_CONFIG_HOME",
                    str(
                        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                        / "kicad"
                        / "10.0"
                    ),
                )
            )
        request = ImportRequest(part, destination, global_scope, table_dir)
        if self.http_client is None or not self.http_client.is_authenticated:
            self._prompt_credentials(request)
            return
        self._start_http_import(request)

    def on_auth_action(self, _event: wx.Event) -> None:
        if self.operation_active:
            return
        if self.http_client is not None:
            self.http_client.close()
            self.http_client = None
            self.auth_username = ""
            self.auth_button.SetLabel("Sign in")
            self.status.SetLabel(
                "Signed out. The HTTP session and cookies were cleared."
            )
            self._update_controls()
            return
        self._prompt_credentials()

    def _prompt_credentials(self, request: ImportRequest | None = None) -> None:
        dialog = CredentialsDialog(self, self.credential_store, self.auth_username)
        result = dialog.ShowModal()
        if result != wx.ID_OK:
            dialog.password_input.Clear()
            dialog.Destroy()
            if request is not None:
                self.status.SetLabel("Sign-in cancelled. No download was started.")
            return
        username = dialog.username_input.GetValue().strip()
        password = dialog.password_input.GetValue()
        remember = dialog.remember_checkbox.GetValue()
        dialog.password_input.Clear()
        dialog.Destroy()
        self._start_login(username, password, remember, request)

    def _start_login(
        self,
        username: str,
        password: str,
        remember: bool,
        request: ImportRequest | None,
    ) -> None:
        if self.operation_active:
            return
        self.operation_active = True
        self.cancel_job = Event()
        self.status.SetLabel("Signing in to SnapEDA…")
        self._update_controls()

        def work() -> None:
            client: SnapEDAClient | None = None
            keyring_failed = False
            try:
                client = SnapEDAClient.authenticate(username, password)
                if remember:
                    try:
                        self.credential_store.set_password(username, password)
                    except Exception:
                        self.credential_store.disable()
                        keyring_failed = True
                if self.closing:
                    client.close()
                    return
                wx.CallAfter(
                    self._login_succeeded,
                    client,
                    username,
                    request,
                    keyring_failed,
                )
                client = None
            except AuthError as exc:
                if client is not None:
                    client.close()
                if not self.closing:
                    wx.CallAfter(self._login_failed, str(exc))
            except Exception:
                if client is not None:
                    client.close()
                if not self.closing:
                    wx.CallAfter(
                        self._login_failed,
                        "Sign-in failed. No request details were recorded.",
                    )

        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()

    def _login_succeeded(
        self,
        client: SnapEDAClient,
        username: str,
        request: ImportRequest | None,
        keyring_failed: bool,
    ) -> None:
        if self.closing:
            client.close()
            return
        self.http_client = client
        self.auth_username = username
        self.auth_button.SetLabel("Sign out")
        self.operation_active = False
        self._update_controls()
        if keyring_failed:
            self.status.SetLabel(
                "Signed in for this session. The OS keyring could not save the password."
            )
        else:
            self.status.SetLabel("Signed in to SnapEDA for this session.")
        if request is not None:
            self._start_http_import(request)

    def _login_failed(self, message: str) -> None:
        if self.closing:
            return
        self.operation_active = False
        self._update_controls()
        self.status.SetLabel(f"Sign-in failed: {message}")

    def _start_http_import(self, request: ImportRequest) -> None:
        client = self.http_client
        if client is None or not client.is_authenticated:
            self.status.SetLabel("Sign in to SnapEDA before downloading.")
            self._update_controls()
            return
        self.operation_active = True
        self.cancel_job = Event()
        self.status.SetLabel("Downloading the selected SnapEDA part…")
        self._update_controls()

        def work() -> None:
            try:
                if self.cancel_job.is_set():
                    raise RuntimeError("Operation cancelled before export")
                with tempfile.TemporaryDirectory(prefix="snapeda-export-") as temp_dir:
                    os.chmod(temp_dir, 0o700)
                    archive_path = Path(temp_dir) / "component.zip"
                    client.export(
                        request.part.export_part_id or 0,
                        request.part.unipart_id or 0,
                        archive_path,
                    )
                    contents = inspect_archive(archive_path.read_bytes())
                _, unmatched_footprints, unmatched_models = model_binding_options(
                    contents
                )
                if self.cancel_job.is_set():
                    raise RuntimeError("Import cancelled before library registration")
                choices: dict[str, str] | None = None
                if unmatched_footprints and unmatched_models:
                    ready = Event()
                    answer: list[dict[str, str] | None] = []
                    if self.closing:
                        raise RuntimeError("Import cancelled while the window closed")
                    wx.CallAfter(
                        self.choose_models,
                        unmatched_footprints,
                        unmatched_models,
                        answer,
                        ready,
                    )
                    if not ready.wait(timeout=600) or not answer or answer[0] is None:
                        raise RuntimeError(
                            "3D model selection was cancelled or timed out"
                        )
                    choices = answer[0]
                if self.cancel_job.is_set():
                    raise RuntimeError("Import cancelled before library registration")
                imported = import_component(
                    contents,
                    request.destination,
                    request.part.part_number,
                    scope="global" if request.global_scope else "project",
                    table_dir=request.table_dir,
                    model_choices=choices,
                )
                if not self.closing:
                    wx.CallAfter(
                        self._http_import_succeeded,
                        imported.root,
                        request.global_scope,
                    )
            except AuthError as exc:
                if exc.session_expired:
                    client.close()
                    if not self.closing:
                        wx.CallAfter(
                            self._http_import_failed,
                            "SnapEDA session expired. Sign in again, then retry the download.",
                            client,
                        )
                elif not self.closing:
                    wx.CallAfter(self._http_import_failed, str(exc))
            except Exception:
                if not self.closing:
                    wx.CallAfter(
                        self._http_import_failed,
                        "Import failed. No credentials or response details were recorded.",
                    )
            finally:
                if self.closing:
                    client.close()
                elif not self.closing:
                    wx.CallAfter(self._finish_operation)

        self.job = threading.Thread(target=work, daemon=True)
        self.job.start()

    def _http_import_succeeded(self, path: Path, global_scope: bool) -> None:
        if self.closing:
            return
        self.show_imported(path, global_scope)
        self._finish_operation()

    def _http_import_failed(
        self, message: str, expired_client: SnapEDAClient | None = None
    ) -> None:
        if self.closing:
            return
        if expired_client is not None and self.http_client is expired_client:
            self.http_client = None
            self.auth_button.SetLabel("Sign in")
        self.status.SetLabel(message)
        self._finish_operation()

    def _finish_operation(self) -> None:
        if self.closing:
            return
        self.operation_active = False
        self._update_controls()

    def choose_models(
        self,
        footprints: list[str],
        models: list[str],
        answer: list[dict[str, str] | None],
        ready: Event,
    ) -> None:
        selected: dict[str, str] = {}
        available = list(models)
        try:
            for footprint in footprints:
                options = available + ["Do not bind a 3D model"]
                dialog = wx.SingleChoiceDialog(
                    self,
                    f"Choose the STEP model for {footprint}.",
                    "Associate 3D model",
                    options,
                )
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        answer.append(None)
                        return
                    model = options[dialog.GetSelection()]
                    if model != "Do not bind a 3D model":
                        selected[footprint] = model
                        available.remove(model)
                    else:
                        selected[footprint] = ""
                finally:
                    dialog.Destroy()
            answer.append(selected)
        finally:
            ready.set()

    def show_imported(self, path: Path, global_scope: bool) -> None:
        if self.closing:
            return
        scope = "global personal" if global_scope else "project"
        self.status.SetLabel(
            f"Imported to {path}; KiCad {scope} library tables were updated. "
            "If new libraries are not visible yet, reload them in KiCad."
        )
        self._update_controls()

    def on_close(self, event: wx.CloseEvent) -> None:
        if self.job and self.job.is_alive():
            if (
                wx.MessageBox(
                    "An operation is running. Close this window? Export will be cancelled; a library transaction already underway will finish.",
                    "Operation running",
                    wx.YES_NO | wx.ICON_WARNING,
                    self,
                )
                != wx.YES
            ):
                event.Veto()
                return
        self.closing = True
        self.cancel_job.set()
        if self.http_client is not None and not (self.job and self.job.is_alive()):
            self.http_client.close()
            self.http_client = None
        event.Skip()


def main() -> None:
    app = wx.App(False)
    frame = MainFrame()
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
