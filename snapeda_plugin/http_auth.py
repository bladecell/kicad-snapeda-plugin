"""Bounded, one-shot HTTP login/export prototype for SnapEDA.

This module deliberately does not persist credentials, cookies, or downloads
outside the user-selected output path. It is separate from the KiCad GUI flow.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
import getpass
import json
import os
import re
import sys
import tempfile
import warnings
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit

from curl_cffi import requests

from .archive import inspect_archive

BASE_URL = "https://www.snapeda.com"
LOGIN_URL = f"{BASE_URL}/account/login/"
SEARCH_URL = f"{BASE_URL}/search/?q=SI-52008-F&search-type=parts"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXPORT_JSON_BYTES = 64 * 1024
MAX_PART_PAGE_BYTES = 2 * 1024 * 1024
MAX_PART_REDIRECTS = 3
CHUNK_SIZE = 64 * 1024


class AuthPhase(StrEnum):
    LOGIN_PAGE = "Login page"
    LOGIN_RESPONSE = "Login response"
    EXPORT_RESPONSE = "Export response"
    PART_INSPECTION = "Part inspection"


class AuthError(RuntimeError):
    """Safe-to-display prototype error without response data or secrets."""

    def __init__(
        self,
        message: str,
        phase: AuthPhase = AuthPhase.LOGIN_PAGE,
        *,
        session_expired: bool = False,
    ) -> None:
        self.message = message
        self.phase = phase
        self.session_expired = session_expired
        super().__init__(f"{phase.value}: {message}")


@dataclass(frozen=True, slots=True)
class PartInspection:
    http_status: int
    mapping: tuple[int, int] | None
    too_large: bool = False


class Response(Protocol):
    status_code: int
    text: str
    headers: dict[str, str]

    def iter_content(self, chunk_size: int) -> Any: ...

    def close(self) -> None: ...


class Session(Protocol):
    cookies: Any

    def get(self, url: str, **kwargs: Any) -> Response: ...

    def post(self, url: str, **kwargs: Any) -> Response: ...

    def close(self) -> None: ...


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.cloudflare_interstitial = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        markers = " ".join((values.get("id") or "", values.get("class") or ""))
        if re.search(
            r"(?:^|\s)(?:cf-chl-widget|cf-browser-verification|cf-error-overview)(?:\s|$)",
            markers,
            re.IGNORECASE,
        ) or (values.get("id") or "").casefold() in {
            "cf-challenge-running",
            "cf-challenge-stage",
        }:
            self.cloudflare_interstitial = True
        if self.current is not None and re.search(
            r"g-recaptcha|h-captcha|cf-turnstile", markers, re.I
        ):
            self.current["challenge"] = True
        if tag == "form":
            self.current = {
                "action": values.get("action"),
                "method": values.get("method", "get"),
                "fields": {},
                "challenge": bool(
                    re.search(r"g-recaptcha|h-captcha|cf-turnstile", markers, re.I)
                ),
            }
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            name = values.get("name")
            if name:
                self.current["fields"][name] = values.get("value", "") or ""
                if re.search(
                    r"(?:g-recaptcha|h-captcha|cf-turnstile)-response", name, re.I
                ):
                    self.current["challenge"] = True
        elif tag == "iframe" and self.current is not None:
            if re.search(
                r"recaptcha|hcaptcha|turnstile", values.get("src") or "", re.I
            ):
                self.current["challenge"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


class _ExportRouteParser(HTMLParser):
    """Collect only executable script text and known public URL attributes."""

    _URL_ATTRIBUTES = {"href", "action", "data-url", "data-download-url", "onclick"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_script = False
        self.evidence: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name in self._URL_ATTRIBUTES:
            value = values.get(name)
            if value:
                self.evidence.append(value)
        if tag == "script":
            self.in_script = True

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.evidence.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False


_EXPORT_ROUTE = re.compile(
    r"/parts/snapapi/download-component/(\d+)/(\d+)/kicad_modv6(?=[/?#\"'\\\s]|$)"
)


def validate_part_page_url(url: str) -> None:
    """Accept only a bare canonical SnapEDA part URL, without query secrets."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise AuthError(
            "Use a valid canonical SnapEDA part page URL.",
            AuthPhase.PART_INSPECTION,
        ) from exc
    segments = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.snapeda.com", "snapeda.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or len(segments) != 6
        or segments[1] != "parts"
        or unquote(segments[2]) in {"", ".", ".."}
        or unquote(segments[3]) in {"", ".", ".."}
        or segments[4] != "view-part"
        or segments[5] != ""
        or "%2f" in parsed.path.casefold()
        or "%5c" in parsed.path.casefold()
        or "\\" in parsed.path
    ):
        raise AuthError(
            "Use a canonical SnapEDA part page URL without query parameters.",
            AuthPhase.PART_INSPECTION,
        )


def _component_mapping(html: str, expected_unipart_id: int) -> tuple[int, int] | None:
    parser = _ExportRouteParser()
    parser.feed(html)
    evidence = " ".join(parser.evidence).replace("\\/", "/")
    matches = {
        (int(part_id), int(unipart_id))
        for part_id, unipart_id in _EXPORT_ROUTE.findall(evidence)
        if int(part_id) > 0 and int(unipart_id) == expected_unipart_id
    }
    return next(iter(matches)) if len(matches) == 1 else None


def inspect_part_page(
    session: Session,
    part_url: str,
    *,
    expected_unipart_id: int,
) -> PartInspection:
    """Inspect an authenticated public part page without exporting or exposing HTML."""
    validate_part_page_url(part_url)
    if expected_unipart_id <= 0:
        raise AuthError(
            "Expected unipart ID must be a positive integer.",
            AuthPhase.PART_INSPECTION,
        )
    current_url = part_url
    for redirect_count in range(MAX_PART_REDIRECTS + 1):
        try:
            response = session.get(
                current_url,
                timeout=30,
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:
            raise AuthError(
                "Part page request failed; no page content was recorded.",
                AuthPhase.PART_INSPECTION,
            ) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            target = urljoin(current_url, location)
            response.close()
            parsed = urlsplit(target)
            if (
                redirect_count >= MAX_PART_REDIRECTS
                or parsed.scheme != "https"
                or parsed.hostname not in {"www.snapeda.com", "snapeda.com"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                raise AuthError(
                    "Part page redirected outside the allowed SnapEDA flow.",
                    AuthPhase.PART_INSPECTION,
                )
            current_url = target
            continue
        if response.status_code != 200:
            response.close()
            return PartInspection(response.status_code, None)
        content_type = response.headers.get("Content-Type", "").casefold()
        if "html" not in content_type:
            response.close()
            return PartInspection(response.status_code, None)
        body = bytearray()
        too_large = False
        try:
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                if len(body) + len(chunk) > MAX_PART_PAGE_BYTES:
                    too_large = True
                    break
                body.extend(chunk)
        finally:
            response.close()
        if too_large:
            return PartInspection(response.status_code, None, too_large=True)
        html = body.decode("utf-8", errors="replace")
        return PartInspection(
            response.status_code,
            _component_mapping(html, expected_unipart_id),
        )
    raise AuthError("Part page redirect limit was reached.", AuthPhase.PART_INSPECTION)


def _parse_login_form(html: str) -> tuple[str, dict[str, str]]:
    parser = _FormParser()
    parser.feed(html)
    if parser.cloudflare_interstitial:
        raise AuthError("SnapEDA presented an interactive login challenge.")
    for form in parser.forms:
        if _is_login_form(form):
            fields = form["fields"]
            if form["challenge"]:
                raise AuthError("SnapEDA presented an interactive login challenge.")
            action = urljoin(LOGIN_URL, str(form.get("action") or LOGIN_URL))
            _require_snapeda_url(action, path_prefix="/account/login/")
            return action, dict(fields)
    raise AuthError("SnapEDA login form was not recognized.")


def _is_login_form(form: dict[str, Any]) -> bool:
    fields = form["fields"]
    return (
        str(form["method"]).casefold() == "post"
        and "username" in fields
        and "password" in fields
        and "csrfmiddlewaretoken" in fields
    )


def _login_response_challenge(html: str) -> tuple[bool, bool]:
    parser = _FormParser()
    parser.feed(html)
    login_forms = [form for form in parser.forms if _is_login_form(form)]
    return parser.cloudflare_interstitial or any(
        form["challenge"] for form in login_forms
    ), bool(login_forms)


def _require_snapeda_url(
    url: str,
    *,
    path_prefix: str | None = None,
    phase: AuthPhase = AuthPhase.LOGIN_PAGE,
) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.snapeda.com", "snapeda.com"}
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.port not in (None, 443))
        or (path_prefix is not None and not parsed.path.startswith(path_prefix))
    ):
        raise AuthError("SnapEDA returned an unexpected URL.", phase)


def validate_download_url(url: str) -> None:
    """Allow exact SnapEDA hosts and observed SnapEDA S3 buckets only."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    snapeda_host = host in {"www.snapeda.com", "snapeda.com"}
    snapeda_bucket = host == "snapeda.s3.amazonaws.com"
    if (
        parsed.scheme != "https"
        or not (snapeda_host or snapeda_bucket)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path
        or parsed.fragment
    ):
        raise AuthError(
            "SnapEDA returned an unsupported download host.",
            AuthPhase.EXPORT_RESPONSE,
        )


def _read_export_json(response: Response) -> dict[str, Any]:
    """Read a small API response without retaining an unbounded response body."""
    body = bytearray()
    try:
        for chunk in response.iter_content(CHUNK_SIZE):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_EXPORT_JSON_BYTES:
                raise AuthError("Export API response exceeded the size limit.")
    finally:
        response.close()
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("Export API returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise AuthError("Export API returned an unexpected JSON schema.")
    if "error" not in payload or payload["error"] is not None:
        raise AuthError("SnapEDA reported an export error.")
    required_bool_fields = (
        "has_irreg_tcream",
        "has_irreg_tstop",
        "has_milling",
    )
    if any(type(payload.get(key)) is not bool for key in required_bool_fields):
        raise AuthError("Export API returned an unexpected JSON schema.")
    profile_ref = payload.get("profile_ref")
    if profile_ref is not None and not isinstance(profile_ref, str):
        raise AuthError("Export API returned an unexpected JSON schema.")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise AuthError("Export API returned an unexpected JSON schema.")
    return payload


def login(session: Session, username: str, password: str) -> str:
    """Bootstrap a fresh anonymous session, then submit one parsed login form."""
    try:
        bootstrap = session.get(SEARCH_URL, timeout=25, allow_redirects=False)
    except Exception as exc:
        raise AuthError("Could not initialize a SnapEDA session.") from exc
    if bootstrap.status_code != 200:
        raise AuthError("Could not initialize a SnapEDA session.")
    try:
        response = session.get(LOGIN_URL, timeout=25, allow_redirects=False)
    except Exception as exc:
        raise AuthError("Could not load the SnapEDA login page.") from exc
    if response.status_code != 200:
        raise AuthError("Could not load the SnapEDA login page.")
    action, fields = _parse_login_form(response.text)
    csrf = fields["csrfmiddlewaretoken"]
    if not csrf:
        raise AuthError("SnapEDA login form had no CSRF token.")
    fields.update(username=username, password=password)
    try:
        submitted = session.post(
            action,
            data=fields,
            headers={"Referer": LOGIN_URL, "Origin": BASE_URL},
            timeout=25,
            allow_redirects=False,
        )
    except Exception as exc:
        raise AuthError(
            "SnapEDA login request failed.",
            AuthPhase.LOGIN_RESPONSE,
        ) from exc
    if submitted.status_code in (200, 401, 403):
        challenge, login_form_present = _login_response_challenge(submitted.text)
        if challenge:
            raise AuthError(
                "SnapEDA presented an interactive login challenge.",
                AuthPhase.LOGIN_RESPONSE,
            )
        if login_form_present:
            raise AuthError(
                "SnapEDA did not accept the login; no retry was made.",
                AuthPhase.LOGIN_RESPONSE,
            )
        raise AuthError(
            "SnapEDA login result was ambiguous.",
            AuthPhase.LOGIN_RESPONSE,
        )
    if submitted.status_code not in (302, 303):
        raise AuthError(
            "SnapEDA returned an unexpected login response.",
            AuthPhase.LOGIN_RESPONSE,
        )
    location = submitted.headers.get("Location", "")
    target = urljoin(LOGIN_URL, location)
    _require_snapeda_url(target, phase=AuthPhase.LOGIN_RESPONSE)
    if urlsplit(target).path != "/home/":
        raise AuthError(
            "SnapEDA returned an unexpected login redirect.",
            AuthPhase.LOGIN_RESPONSE,
        )
    post_login_csrf = session.cookies.get("csrftoken")
    if not isinstance(post_login_csrf, str) or not post_login_csrf:
        raise AuthError(
            "SnapEDA session has no post-login CSRF cookie.",
            AuthPhase.LOGIN_RESPONSE,
        )
    return post_login_csrf


def _download_component(
    session: Session,
    part_id: int,
    unipart_id: int,
    target: Path,
    *,
    csrf_token: str = "",
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> int:
    """Request and validate one component ZIP, refusing overwrite and unsafe URLs."""
    if part_id <= 0 or unipart_id <= 0 or max_bytes <= 0:
        raise AuthError("Part identifiers and size limit must be positive integers.")
    if target.exists() or target.is_symlink():
        raise AuthError("Output path already exists; choose a new path.")
    endpoint = (
        f"{BASE_URL}/parts/snapapi/download-component/"
        f"{part_id}/{unipart_id}/kicad_modv6"
    )
    response = session.post(
        endpoint,
        data={
            "csrfmiddlewaretoken": csrf_token,
            "meta_data": '{"Availability":"","Description":"","MF":"","MP":"","Package":"","Price":""}',
        },
        headers={
            "Referer": SEARCH_URL,
            "Origin": BASE_URL,
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=45,
        allow_redirects=False,
        stream=True,
    )
    content_type = (
        response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    )
    if response.status_code == 200 and (
        content_type == "application/json" or content_type.endswith("+json")
    ):
        payload = _read_export_json(response)
        signed_url = payload["url"]
        validate_download_url(signed_url)
        response = session.get(
            signed_url,
            timeout=45,
            allow_redirects=False,
            stream=True,
        )
    elif response.status_code in (302, 303):
        location = response.headers.get("Location", "")
        candidate = urljoin(endpoint, location)
        response.close()
        if urlsplit(candidate).path.startswith("/account/login/"):
            raise AuthError(
                "SnapEDA session expired; sign in again.",
                session_expired=True,
            )
        validate_download_url(candidate)
        response = session.get(
            candidate,
            timeout=45,
            allow_redirects=False,
            stream=True,
        )
    if response.status_code == 401:
        response.close()
        raise AuthError(
            "SnapEDA session expired; sign in again.",
            session_expired=True,
        )
    if response.status_code != 200:
        response.close()
        raise AuthError("SnapEDA did not return the requested component export.")
    content_type = response.headers.get("Content-Type", "").casefold()
    if "html" in content_type or "json" in content_type:
        response.close()
        raise AuthError("Component export returned a page or API error, not a ZIP.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".snapeda-export-", dir=target.parent
        )
    except Exception:
        response.close()
        raise
    temporary = Path(temporary_name)
    try:
        size = 0
        with os.fdopen(fd, "wb") as output:
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise AuthError(
                        "Component export exceeded the 64 MiB prototype size limit."
                    )
                output.write(chunk)
        if size == 0:
            raise AuthError("Component export was empty.")
        try:
            contents = inspect_archive(temporary.read_bytes())
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise AuthError("Component export was not a safe KiCad ZIP file.") from exc
        if not (contents.symbols or contents.footprints or contents.models):
            raise AuthError("Component ZIP had no supported KiCad assets.")
        if target.exists() or target.is_symlink():
            raise AuthError("Output path appeared during download; refusing overwrite.")
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise AuthError(
                "Output path appeared during download; refusing overwrite."
            ) from exc
        return size
    finally:
        response.close()
        temporary.unlink(missing_ok=True)


def download_component(
    session: Session,
    part_id: int,
    unipart_id: int,
    target: Path,
    *,
    csrf_token: str = "",
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> int:
    """Run one export and attach a safe, unambiguous failure phase."""
    try:
        return _download_component(
            session,
            part_id,
            unipart_id,
            target,
            csrf_token=csrf_token,
            max_bytes=max_bytes,
        )
    except AuthError as exc:
        raise AuthError(
            exc.message,
            AuthPhase.EXPORT_RESPONSE,
            session_expired=exc.session_expired,
        ) from None
    except Exception:
        raise AuthError(
            "Component export request failed.",
            AuthPhase.EXPORT_RESPONSE,
        ) from None


def create_session() -> Session:
    """Create a fresh in-memory curl_cffi session; no cookies are persisted."""
    session = requests.Session(impersonate="chrome")
    session.headers["User-Agent"] = "SnapEDA-KiCad-HTTP-Prototype/0.1"
    return session


class SnapEDAClient:
    """One in-memory authenticated session, serialized by its GUI caller."""

    def __init__(self, session: Session, csrf_token: str) -> None:
        self._session = session
        self._csrf_token = csrf_token
        self._closed = False
        self._expired = False

    @classmethod
    def authenticate(cls, username: str, password: str) -> SnapEDAClient:
        session = create_session()
        try:
            csrf_token = login(session, username, password)
        except Exception:
            try:
                session.cookies.clear()
            finally:
                session.close()
            raise
        return cls(session, csrf_token)

    @property
    def is_authenticated(self) -> bool:
        return not self._closed and not self._expired

    def export(
        self,
        part_id: int,
        unipart_id: int,
        target: Path,
    ) -> int:
        if self._closed or self._expired:
            raise AuthError(
                "Sign in before exporting a part.",
                AuthPhase.EXPORT_RESPONSE,
                session_expired=self._expired,
            )
        try:
            return download_component(
                self._session,
                part_id,
                unipart_id,
                target,
                csrf_token=self._csrf_token,
            )
        except AuthError as exc:
            if exc.session_expired:
                self._expired = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._csrf_token = ""
        try:
            self._session.cookies.clear()
        finally:
            self._session.close()


def main() -> int:
    """Run one local export or inspect one authenticated part page."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="new ZIP output path; existing files are never replaced",
    )
    parser.add_argument("--part-id", type=int, default=10314751)
    parser.add_argument("--unipart-id", type=int, default=4962805)
    parser.add_argument(
        "--inspect-part-url",
        help="inspect a canonical part page for its exact export route; do not download",
    )
    parser.add_argument("--expected-unipart-id", type=int)
    args = parser.parse_args()
    if args.inspect_part_url:
        if args.output is not None:
            parser.error("part inspection does not accept an output path")
        if args.expected_unipart_id is None:
            parser.error("--expected-unipart-id is required with --inspect-part-url")
        if args.expected_unipart_id <= 0:
            parser.error("--expected-unipart-id must be positive")
        try:
            validate_part_page_url(args.inspect_part_url)
        except AuthError as exc:
            parser.error(str(exc))
    else:
        if args.output is None:
            parser.error("output ZIP path is required unless inspecting a part page")
        if args.expected_unipart_id is not None:
            parser.error("--expected-unipart-id requires --inspect-part-url")
    if not sys.stdin.isatty():
        print(
            "Run from a terminal so the password prompt can disable echo.",
            file=sys.stderr,
        )
        return 2
    username = input("SnapEDA username: ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("SnapEDA password (not saved): ")
    except getpass.GetPassWarning:
        print(
            "This terminal cannot safely disable password echo; no password was read.",
            file=sys.stderr,
        )
        return 2
    if not username or not password:
        print("Username and password must not be empty.", file=sys.stderr)
        return 2
    try:
        session = create_session()
        csrf = login(session, username, password)
        if args.inspect_part_url:
            result = inspect_part_page(
                session,
                args.inspect_part_url,
                expected_unipart_id=args.expected_unipart_id,
            )
            print(f"Part page HTTP {result.http_status}.")
            if result.too_large:
                print("Inspection stopped at the 2 MiB limit; no mapping reported.")
            elif result.mapping is None:
                print("No verified export mapping found.")
            else:
                part_id, unipart_id = result.mapping
                print(
                    "Verified export route for the requested unipart ID: "
                    f"part_id={part_id}, unipart_id={unipart_id}."
                )
            return 0
        print(f"Requesting one component export to {args.output}.")
        size = download_component(
            session,
            args.part_id,
            args.unipart_id,
            args.output,
            csrf_token=csrf,
        )
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "HTTP prototype failed; no request details or response data were logged.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Session accepted for this component export; ZIP validated: {args.output} ({size} bytes)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
