"""Validate downloaded archives before exposing KiCad assets."""

from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

MAX_MEMBERS = 80
MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
MAX_TOTAL_SIZE = 128 * 1024 * 1024
MAX_MEMBER_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ArchiveContents:
    symbols: tuple[Asset, ...]
    footprints: tuple[Asset, ...]
    models: tuple[Asset, ...]


def inspect_archive(data: bytes) -> ArchiveContents:
    """Read only KiCad assets from a bounded, path-safe ZIP archive."""
    try:
        bundle = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Downloaded file is not a valid ZIP archive") from exc
    with bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_MEMBERS:
            raise ValueError("Archive contains too many entries")
        names: set[str] = set()
        asset_names: set[str] = set()
        total = 0
        result: dict[str, list[Asset]] = {
            ".kicad_sym": [],
            ".kicad_mod": [],
            ".step": [],
        }
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or "\x00" in entry.filename
            ):
                raise ValueError(f"Archive contains unsafe path: {entry.filename!r}")
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"Archive contains unsafe symbolic link: {entry.filename!r}"
                )
            if entry.is_dir():
                continue
            normalized = entry.filename.casefold()
            if normalized in names:
                raise ValueError(
                    f"Archive contains duplicate entry: {entry.filename!r}"
                )
            names.add(normalized)
            total += entry.file_size
            if entry.file_size > MAX_MEMBER_SIZE or total > MAX_TOTAL_SIZE:
                raise ValueError("Archive expands beyond the permitted size")
            suffix = path.suffix.casefold()
            if suffix in result:
                asset_key = f"{suffix}/{path.name.casefold()}"
                if asset_key in asset_names:
                    raise ValueError(
                        f"Archive has colliding KiCad asset names: {path.name!r}"
                    )
                asset_names.add(asset_key)
                content = bytearray()
                with bundle.open(entry) as stream:
                    while chunk := stream.read(1024 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_MEMBER_SIZE:
                            raise ValueError(
                                "Archive member expands beyond the permitted size"
                            )
                total += len(content) - entry.file_size
                if total > MAX_TOTAL_SIZE:
                    raise ValueError("Archive expands beyond the permitted size")
                result[suffix].append(Asset(path.name, bytes(content)))
        return ArchiveContents(
            tuple(result[".kicad_sym"]),
            tuple(result[".kicad_mod"]),
            tuple(result[".step"]),
        )
