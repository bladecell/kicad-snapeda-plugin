from snapeda_plugin.app import fit_preview_size


def test_preview_size_fits_landscape_image_without_distorting_it() -> None:
    assert fit_preview_size(1600, 900, 1000, 700) == (1000, 562)


def test_preview_size_fits_portrait_image_without_distorting_it() -> None:
    assert fit_preview_size(600, 900, 500, 400) == (266, 400)


def test_preview_size_returns_none_when_there_is_no_room() -> None:
    assert fit_preview_size(600, 400, 0, 200) is None
    assert fit_preview_size(600, 400, 200, 0) is None


def test_symbol_footprint_and_3d_previews_are_visible_side_by_side(monkeypatch) -> None:
    import wx

    from snapeda_plugin.app import MainFrame
    from snapeda_plugin.search import Part

    class UnavailableCredentialStore:
        available = False

    monkeypatch.setattr(
        "snapeda_plugin.app.CredentialStore.detect",
        lambda: UnavailableCredentialStore(),
    )
    app = wx.App(False)
    frame = MainFrame()
    frame.SetSize((1248, 800))
    frame.Show()
    app.Yield()

    frame.parts = [Part("SYNTHETIC", "Maker", "", "", True, True, True, {})]
    selected_event = wx.ListEvent(wx.wxEVT_LIST_ITEM_SELECTED, frame.results.GetId())
    selected_event.SetIndex(0)
    frame.on_select(selected_event)
    app.Yield()

    assert list(frame.preview_widgets) == ["Symbol", "Footprint", "3D model"]
    canvases = [frame.preview_widgets[label][0] for label in frame.preview_widgets]
    assert all(canvas.IsShownOnScreen() for canvas in canvases)
    bounds = [canvas.GetScreenRect() for canvas in canvases]
    assert bounds[0].GetRight() <= bounds[1].GetLeft()
    assert bounds[1].GetRight() <= bounds[2].GetLeft()
    assert bounds[0].width > 0 and bounds[1].width > 0 and bounds[2].width > 0

    for canvas, size in zip(canvases, ((160, 90), (90, 160), (120, 120))):
        image = wx.Image(*size)
        image.SetRGB(wx.Rect(0, 0, *size), 180, 40, 30)
        canvas.set_image(image)
    assert [canvas._source.GetSize() for canvas in canvases] == [
        wx.Size(*size) for size in ((160, 90), (90, 160), (120, 120))
    ]
    assert fit_preview_size(160, 90, 300, 200) == (300, 168)
    assert fit_preview_size(90, 160, 300, 200) == (112, 200)
    assert fit_preview_size(120, 120, 300, 200) == (200, 200)

    frame.Destroy()
    app.Destroy()


def test_datasheet_urls_reject_unsafe_or_malformed_values() -> None:
    from snapeda_plugin.app import _is_valid_datasheet_url

    assert _is_valid_datasheet_url("http://vendor.example/data.pdf")
    assert _is_valid_datasheet_url("https://vendor.example:443/data.pdf")
    for url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "https:///missing-host",
        "https://user:pass@vendor.example/file",
        "https://vendor.example:bad/file",
        "https://vendor.example:0/file",
        "https://vendor.example/a\nb",
    ):
        assert not _is_valid_datasheet_url(url)


def test_selected_part_details_fit_above_auth_button(monkeypatch) -> None:
    import wx

    from snapeda_plugin.app import MainFrame
    from snapeda_plugin.search import Part

    class UnavailableCredentialStore:
        available = False

    monkeypatch.setattr(
        "snapeda_plugin.app.CredentialStore.detect",
        lambda: UnavailableCredentialStore(),
    )
    app = wx.App(False)
    frame = MainFrame()
    frame.SetSize((840, 700))
    frame.Show()
    app.Yield()
    part = Part(
        "LONG-PART-NUMBER",
        "A manufacturer with a long name",
        "",
        "",
        True,
        True,
        True,
        {},
    )
    frame.parts = [part]
    selected_event = wx.ListEvent(wx.wxEVT_LIST_ITEM_SELECTED, frame.results.GetId())
    selected_event.SetIndex(0)
    frame.on_select(selected_event)
    app.Yield()

    details_bottom = frame.details.GetPosition().y + frame.details.GetSize().height
    auth_top = frame.auth_button.GetPosition().y
    datasheet_bottom = (
        frame.datasheet_button.GetPosition().y + frame.datasheet_button.GetSize().height
    )
    auth_bottom = auth_top + frame.auth_button.GetSize().height
    import_bottom = (
        frame.import_button.GetPosition().y + frame.import_button.GetSize().height
    )
    preview_height = frame.preview_area.GetParent().GetClientSize().height
    details_top = frame.details.GetPosition().y
    assert auth_top >= details_bottom
    assert frame.datasheet_button.GetPosition().y >= details_bottom
    assert auth_top >= datasheet_bottom
    assert frame.import_button.GetPosition().y >= auth_bottom
    assert import_bottom <= preview_height
    assert details_top >= 0
    assert details_bottom <= preview_height

    frame.Destroy()
    app.Destroy()


def test_default_preview_pane_is_taller_without_squeezing_results(monkeypatch) -> None:
    import wx

    from snapeda_plugin.app import MainFrame

    class UnavailableCredentialStore:
        available = False

    monkeypatch.setattr(
        "snapeda_plugin.app.CredentialStore.detect",
        lambda: UnavailableCredentialStore(),
    )
    app = wx.App(False)
    frame = MainFrame()
    frame.SetSize((840, 700))
    frame.Show()
    app.Yield()

    splitter = frame.preview_area.GetParent().GetParent()
    preview_pane = frame.preview_area.GetParent()
    results_pane = splitter.GetWindow2()
    assert splitter.GetSashPosition() >= 450
    assert preview_pane.GetClientSize().height > 400
    assert results_pane.GetClientSize().height >= 100

    frame.Destroy()
    app.Destroy()


def test_results_show_separate_cad_capability_columns(monkeypatch) -> None:
    import wx

    from snapeda_plugin.app import MainFrame
    from snapeda_plugin.search import Part

    class UnavailableCredentialStore:
        available = False

    monkeypatch.setattr(
        "snapeda_plugin.app.CredentialStore.detect",
        lambda: UnavailableCredentialStore(),
    )
    app = wx.App(False)
    frame = MainFrame()
    frame.show_results(
        [
            Part(
                "MIXED-1",
                "Maker",
                "",
                "",
                False,
                False,
                True,
                {},
                description="3D only",
            )
        ]
    )

    assert [
        frame.results.GetColumn(index).GetText()
        for index in range(frame.results.GetColumnCount())
    ] == ["Part number", "Manufacturer", "Description", "Symbol", "Footprint", "3D"]
    assert [frame.results.GetItemText(0, index) for index in range(6)] == [
        "MIXED-1",
        "Maker",
        "3D only",
        "",
        "",
        "✓",
    ]

    frame.Destroy()
    app.Destroy()


def test_datasheet_button_opens_only_selected_valid_url_on_click(monkeypatch) -> None:
    import wx

    from snapeda_plugin.app import MainFrame
    from snapeda_plugin.search import Part

    class UnavailableCredentialStore:
        available = False

    monkeypatch.setattr(
        "snapeda_plugin.app.CredentialStore.detect",
        lambda: UnavailableCredentialStore(),
    )
    opened = []
    monkeypatch.setattr(wx, "LaunchDefaultBrowser", opened.append)
    app = wx.App(False)
    frame = MainFrame()
    first = Part(
        "A",
        "Maker",
        "",
        "",
        True,
        False,
        False,
        {},
        description="Low-noise amplifier",
        datasheet_url="https://vendor.example/a.pdf",
    )
    second = Part(
        "B",
        "Maker",
        "",
        "",
        True,
        False,
        False,
        {},
        datasheet_url="javascript:alert(1)",
    )
    frame.parts = [first, second]
    frame.show_results(frame.parts)

    assert not frame.datasheet_button.IsEnabled()
    assert frame.results.GetItemText(0, 2) == "Low-noise amplifier"
    frame.results.Select(0)
    selected_event = wx.ListEvent(wx.wxEVT_LIST_ITEM_SELECTED, frame.results.GetId())
    selected_event.SetIndex(0)
    frame.on_select(selected_event)
    assert frame.datasheet_button.IsEnabled()
    assert frame.results.GetItemText(0, 2) == "Low-noise amplifier"
    assert (
        frame.details.GetMinSize().height
        == frame.details.GetTextExtent("Ag").height * 3
    )
    assert opened == []
    frame.on_open_datasheet(wx.CommandEvent())
    assert opened == ["https://vendor.example/a.pdf"]

    frame.results.Select(0, False)
    frame.results.Select(1)
    selected_event = wx.ListEvent(wx.wxEVT_LIST_ITEM_SELECTED, frame.results.GetId())
    selected_event.SetIndex(1)
    frame.on_select(selected_event)
    assert not frame.datasheet_button.IsEnabled()
    frame.on_open_datasheet(wx.CommandEvent())
    assert opened == ["https://vendor.example/a.pdf"]

    frame.parts.clear()
    frame.selected = None
    frame.results.DeleteAllItems()
    frame.show_results([])
    assert not frame.datasheet_button.IsEnabled()
    frame.Destroy()
    app.Destroy()
