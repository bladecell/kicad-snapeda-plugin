from __future__ import annotations

import struct
import zlib

import pytest

from snapeda_plugin.images import inspect_image_header, preview_is_current


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def valid_png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        content = kind + payload
        return (
            struct.pack(">I", len(payload))
            + content
            + struct.pack(">I", zlib.crc32(content))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


def test_reads_png_dimensions_without_decoding_pixels() -> None:
    info = inspect_image_header(png_header(320, 180))
    assert (info.format, info.width, info.height) == ("png", 320, 180)


def test_rejects_png_dimensions_over_pixel_budget() -> None:
    with pytest.raises(ValueError, match="pixel limit"):
        inspect_image_header(png_header(65_535, 65_535))


def test_reads_jpeg_dimensions_from_frame_header() -> None:
    data = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\xb4\x01\x40" + bytes(10)
    info = inspect_image_header(data)
    assert (info.format, info.width, info.height) == ("jpeg", 320, 180)


def test_rejects_unknown_or_truncated_image_headers() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        inspect_image_header(b"GIF89a" + bytes(32))
    with pytest.raises(ValueError, match="header"):
        inspect_image_header(b"\x89PNG\r\n\x1a\n")


def test_preview_callback_must_match_current_selection_and_open_window() -> None:
    current = (5, "SI-52008-F", "Footprint")
    assert preview_is_current(current, current, closing=False)
    assert not preview_is_current((5, "SI-52008-F", "Symbol"), current, closing=False)
    assert not preview_is_current(
        (4, "SI-52008-F", "Footprint"), current, closing=False
    )
    assert not preview_is_current(current, current, closing=True)


def test_wx_decodes_real_preview_bytes_without_memory_input_stream() -> None:
    from snapeda_plugin.app import decode_preview_image

    image = decode_preview_image(valid_png())
    assert image.IsOk()
    assert (image.GetWidth(), image.GetHeight()) == (1, 1)
