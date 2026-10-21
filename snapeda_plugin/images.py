"""Read bounded PNG/JPEG dimensions before asking wx to decode pixel data."""

from __future__ import annotations

from dataclasses import dataclass

MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 16_384
_JPEG_START_OF_FRAME = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


@dataclass(frozen=True, slots=True)
class ImageHeader:
    format: str
    width: int
    height: int


def inspect_image_header(data: bytes) -> ImageHeader:
    """Accept only PNG/JPEG and enforce dimension limits without decoding."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValueError("truncated PNG image header")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        info = ImageHeader("png", width, height)
    elif data.startswith(b"\xff\xd8"):
        info = _jpeg_header(data)
    else:
        raise ValueError("unsupported preview image format")
    if (
        info.width < 1
        or info.height < 1
        or info.width > MAX_IMAGE_DIMENSION
        or info.height > MAX_IMAGE_DIMENSION
        or info.width * info.height > MAX_IMAGE_PIXELS
    ):
        raise ValueError("preview image exceeds the pixel limit")
    return info


PreviewToken = tuple[int, str, str]


def preview_is_current(
    request_token: PreviewToken, current_token: PreviewToken, *, closing: bool
) -> bool:
    """Discard decoded previews after selection changes or window closure."""
    return not closing and request_token == current_token


def _jpeg_header(data: bytes) -> ImageHeader:
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            raise ValueError("malformed JPEG image header")
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            raise ValueError("truncated JPEG image header")
        if marker in _JPEG_START_OF_FRAME:
            if segment_length < 7:
                raise ValueError("truncated JPEG frame header")
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return ImageHeader("jpeg", width, height)
        index += segment_length
    raise ValueError("JPEG frame dimensions were not found in the image header")
