from snapeda_plugin import search
from snapeda_plugin.search import parse_parts


def test_search_results_expose_capabilities_and_preview() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "ABC-1",
                    "manufacturer": "Maker",
                    "urlname": "abc-1",
                    "has_symbol": True,
                    "has_footprint": True,
                    "has_3d": False,
                    "te_param": {
                        "part_images": [{"url": "https://images.snapeda.com/abc.png"}]
                    },
                }
            ]
        }
    )
    assert parts[0].url == "https://www.snapeda.com/parts/abc-1/Maker/view-part/"
    assert parts[0].preview == "https://images.snapeda.com/abc.png"
    assert (parts[0].has_symbol, parts[0].has_footprint, parts[0].has_3d) == (
        True,
        True,
        False,
    )


def test_search_parses_optional_description_and_datasheet_fields_safely() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "A",
                    "urlname": "a",
                    "short_description": "  Low-noise\n amplifier ",
                    "datasheeturl": "https://vendor.example/a.pdf",
                    "has_datasheet": 1,
                },
                {
                    "part_number": "B",
                    "urlname": "b",
                    "short_description": ["bad"],
                    "datasheeturl": None,
                    "has_datasheet": "bad",
                },
            ]
        }
    )

    assert (parts[0].description, parts[0].datasheet_url, parts[0].has_datasheet) == (
        "Low-noise amplifier",
        "https://vendor.example/a.pdf",
        1,
    )
    assert (parts[1].description, parts[1].datasheet_url, parts[1].has_datasheet) == (
        "",
        "",
        0,
    )


def test_search_ignores_malformed_rows() -> None:
    assert parse_parts({"results": [None, {"urlname": "missing-number"}]}) == []


def test_search_tolerates_malformed_preview_metadata_without_dropping_results() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "ABC-2",
                    "urlname": "abc-2",
                    "manufacturer": "Maker",
                    "te_param": "unexpected string",
                },
                {
                    "part_number": "ABC-3",
                    "urlname": "abc-3",
                    "te_param": {"part_images": "unexpected list value"},
                },
            ]
        }
    )
    assert [part.part_number for part in parts] == ["ABC-2", "ABC-3"]
    assert all(part.preview == "" for part in parts)


def test_search_tolerates_non_list_results() -> None:
    assert parse_parts({"results": None}) == []


def test_search_extracts_preview_from_live_part_images_mapping() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "SI-52008-F",
                    "manufacturer": "Bel Fuse",
                    "urlname": "SI-52008-F",
                    "has_symbol": True,
                    "has_footprint": True,
                    "has_3d": True,
                    "te_param": {
                        "part_images": {
                            "symbol": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-Symbol.png",
                            "footprint": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-Footprint.png",
                            "3dmodel": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-3Dmodel.png",
                        }
                    },
                }
            ]
        }
    )

    assert parts[0].previews == {
        "Symbol": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-Symbol.png",
        "Footprint": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-Footprint.png",
        "3D model": "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-3Dmodel.png",
    }
    assert parts[0].url == (
        "https://www.snapeda.com/parts/SI-52008-F/Bel%20Fuse/view-part/"
    )


def test_search_parses_only_numeric_symbol_and_footprint_export_ids() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "NE555",
                    "manufacturer": "Example",
                    "urlname": "ne555",
                    "unipart_id": "22235084",
                    "te_param": {
                        "part_images": {
                            "symbol_id": "10314751",
                            "footprint_id": "10314751",
                            "3dmodel_id": "12568557",
                        }
                    },
                }
            ]
        }
    )
    part = parts[0]
    assert part.unipart_id == 22235084
    assert part.symbol_id == part.footprint_id == 10314751
    assert part.model_3d_id == 12568557
    assert part.export_part_id == 10314751


def test_search_refuses_ambiguous_ids_and_never_substitutes_3d_id() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "AMBIGUOUS",
                    "urlname": "ambiguous",
                    "unipart_id": "20",
                    "te_param": {
                        "part_images": {
                            "symbol_id": "10",
                            "footprint_id": "11",
                            "3dmodel_id": "12",
                        }
                    },
                },
                {
                    "part_number": "3D ONLY",
                    "urlname": "3d-only",
                    "unipart_id": "21",
                    "te_param": {"part_images": {"3dmodel_id": "13"}},
                },
                {
                    "part_number": "BAD IDS",
                    "urlname": "bad-ids",
                    "unipart_id": "not-a-number",
                    "te_param": {
                        "part_images": {
                            "symbol_id": "0",
                            "footprint_id": "12abc",
                            "3dmodel_id": "14",
                        }
                    },
                },
            ]
        }
    )
    assert parts[0].export_part_id is None
    assert parts[0].unipart_id == 20
    assert parts[1].export_part_id is None
    assert parts[2].export_part_id is None
    assert parts[2].unipart_id is None


def test_search_part_url_encodes_part_and_manufacturer_path_segments() -> None:
    parts = parse_parts(
        {
            "results": [
                {
                    "part_number": "ignored-number",
                    "urlname": "part/a?b#c",
                    "manufacturer": "Mañu/fact?urer#",
                }
            ]
        }
    )

    assert parts[0].url == (
        "https://www.snapeda.com/parts/part%2Fa%3Fb%23c/"
        "Ma%C3%B1u%2Ffact%3Furer%23/view-part/"
    )


def test_search_bootstraps_csrf_from_search_page_and_posts_in_same_session(
    monkeypatch,
) -> None:
    calls = []

    class Response:
        def __init__(self, payload=None):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookies = {"csrftoken": "session-csrf"}

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs, self))
            return Response()

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs, self))
            return Response({"results": []})

    session = Session()
    monkeypatch.setattr(search.requests, "Session", lambda: session)

    assert search.search_parts(" SI-52008-F ") == []

    get_call, post_call = calls
    assert get_call[0:2] == ("get", "https://www.snapeda.com/search/")
    assert get_call[2]["params"] == {"q": "SI-52008-F", "search-type": "parts"}
    assert get_call[3] is post_call[3]
    assert post_call[1] == "https://www.snapeda.com/api/v1/search_local_internal"
    assert post_call[2]["data"] == {
        "q": "SI-52008-F",
        "search-type": "parts",
        "csrfmiddlewaretoken": "session-csrf",
    }
    assert post_call[2]["headers"] == {
        "Referer": "https://www.snapeda.com/search/?q=SI-52008-F&search-type=parts",
        "X-CSRFToken": "session-csrf",
    }
