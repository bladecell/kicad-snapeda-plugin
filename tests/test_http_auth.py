from __future__ import annotations

import json
import warnings
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import snapeda_plugin.http_auth as http_auth
from snapeda_plugin.http_auth import (
    AuthError,
    download_component,
    inspect_part_page,
    login,
    validate_download_url,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        text: str = "",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        assert chunk_size > 0
        return self._chunks or ([self.text.encode()] if self.text else [])

    def close(self) -> None:
        self.closed = True


class FakeCookies:
    def __init__(self, csrf: str | None = "cookie-csrf") -> None:
        self.csrf = csrf

    def get(self, name: str) -> str | None:
        return self.csrf if name == "csrftoken" else None

    def clear(self) -> None:
        self.csrf = None


class FakeSession:
    def __init__(
        self, responses: list[FakeResponse], csrf: str | None = "cookie-csrf"
    ) -> None:
        self.responses = responses
        self.cookies = FakeCookies(csrf)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_response: FakeResponse | None = None
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if len(self.calls) > 1 and self.calls[-2][0] == "POST":
            assert self.last_response is not None and self.last_response.closed
        response = self.responses.pop(0)
        self.last_response = response
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        response = self.responses.pop(0)
        self.last_response = response
        return response

    def close(self) -> None:
        self.closed = True


LOGIN_HTML = """<form action="/account/login/?next=/home/" method="post">
<input type="hidden" name="csrfmiddlewaretoken" value="form-csrf">
<input type="text" name="username"><input type="password" name="password">
<input type="hidden" name="plugin" value="1">
<input type="hidden" name="ref_email" value="">
</form>"""


def zip_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("parts/test.kicad_mod", "(footprint test)")
    return buffer.getvalue()


def test_login_uses_rotated_post_login_cookie_for_protected_export(
    tmp_path: Path,
) -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(302, headers={"Location": "/home/"}),
            FakeResponse(
                200,
                headers={"Content-Type": "application/zip"},
                chunks=[zip_bytes()],
            ),
        ]
    )
    csrf = login(session, "person", "private")
    assert csrf == "cookie-csrf"
    method, url, kwargs = session.calls[2]
    assert (method, url) == (
        "POST",
        "https://www.snapeda.com/account/login/?next=/home/",
    )
    assert kwargs["allow_redirects"] is False
    assert kwargs["data"] == {
        "csrfmiddlewaretoken": "form-csrf",
        "username": "person",
        "password": "private",
        "plugin": "1",
        "ref_email": "",
    }
    download_component(
        session, 10314751, 4962805, tmp_path / "component.zip", csrf_token=csrf
    )
    _, _, export_kwargs = session.calls[3]
    assert export_kwargs["data"]["csrfmiddlewaretoken"] == "cookie-csrf"
    assert export_kwargs["headers"]["X-CSRFToken"] == "cookie-csrf"


def test_login_requires_post_login_csrf_cookie() -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(302, headers={"Location": "/home/"}),
        ],
        csrf=None,
    )
    with pytest.raises(AuthError, match="no post-login CSRF cookie"):
        login(session, "person", "private")


def test_password_prompt_aborts_if_getpass_would_fallback_to_echo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fallback_read_attempted = False
    monkeypatch.setattr(http_auth.sys, "argv", ["http_auth", "output.zip"])
    monkeypatch.setattr(http_auth.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "person")

    def warn_then_fallback(_prompt: str) -> str:
        nonlocal fallback_read_attempted
        warnings.warn("echo fallback", http_auth.getpass.GetPassWarning)
        fallback_read_attempted = True
        return "would-have-echoed"

    monkeypatch.setattr(http_auth.getpass, "getpass", warn_then_fallback)
    assert http_auth.main() == 2
    assert fallback_read_attempted is False
    captured = capsys.readouterr()
    assert "would-have-echoed" not in captured.out + captured.err
    assert "cannot safely disable password echo" in captured.err


def test_login_rejects_200_login_error_without_retries() -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(200, text=LOGIN_HTML + "Invalid login"),
        ]
    )
    with pytest.raises(AuthError, match="did not accept the login"):
        login(session, "person", "private")
    assert len(session.calls) == 3


def test_login_stops_at_challenge_and_does_not_submit_password() -> None:
    login_with_challenge = LOGIN_HTML.replace(
        "</form>", '<div class="g-recaptcha" data-sitekey="public-key"></div></form>'
    )
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=login_with_challenge),
        ]
    )
    with pytest.raises(AuthError, match="interactive login challenge"):
        login(session, "person", "private")
    assert len(session.calls) == 2


def test_login_page_script_and_unrelated_hidden_modal_are_not_challenge() -> None:
    html = (
        LOGIN_HTML
        + '<script src="https://www.recaptcha.net/recaptcha/api.js"></script>'
        + '<div hidden class="challenge-modal">Sign-in help</div>'
    )
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=html),
            FakeResponse(200, text=LOGIN_HTML),
        ]
    )
    with pytest.raises(AuthError, match="Login response:.*did not accept"):
        login(session, "person", "private")
    assert [call[0] for call in session.calls] == ["GET", "GET", "POST"]


def test_login_response_script_and_unrelated_modal_means_rejected_not_challenge() -> (
    None
):
    html = (
        LOGIN_HTML
        + '<script src="https://www.recaptcha.net/recaptcha/api.js"></script>'
        + '<div hidden class="challenge-modal">Sign-in help</div>'
    )
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(200, text=html),
        ]
    )
    with pytest.raises(AuthError, match="Login response:.*did not accept"):
        login(session, "person", "private")
    assert len(session.calls) == 3


def test_login_recognizes_cloudflare_interstitial_marker_without_form() -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(
                200,
                text='<div id="cf-challenge-running">Checking browser</div>',
            ),
        ]
    )
    with pytest.raises(AuthError, match="Login page:.*interactive login challenge"):
        login(session, "person", "private")
    assert len(session.calls) == 2


def test_login_rejects_unexpected_redirect_without_following_it() -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(302, headers={"Location": "https://evil.invalid/"}),
        ]
    )
    with pytest.raises(AuthError, match="unexpected URL"):
        login(session, "person", "private")
    assert len(session.calls) == 3


def test_download_saves_only_valid_zip_with_bounded_response(tmp_path: Path) -> None:
    archive = zip_bytes()
    session = FakeSession(
        [
            FakeResponse(
                200, headers={"Content-Type": "application/zip"}, chunks=[archive]
            )
        ]
    )
    target = tmp_path / "component.zip"
    download_component(session, 10314751, 4962805, target)
    assert target.read_bytes() == archive
    assert session.calls[0][0:2] == (
        "POST",
        "https://www.snapeda.com/parts/snapapi/download-component/10314751/4962805/kicad_modv6",
    )


def test_export_parses_known_json_and_fetches_validated_signed_zip(
    tmp_path: Path,
) -> None:
    signed_url = (
        "https://snapeda.s3.amazonaws.com/components/file.zip?signature=private"
    )
    payload = {
        "error": None,
        "has_irreg_tcream": False,
        "has_irreg_tstop": False,
        "has_milling": False,
        "profile_ref": None,
        "url": signed_url,
    }
    api_response = FakeResponse(
        200,
        text=json.dumps(payload),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    archive = zip_bytes()
    zip_response = FakeResponse(
        200,
        headers={"Content-Type": "application/zip"},
        chunks=[archive],
    )
    session = FakeSession([api_response, zip_response])
    target = tmp_path / "component.zip"
    size = download_component(
        session, 10314751, 4962805, target, csrf_token="csrf-value"
    )
    assert size == len(archive)
    assert target.read_bytes() == archive
    assert api_response.closed
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert session.calls[1][1] == signed_url
    assert "headers" not in session.calls[1][2]
    assert session.calls[0][2]["headers"]["X-Requested-With"] == "XMLHttpRequest"


def test_export_api_error_is_sanitized_and_never_fetches_url(tmp_path: Path) -> None:
    response = FakeResponse(
        200,
        text=json.dumps(
            {
                "error": "private server detail",
                "has_irreg_tcream": False,
                "has_irreg_tstop": False,
                "has_milling": False,
                "profile_ref": None,
                "url": "https://snapeda.s3.amazonaws.com/signed.zip?token=secret",
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    session = FakeSession([response])
    with pytest.raises(
        AuthError, match="Export response:.*SnapEDA reported an export error"
    ) as error:
        download_component(session, 10314751, 4962805, tmp_path / "component.zip")
    assert "private server detail" not in str(error.value)
    assert response.closed
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        '{"error": null, "url": "https://snapeda.s3.amazonaws.com/a.zip"}',
        '{"error": null, "has_irreg_tcream": "false", "has_irreg_tstop": false, "has_milling": false, "profile_ref": null, "url": "https://snapeda.s3.amazonaws.com/a.zip"}',
        "not-json",
    ],
)
def test_export_api_rejects_malformed_or_unexpected_json(
    tmp_path: Path, payload: str
) -> None:
    response = FakeResponse(
        200,
        text=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    session = FakeSession([response])
    with pytest.raises(AuthError, match="Export response:"):
        download_component(session, 10314751, 4962805, tmp_path / "component.zip")
    assert response.closed
    assert len(session.calls) == 1


def test_export_json_response_is_bounded_and_closed(tmp_path: Path) -> None:
    response = FakeResponse(
        200,
        headers={"Content-Type": "application/json"},
        chunks=[b" " * (http_auth.MAX_EXPORT_JSON_BYTES + 1)],
    )
    session = FakeSession([response])
    with pytest.raises(AuthError, match="Export response:.*size limit"):
        download_component(session, 10314751, 4962805, tmp_path / "component.zip")
    assert response.closed
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/file.zip?token=secret",
        "https://user@snapeda.s3.amazonaws.com/file.zip?token=secret",
        "http://snapeda.s3.amazonaws.com/file.zip?token=secret",
        "https://s3.amazonaws.com/other-bucket/file.zip?token=secret",
        "https://s3.amazonaws.com/snapeda/file.zip?token=secret",
        "https://s3.amazonaws.com/snapeda/../attacker/file.zip?token=secret",
        "https://s3.amazonaws.com/snapeda/%2e%2e/attacker/file.zip?token=secret",
        "https://s3.amazonaws.com/snapeda/%2f..%2fattacker/file.zip?token=secret",
        "https://s3.amazonaws.com/snapeda/..%5cattacker/file.zip?token=secret",
    ],
)
def test_export_rejects_unapproved_signed_url_without_fetch_or_leak(
    tmp_path: Path, url: str
) -> None:
    payload = {
        "error": None,
        "has_irreg_tcream": False,
        "has_irreg_tstop": False,
        "has_milling": False,
        "profile_ref": None,
        "url": url,
    }
    response = FakeResponse(
        200,
        text=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    session = FakeSession([response])
    with pytest.raises(AuthError) as error:
        download_component(session, 10314751, 4962805, tmp_path / "component.zip")
    assert url.split("?")[0] not in str(error.value)
    assert response.closed
    assert len(session.calls) == 1


def test_signed_download_redirect_is_not_followed(tmp_path: Path) -> None:
    payload = {
        "error": None,
        "has_irreg_tcream": False,
        "has_irreg_tstop": False,
        "has_milling": False,
        "profile_ref": None,
        "url": "https://snapeda.s3.amazonaws.com/file.zip?signature=private",
    }
    api_response = FakeResponse(
        200,
        text=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    zip_response = FakeResponse(
        302,
        headers={"Location": "https://evil.invalid/file.zip?secret=never-log"},
    )
    session = FakeSession([api_response, zip_response])
    with pytest.raises(AuthError, match="Export response:") as error:
        download_component(session, 10314751, 4962805, tmp_path / "component.zip")
    assert "evil.invalid" not in str(error.value)
    assert "never-log" not in str(error.value)
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert api_response.closed
    assert zip_response.closed


def test_part_inspection_reports_only_verified_component_route() -> None:
    html = """<!doctype html>
    <input type="hidden" name="account_id" value="998877">
    <script>
      const exportUrl = "/parts/snapapi/download-component/10314751/4962805/kicad_modv6";
      const unrelated = "/parts/snapapi/download-component/998877/123/kicad_modv6";
    </script>
    """
    response = FakeResponse(200, text=html, headers={"Content-Type": "text/html"})
    session = FakeSession([response])
    result = inspect_part_page(
        session,
        "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
        expected_unipart_id=4962805,
    )
    assert result.http_status == 200
    assert result.mapping == (10314751, 4962805)
    assert response.closed
    assert session.calls[0][2]["allow_redirects"] is False
    assert session.calls[0][2]["stream"] is True


def test_part_inspection_refuses_mismatched_or_ambiguous_component_routes() -> None:
    mismatch = FakeSession(
        [
            FakeResponse(
                200,
                text='<script>"/parts/snapapi/download-component/100/200/kicad_modv6"</script>',
            )
        ]
    )
    result = inspect_part_page(
        mismatch,
        "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
        expected_unipart_id=201,
    )
    assert result.mapping is None

    ambiguous = FakeSession(
        [
            FakeResponse(
                200,
                text=(
                    '<script>"/parts/snapapi/download-component/100/200/kicad_modv6" '
                    '"/parts/snapapi/download-component/101/200/kicad_modv6"</script>'
                ),
            )
        ]
    )
    result = inspect_part_page(
        ambiguous,
        "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
        expected_unipart_id=200,
    )
    assert result.mapping is None


def test_part_inspection_sanitizes_403_and_never_reads_body() -> None:
    response = FakeResponse(403, text="private page body with account details")
    session = FakeSession([response])
    result = inspect_part_page(
        session,
        "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
        expected_unipart_id=200,
    )
    assert result.http_status == 403
    assert result.mapping is None
    assert response.closed


def test_part_inspection_rejects_noncanonical_url_without_request() -> None:
    session = FakeSession([])
    with pytest.raises(AuthError, match="Part inspection:"):
        inspect_part_page(
            session,
            "https://www.snapeda.com/parts/example-maker/example-part/view-part/?token=private",
            expected_unipart_id=200,
        )
    assert session.calls == []


def test_part_inspection_caps_page_and_rejects_external_redirect() -> None:
    oversized = FakeResponse(
        200,
        headers={"Content-Type": "text/html"},
        chunks=[b"x" * (http_auth.MAX_PART_PAGE_BYTES + 1)],
    )
    session = FakeSession([oversized])
    result = inspect_part_page(
        session,
        "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
        expected_unipart_id=200,
    )
    assert result.too_large
    assert result.mapping is None
    assert oversized.closed

    redirect = FakeResponse(
        302,
        headers={"Location": "https://evil.invalid/page?token=private"},
    )
    session = FakeSession([redirect])
    with pytest.raises(AuthError, match="Part inspection:") as error:
        inspect_part_page(
            session,
            "https://www.snapeda.com/parts/example-maker/example-part/view-part/",
            expected_unipart_id=200,
        )
    assert "evil.invalid" not in str(error.value)
    assert "private" not in str(error.value)
    assert redirect.closed
    assert len(session.calls) == 1


def test_cli_inspection_does_not_attempt_component_export(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = "https://www.snapeda.com/parts/example-maker/example-part/view-part/"
    html = (
        "<script>const url = "
        '"/parts/snapapi/download-component/321/654/kicad_modv6";</script>'
    )
    session = FakeSession(
        [FakeResponse(200, text=html, headers={"Content-Type": "text/html"})]
    )
    monkeypatch.setattr(
        http_auth.sys,
        "argv",
        [
            "http_auth",
            "--inspect-part-url",
            url,
            "--expected-unipart-id",
            "654",
        ],
    )
    monkeypatch.setattr(http_auth.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "person")
    monkeypatch.setattr(
        http_auth.getpass, "getpass", lambda _prompt: "private-password"
    )
    monkeypatch.setattr(http_auth, "create_session", lambda: session)
    monkeypatch.setattr(http_auth, "login", lambda *_args: "private-csrf")
    monkeypatch.setattr(
        http_auth,
        "download_component",
        lambda *_args, **_kwargs: pytest.fail("inspection must not export"),
    )
    assert http_auth.main() == 0
    output = capsys.readouterr().out
    assert "part_id=321, unipart_id=654" in output
    assert "private-password" not in output
    assert "private-csrf" not in output


def test_download_refuses_overwrite_and_bad_zip(tmp_path: Path) -> None:
    target = tmp_path / "component.zip"
    target.write_bytes(b"keep")
    session = FakeSession([FakeResponse(200, chunks=[b"not zip"])])
    with pytest.raises(AuthError, match="already exists"):
        download_component(session, 10314751, 4962805, target)
    assert target.read_bytes() == b"keep"


def test_download_rejects_truncated_or_oversized_stream(tmp_path: Path) -> None:
    target = tmp_path / "component.zip"
    session = FakeSession([FakeResponse(200, chunks=[zip_bytes()[:12]])])
    with pytest.raises(AuthError, match="safe KiCad ZIP"):
        download_component(session, 10314751, 4962805, target)
    session = FakeSession([FakeResponse(200, chunks=[b"12345"])])
    with pytest.raises(AuthError, match="size limit"):
        download_component(session, 10314751, 4962805, target, max_bytes=4)
    assert not target.exists()


def test_export_login_redirect_is_not_followed(tmp_path: Path) -> None:
    session = FakeSession(
        [FakeResponse(302, headers={"Location": "/account/login/?next=/home/"})]
    )
    with pytest.raises(AuthError, match="session expired"):
        download_component(
            session,
            10314751,
            4962805,
            tmp_path / "component.zip",
            csrf_token="secret-csrf",
        )
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


def test_authenticated_client_closes_and_clears_session_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(302, headers={"Location": "/home/"}),
        ]
    )
    monkeypatch.setattr(http_auth, "create_session", lambda: session)
    client = http_auth.SnapEDAClient.authenticate("person", "private")
    assert client.is_authenticated
    client.close()
    assert not client.is_authenticated
    assert session.cookies.get("csrftoken") is None
    assert session.closed
    with pytest.raises(AuthError, match="Sign in before exporting"):
        client.export(1, 2, Path("unused.zip"))


def test_authenticated_client_uses_explicit_selected_component_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = zip_bytes()
    session = FakeSession(
        [
            FakeResponse(200),
            FakeResponse(200, text=LOGIN_HTML),
            FakeResponse(302, headers={"Location": "/home/"}),
            FakeResponse(
                200,
                headers={"Content-Type": "application/zip"},
                chunks=[archive],
            ),
        ]
    )
    monkeypatch.setattr(http_auth, "create_session", lambda: session)
    client = http_auth.SnapEDAClient.authenticate("person", "private")
    client.export(221, 332, tmp_path / "component.zip")
    assert "/221/332/kicad_modv6" in session.calls[3][1]


def test_authenticated_client_marks_login_redirect_as_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = FakeSession(
        [FakeResponse(302, headers={"Location": "/account/login/?next=/home/"})]
    )
    monkeypatch.setattr(http_auth, "create_session", lambda: session)
    client = http_auth.SnapEDAClient(session, "csrf-value")
    with pytest.raises(AuthError) as error:
        client.export(1, 2, tmp_path / "component.zip")
    assert error.value.session_expired
    assert client.is_authenticated is False


def test_authenticated_client_marks_unauthorized_export_as_expired(
    tmp_path: Path,
) -> None:
    session = FakeSession([FakeResponse(401)])
    client = http_auth.SnapEDAClient(session, "csrf-value")

    with pytest.raises(AuthError) as error:
        client.export(10314751, 4962805, tmp_path / "component.zip")

    assert error.value.session_expired
    assert not client.is_authenticated


@pytest.mark.parametrize(
    "url",
    [
        "http://www.snapeda.com/file.zip",
        "https://evil.invalid/file.zip",
        "https://www.snapeda.com.evil.invalid/file.zip",
        "https://user@www.snapeda.com/file.zip",
    ],
)
def test_download_url_allowlist_rejects_non_snapeda_urls(url: str) -> None:
    with pytest.raises(AuthError, match="unsupported download host"):
        validate_download_url(url)
