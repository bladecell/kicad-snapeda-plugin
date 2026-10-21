from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from tools.build_pcm import build_package, build_repository

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"


def test_build_package_is_reproducible_and_contains_only_pcm_payload(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"

    build_package(ROOT, VERSION, first)
    build_package(ROOT, VERSION, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names, key=lambda name: (name != "metadata.json", name))
        assert "metadata.json" in names
        assert "plugins/plugin.json" in names
        assert "plugins/ipc_action.py" in names
        assert "plugins/launch.py" in names
        assert "plugins/LICENSE" in names
        assert b"GNU GENERAL PUBLIC LICENSE" in archive.read("plugins/LICENSE")
        assert "plugins/snapeda_plugin/browser.py" not in names
        assert "plugins/snapeda_plugin/browser_helper.py" not in names
        assert "plugins/requirements-system.txt" not in names
        assert not any(name.startswith("plugins/tests/") for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        package_metadata = json.loads(archive.read("metadata.json"))
        version = package_metadata["versions"][0]
        assert version == {
            "version": VERSION,
            "status": "development",
            "kicad_version": "10.0",
            "platforms": ["linux"],
            "runtime": "ipc",
        }
        assert "download_url" not in version


def test_repository_keeps_other_packages_and_prior_versions(tmp_path: Path) -> None:
    package = tmp_path / "package.zip"
    build_package(ROOT, VERSION, package)
    old = json.loads(
        (ROOT / "packaging/package-metadata.json").read_text(encoding="utf-8")
    )
    old["identifier"] = "org.example.keep"
    old["versions"] = [{"version": "1.0", "status": "stable", "kicad_version": "10"}]
    previous_release = json.loads(
        (ROOT / "packaging/package-metadata.json").read_text(encoding="utf-8")
    )
    previous_release["versions"] = [
        {"version": "0.0.9", "status": "development", "kicad_version": "10.0"}
    ]
    prior = tmp_path / "old.json"
    prior.write_text(
        json.dumps({"packages": [old, previous_release]}), encoding="utf-8"
    )
    output = tmp_path / "site"

    build_repository(ROOT, VERSION, package, output, prior)

    index = json.loads((output / "packages.json").read_text(encoding="utf-8"))
    assert old in index["packages"]
    package_metadata = next(
        item
        for item in index["packages"]
        if item["identifier"] == "org.snapeda.kicad.parts-search"
    )
    assert [entry["version"] for entry in package_metadata["versions"]] == [
        VERSION,
        "0.0.9",
    ]
    version = package_metadata["versions"][0]
    assert (
        version["download_sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()
    )
    repository = json.loads((output / "repository.json").read_text(encoding="utf-8"))
    assert (
        repository["packages"]["sha256"]
        == hashlib.sha256((output / "packages.json").read_bytes()).hexdigest()
    )


def test_pcm_v2_schema_accepts_generated_metadata(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "tests/fixtures/pcm-schema-v2.json").read_text(encoding="utf-8")
    )
    package = tmp_path / "package.zip"
    build_package(ROOT, VERSION, package)
    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read("metadata.json"))
    jsonschema.Draft7Validator(schema).validate(metadata)

    output = tmp_path / "site"
    repository_path, packages_path = build_repository(
        ROOT, VERSION, package, output, timestamp=1_800_000_000
    )
    package_array_schema = {**schema, "$ref": "#/definitions/PackageArray"}
    repository_schema = {**schema, "$ref": "#/definitions/Repository"}
    jsonschema.Draft7Validator(package_array_schema).validate(
        json.loads(packages_path.read_text(encoding="utf-8"))
    )
    jsonschema.Draft7Validator(repository_schema).validate(
        json.loads(repository_path.read_text(encoding="utf-8"))
    )


def test_build_rejects_non_release_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version"):
        build_package(ROOT, "not-a-version", tmp_path / "bad.zip")
