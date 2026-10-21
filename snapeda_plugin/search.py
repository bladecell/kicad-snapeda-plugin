"""SnapEDA's public part-search adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from curl_cffi import requests

BASE_URL = "https://www.snapeda.com"


@dataclass(frozen=True, slots=True)
class Part:
    part_number: str
    manufacturer: str
    url: str
    preview: str
    has_symbol: bool
    has_footprint: bool
    has_3d: bool
    previews: dict[str, str]
    unipart_id: int | None = None
    symbol_id: int | None = None
    footprint_id: int | None = None
    model_3d_id: int | None = None
    description: str = ""
    datasheet_url: str = ""
    has_datasheet: int = 0

    @property
    def export_part_id(self) -> int | None:
        """Return a non-ambiguous symbol/footprint ID, never a 3D model ID."""
        if (
            self.symbol_id is not None
            and self.footprint_id is not None
            and self.symbol_id != self.footprint_id
        ):
            return None
        return self.symbol_id or self.footprint_id

    @property
    def has_ambiguous_export_ids(self) -> bool:
        return (
            self.symbol_id is not None
            and self.footprint_id is not None
            and self.symbol_id != self.footprint_id
        )


def _numeric_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 9_223_372_036_854_775_807 else None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    if not 1 <= len(value) <= 19:
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= 9_223_372_036_854_775_807 else None


def parse_parts(payload: dict[str, Any]) -> list[Part]:
    """Parse the search response, ignoring malformed rows."""
    parts: list[Part] = []
    rows = payload.get("results")
    if not isinstance(rows, list):
        return parts
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = row.get("urlname")
        number = row.get("part_number")
        if not isinstance(slug, str) or not isinstance(number, str):
            continue
        manufacturer = row.get("manufacturer") or ""
        manufacturer_name = str(manufacturer)
        parameters = row.get("te_param")
        images = parameters.get("part_images") if isinstance(parameters, dict) else None
        image_metadata: dict[str, Any] = {}
        previews: dict[str, str] = {}
        if isinstance(images, list) and images:
            first_image = images[0]
            if isinstance(first_image, dict):
                image_metadata = first_image
            preview_value = (
                first_image.get("url") if isinstance(first_image, dict) else None
            )
            if isinstance(preview_value, str):
                previews["Symbol"] = preview_value
        elif isinstance(images, dict):
            image_metadata = images
            for key, label in (
                ("symbol", "Symbol"),
                ("footprint", "Footprint"),
                ("3dmodel", "3D model"),
            ):
                value = images.get(key)
                if isinstance(value, str):
                    previews[label] = value
        preview = previews.get("Symbol", "")
        parts.append(
            Part(
                number,
                manufacturer_name,
                f"{BASE_URL}/parts/{quote(slug, safe='')}/{quote(manufacturer_name, safe='')}/view-part/",
                preview,
                bool(row.get("has_symbol")),
                bool(row.get("has_footprint")),
                bool(row.get("has_3d")),
                previews,
                _numeric_id(row.get("unipart_id")),
                _numeric_id(image_metadata.get("symbol_id")),
                _numeric_id(image_metadata.get("footprint_id")),
                _numeric_id(image_metadata.get("3dmodel_id")),
                (
                    " ".join(row.get("short_description", "").split())
                    if isinstance(row.get("short_description"), str)
                    else ""
                ),
                (
                    row.get("datasheeturl", "")
                    if isinstance(row.get("datasheeturl"), str)
                    else ""
                ),
                (
                    row.get("has_datasheet", 0)
                    if isinstance(row.get("has_datasheet"), int)
                    and not isinstance(row.get("has_datasheet"), bool)
                    and row.get("has_datasheet") in (0, 1)
                    else 0
                ),
            )
        )
    return parts


def search_parts(query: str, timeout: float = 25.0) -> list[Part]:
    """Search SnapEDA and parse its results. Raises on network/service failure."""
    term = query.strip()
    if not term:
        return []
    session = requests.Session()
    session.headers["User-Agent"] = "SnapEDA-KiCad-Plugin/1.0"
    search_params = {"q": term, "search-type": "parts"}
    search_url = f"{BASE_URL}/search/?{urlencode(search_params)}"
    response = session.get(f"{BASE_URL}/search/", params=search_params, timeout=timeout)
    response.raise_for_status()
    csrf = session.cookies.get("csrftoken")
    if not csrf:
        raise RuntimeError("SnapEDA did not provide a search token")
    response = session.post(
        f"{BASE_URL}/api/v1/search_local_internal",
        data={
            "q": term,
            "search-type": "parts",
            "csrfmiddlewaretoken": csrf,
        },
        headers={"Referer": search_url, "X-CSRFToken": csrf},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_parts(response.json())
