from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from tools.fetch_pcm_index import fetch_existing_index

ROOT = Path(__file__).resolve().parents[1]


def test_fetch_existing_index_saves_successful_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tools.fetch_pcm_index.urlopen",
        lambda url, timeout: BytesIO(b'{"packages": []}'),
    )
    destination = tmp_path / "packages.json"

    assert fetch_existing_index("https://example.test/packages.json", destination)
    assert destination.read_bytes() == b'{"packages": []}'


def test_fetch_existing_index_allows_only_404_as_initial_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_found(url: str, timeout: float) -> BytesIO:
        raise HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr("tools.fetch_pcm_index.urlopen", not_found)
    destination = tmp_path / "packages.json"
    destination.write_text('{"packages": ["stale"]}', encoding="utf-8")

    assert not fetch_existing_index("https://example.test/packages.json", destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("https://example.test/packages.json", 503, "Unavailable", None, None),
        URLError("network unavailable"),
    ],
)
def test_fetch_existing_index_fails_closed_on_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail(url: str, timeout: float) -> BytesIO:
        raise failure

    monkeypatch.setattr("tools.fetch_pcm_index.urlopen", fail)

    with pytest.raises((HTTPError, URLError)):
        fetch_existing_index(
            "https://example.test/packages.json", tmp_path / "packages.json"
        )


def test_release_workflow_is_tag_only_explicit_repo_and_serialized() -> None:
    workflow = (ROOT / ".github/workflows/pcm-release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" not in workflow
    assert 'gh release create --repo "$GITHUB_REPOSITORY"' in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "tools/fetch_pcm_index.py" in workflow
    assert "tests/test_pcm_workflow.py" in workflow
    assert 'version="${GITHUB_REF_NAME#v}"' in workflow
    assert "cp dist/*.zip artifact/release/" in workflow
    assert "release-files/release/*.zip" in workflow
    assert "path: artifact/site" in workflow
