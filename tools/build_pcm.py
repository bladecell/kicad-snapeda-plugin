"""Build deterministic KiCad PCM archives and a GitHub Pages repository index."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY = "https://github.com/bladecell/kicad-snapeda-plugin"
PAGES_URL = "https://bladecell.github.io/kicad-snapeda-plugin"
IDENTIFIER = "org.snapeda.kicad.parts-search"
ASSET = f"{IDENTIFIER}_v{{version}}_pcm.zip"
SCHEMA_V2 = "https://go.kicad.org/pcm/schemas/v2"
VERSION_PATTERN = re.compile(
    r"\A(?:0|[1-9]\d{0,3})\.(?:0|[1-9]\d{0,3})\.(?:0|[1-9]\d{0,5})\Z"
)
PAYLOAD_FILES = (
    "plugin.json",
    "ipc_action.py",
    "launch.py",
    "snapeda_plugin/__init__.py",
    "snapeda_plugin/app.py",
    "snapeda_plugin/archive.py",
    "snapeda_plugin/credential_store.py",
    "snapeda_plugin/downloads.py",
    "snapeda_plugin/http_auth.py",
    "snapeda_plugin/images.py",
    "snapeda_plugin/library.py",
    "snapeda_plugin/search.py",
    "snapeda_plugin/sexpr.py",
)
PACKAGE_TEMPLATE = Path("packaging/package-metadata.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _version(version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must be numeric major.minor.patch without a v prefix")


def _metadata(
    root: Path, version: str, *, download: dict[str, Any] | None = None
) -> dict[str, Any]:
    template = _read_json(root / PACKAGE_TEMPLATE)
    if template.get("$schema") != SCHEMA_V2 or template.get("identifier") != IDENTIFIER:
        raise ValueError(
            "package metadata template has unexpected schema or identifier"
        )
    version_info: dict[str, Any] = {
        "version": version,
        "status": "development",
        "kicad_version": "10.0",
        "platforms": ["linux"],
        "runtime": "ipc",
    }
    if download is not None:
        version_info.update(download)
    template["versions"] = [version_info]
    return template


def build_package(root: Path, version: str, output: Path) -> Path:
    """Write deterministic ZIP content following KiCad's Python-plugin layout."""
    _version(version)
    missing = [name for name in PAYLOAD_FILES if not (root / name).is_file()]
    if not (root / "LICENSE").is_file():
        missing.append("LICENSE")
    if missing:
        raise ValueError(f"Required runtime files are missing: {', '.join(missing)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = json.dumps(_metadata(root, version), ensure_ascii=False, indent=2) + "\n"
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        members = [("metadata.json", metadata.encode("utf-8"))]
        members.append(("plugins/__init__.py", b'"""KiCad PCM plugin payload."""\n'))
        members.append(("plugins/LICENSE", (root / "LICENSE").read_bytes()))
        members.extend(
            (f"plugins/{relative}", (root / relative).read_bytes())
            for relative in PAYLOAD_FILES
        )
        for name, content in sorted(members, key=lambda member: member[0]):
            _write_member(archive, name, content)
    return output


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_repository(
    root: Path,
    version: str,
    package: Path,
    output: Path,
    previous_packages: Path | None = None,
    *,
    timestamp: int | None = None,
) -> tuple[Path, Path]:
    """Write PCM v2 repository files, merging prior versions when explicitly supplied."""
    _version(version)
    output.mkdir(parents=True, exist_ok=True)
    if previous_packages is None:
        packages: list[dict[str, Any]] = []
    else:
        prior = _read_json(previous_packages)
        items = prior.get("packages")
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("identifier"), str)
            or not isinstance(item.get("versions"), list)
            or not {
                "name",
                "description",
                "description_full",
                "type",
                "author",
                "license",
                "resources",
            }
            <= item.keys()
            for item in items
        ):
            raise ValueError(
                "previous packages file must contain valid package objects"
            )
        identifiers = [item["identifier"] for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                "previous packages file contains duplicate package identifiers"
            )
        for item in items:
            versions = item["versions"]
            if any(
                not isinstance(entry, dict)
                or not {"version", "status", "kicad_version"} <= entry.keys()
                for entry in versions
            ):
                raise ValueError(
                    f"previous package {item['identifier']} contains invalid versions"
                )
        packages = items.copy()

    raw_archive = package.read_bytes()
    asset_name = ASSET.format(version=version)
    url = f"https://github.com/bladecell/kicad-snapeda-plugin/releases/download/v{version}/{asset_name}"
    download = {
        "download_url": url,
        "download_sha256": hashlib.sha256(raw_archive).hexdigest(),
        "download_size": len(raw_archive),
        "install_size": _install_size(package),
    }
    current = _metadata(root, version, download=download)
    old = next((item for item in packages if item["identifier"] == IDENTIFIER), None)
    if old is not None:
        existing_versions = old["versions"]
        if any(
            isinstance(entry, dict) and entry.get("version") == version
            for entry in existing_versions
        ):
            raise ValueError(
                f"version {version} already exists in previous repository metadata"
            )
        current["versions"].extend(existing_versions)
        packages.remove(old)
    packages.insert(0, current)
    packages_path = output / "packages.json"
    packages_path.write_text(
        json.dumps({"packages": packages}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    epoch = (
        timestamp
        if timestamp is not None
        else int(datetime.now(timezone.utc).timestamp())
    )
    repository = {
        "$schema": "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository",
        "name": "SnapEDA KiCad Plugin Repository",
        "schema_version": 2,
        "maintainer": {
            "name": "bladecell",
            "contact": {"web": "https://github.com/bladecell"},
        },
        "packages": {
            "url": f"{PAGES_URL}/packages.json",
            "sha256": hashlib.sha256(packages_path.read_bytes()).hexdigest(),
            "update_timestamp": epoch,
            "update_time_utc": datetime.fromtimestamp(epoch, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        },
    }
    repository_path = output / "repository.json"
    repository_path.write_text(
        json.dumps(repository, indent=2) + "\n", encoding="utf-8"
    )
    return repository_path, packages_path


def _install_size(package: Path) -> int:
    with zipfile.ZipFile(package) as archive:
        return sum(info.file_size for info in archive.infolist())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", required=True, help="release version without leading v"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--existing-packages", type=Path)
    parser.add_argument(
        "--timestamp",
        type=int,
        help="UTC Unix timestamp for reproducible repository metadata",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        package_path = args.output_dir / ASSET.format(version=args.version)
        build_package(root, args.version, package_path)
        build_repository(
            root,
            args.version,
            package_path,
            args.output_dir,
            args.existing_packages,
            timestamp=args.timestamp,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"PCM build failed: {error}", file=sys.stderr)
        return 2
    print(f"Built {package_path} and PCM repository files in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
