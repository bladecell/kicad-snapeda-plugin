import urllib.request

from snapeda_plugin.app import _is_allowed_preview_url, _PreviewRedirectHandler


def test_snapeda_s3_preview_host_is_allowed() -> None:
    assert _is_allowed_preview_url(
        "https://snapeda.s3.amazonaws.com/partimage/Bel_Fuse/SI-52008-F-Symbol.png"
    )


def test_preview_url_requires_exact_https_host_without_credentials() -> None:
    for url in (
        "http://snapeda.s3.amazonaws.com/image.png",
        "https://snapeda.s3.amazonaws.com.evil.test/image.png",
        "https://user@snapeda.s3.amazonaws.com/image.png",
        "https://snapeda.s3.amazonaws.com:444/image.png",
    ):
        assert not _is_allowed_preview_url(url)


def test_preview_redirect_checks_each_https_target_before_following() -> None:
    handler = _PreviewRedirectHandler()
    request = urllib.request.Request("https://www.snapeda.com/part/image.png")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://snapeda.s3.amazonaws.com/image.png",
    )
    assert redirected is not None
    assert (
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://snapeda.s3.amazonaws.com.evil.test/image.png",
        )
        is None
    )
    assert (
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://snapeda.s3.amazonaws.com/image.png",
        )
        is None
    )
    assert (
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://user@snapeda.s3.amazonaws.com/image.png",
        )
        is None
    )
